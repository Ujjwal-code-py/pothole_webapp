from flask import Flask, render_template, request, jsonify, send_file
import os
import cv2
import json
import tempfile
from werkzeug.utils import secure_filename
from utils.depth_estimation import PotholeDepthEstimator
from utils.cost_estimation import CostEstimator
from cloudinary_config import configure_cloudinary, upload_to_cloudinary, upload_annotated_image
from models import Location, MediaFile, PotholeAnalysis, PotholeDetails, CostAnalysis, TimeEstimation
from database import db
from datetime import datetime
import io  # For in-memory PDF buffer

# ReportLab imports for PDF generation
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
db.get_connection()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'pothole-detection-secret-key')
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['RESULTS_FOLDER'] = 'results'
# NOTE: Render default upload limit may be ~25MB. Keep uploads smaller or configure server.
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max (Flask-side)

# Allowed extensions
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'mp4', 'avi', 'mov', 'mkv'}

# Initialize estimators and services
depth_estimator = PotholeDepthEstimator()
cost_estimator = CostEstimator()
configure_cloudinary()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_temp_file_path(filename):
    secure_name = secure_filename(filename)
    return os.path.join(tempfile.gettempdir(), secure_name)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/analytics')
def analytics():
    return render_template('analytics.html')

@app.route('/analytics-data')
def analytics_data():
    """Return arrays for charts: dates, potholes_detected, avg_depth, material_used"""
    try:
        cursor = db.get_cursor()
        cursor.execute("""
            SELECT pa.analysis_date AS date,
                   pa.total_potholes AS potholes,
                   pa.average_depth_cm AS avg_depth,
                   ca.material_cost AS material_cost,
                   pa.total_volume_liters AS total_volume
            FROM pothole_analysis pa
            LEFT JOIN cost_analysis ca ON pa.analysis_id = ca.analysis_id
            ORDER BY pa.analysis_date ASC
            LIMIT 200
        """)
        rows = cursor.fetchall()
        cursor.close()

        dates = []
        potholes = []
        avg_depth = []
        material_used = []

        for r in rows:
            # r may be sqlite.Row or tuple/dict depending on db implementation
            # Use key access when available
            try:
                date = r['date'] if isinstance(r, dict) else r[0]
                p = r['potholes'] if isinstance(r, dict) else r[1]
                d = r['avg_depth'] if isinstance(r, dict) else r[2]
                m = r['total_volume'] if isinstance(r, dict) else r[4]
            except Exception:
                # fallback index-based
                date = r[0]
                p = r[1]
                d = r[2]
                m = r[4] if len(r) > 4 else 0

            # Normalize date string
            if isinstance(date, (int, float)):
                date_str = datetime.fromtimestamp(date).strftime('%Y-%m-%d')
            else:
                date_str = str(date)[:10]
            dates.append(date_str)
            potholes.append(int(p or 0))
            avg_depth.append(float(d or 0.0))
            material_used.append(float(m or 0.0))

        return jsonify({
            'success': True,
            'dates': dates,
            'potholes_detected': potholes,
            'avg_depth': avg_depth,
            'material_used': material_used
        })
    except Exception as e:
        print("Analytics data error:", e)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    temp_path = None
    try:
        # Basic validation
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file selected'}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400

        if not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Invalid file type. Please upload images (PNG, JPG) or videos (MP4, AVI, MOV)'}), 400

        # Save uploaded file to OS temp directory
        temp_path = get_temp_file_path(file.filename)
        file.save(temp_path)
        print(f"File saved to temporary location: {temp_path}")

        # Read cost params and location data from form (with defaults)
        material_cost = float(request.form.get('material_cost', 40.0))
        labor_cost = float(request.form.get('labor_cost', 300.0))
        team_size = int(request.form.get('team_size', 2))
        overhead = float(request.form.get('overhead', 15.0))

        location_data = {
            'location_name': request.form.get('location_name', ''),
            'latitude': request.form.get('latitude', ''),
            'longitude': request.form.get('longitude', ''),
            'city': request.form.get('city', ''),
            'additional_notes': request.form.get('additional_notes', '')
        }

        # Upload original file to Cloudinary (defensive)
        file_ext = file.filename.lower().split('.')[-1]
        file_type = 'image' if file_ext in ['png', 'jpg', 'jpeg'] else 'video'

        print("Uploading to Cloudinary...")
        try:
            upload_result = upload_to_cloudinary(temp_path, 'uploads', file_type)
        except Exception as e:
            print("Cloudinary upload exception:", e)
            return jsonify({'success': False, 'error': 'Cloudinary upload failed — check API keys or network'}), 500

        if not upload_result or not upload_result.get('success'):
            print("Cloudinary returned failure:", upload_result)
            return jsonify({'success': False, 'error': f'Cloudinary upload failed: {upload_result.get("error", "Unknown")}'}), 500

        print("Cloudinary upload successful:", upload_result.get('url'))

        # Store media file and location in DB
        media_data = {
            'original_filename': file.filename,
            'file_type': file_type,
            'original_file_url': upload_result['url'],
            'processed_file_url': None,
            'file_size': upload_result.get('bytes', 0)
        }
        media_id = MediaFile.create(media_data)
        if not media_id:
            return jsonify({'success': False, 'error': 'Failed to store media file in database'}), 500

        location_id = Location.create(location_data)
        if not location_id:
            return jsonify({'success': False, 'error': 'Failed to store location in database'}), 500

        # Process based on file type
        print("Processing file for pothole detection...")
        if file_type == 'image':
            result = process_image(temp_path, material_cost, labor_cost, team_size, overhead, location_id, media_id, file.filename)
        else:
            result = process_video(temp_path, material_cost, labor_cost, team_size, overhead, location_id, media_id, file.filename)

        # Ensure result is JSON-serializable and always return JSON
        if not isinstance(result, dict):
            return jsonify({'success': False, 'error': 'Unexpected processing result type'}), 500

        return jsonify(result)

    except Exception as e:
        # Catch-all: always return JSON on errors
        print("Upload error:", e)
        return jsonify({'success': False, 'error': f'Upload failed: {str(e)}'}), 500

    finally:
        # Cleanup temporary file
        try:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
                print(f"Cleaned up temporary file: {temp_path}")
        except Exception as e:
            print("Temp file cleanup failed:", e)

def process_image(image_path, material_cost, labor_cost, team_size, overhead, location_id, media_id, filename):
    try:
        print("Processing image...")
        results = depth_estimator.calculate_pothole_dimensions(image_path)

        if not results:
            return {'success': False, 'error': 'No potholes detected in the image'}

        pothole_data, image = results
        print(f"Found {len(pothole_data)} potholes")

        cost_estimator.material_cost_per_liter = material_cost
        cost_estimator.labor_cost_per_hour = labor_cost
        cost_estimator.team_size = team_size
        cost_estimator.overhead_percentage = overhead

        cost_breakdown = cost_estimator.calculate_repair_cost(pothole_data)

        # Annotate image
        result_image = image.copy()
        for pothole in pothole_data:
            x1, y1, x2, y2 = pothole['bbox']
            cv2.rectangle(result_image, (x1, y1), (x2, y2), (0, 255, 0), 2)
            info_text = f"Pothole {pothole.get('id', '')}"
            cv2.putText(result_image, info_text, (x1, max(y1-10, 0)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 2)

        # Upload annotated image
        try:
            annotated_result = upload_annotated_image(result_image, filename, 'results')
        except Exception as e:
            print("Annotated image upload error:", e)
            return {'success': False, 'error': 'Annotated image upload failed'}

        if not annotated_result.get('success'):
            return {'success': False, 'error': 'Annotated image upload failed'}

        # Update media processed URL in DB
        cursor = db.get_cursor()
        try:
            cursor.execute("UPDATE media_files SET processed_file_url = %s WHERE media_id = %s", 
              (annotated_result['url'], media_id))

            db.get_connection().commit()
        finally:
            cursor.close()

        # Store analysis data
        analysis_id = store_analysis_data(location_id, media_id, pothole_data, cost_breakdown, material_cost, labor_cost, team_size, overhead)
        if not analysis_id:
            return {'success': False, 'error': 'Failed to store analysis data'}

        return {
            'success': True,
            'file_type': 'image',
            'potholes_detected': len(pothole_data),
            'pothole_data': pothole_data,
            'cost_breakdown': cost_breakdown,
            'result_image': annotated_result['url'],
            'location_data': {'location_name': request.form.get('location_name', ''), 'city': request.form.get('city', '')}
        }
    except Exception as e:
        print("Image processing error:", e)
        return {'success': False, 'error': f'Image processing failed: {str(e)}'}

def process_video(video_path, material_cost, labor_cost, team_size, overhead, location_id, media_id, filename):
    try:
        print("Processing video...")
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return {'success': False, 'error': 'Could not open video file'}

        fps = cap.get(cv2.CAP_PROP_FPS)
        total_video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        frame_count = 0
        total_frames_analyzed = 0
        all_potholes = []

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            try:
                results = depth_estimator.calculate_pothole_dimensions_from_array(frame)
                total_frames_analyzed += 1
                if results and results[0]:
                    pothole_data, _ = results
                    all_potholes.extend(pothole_data)
            except Exception as e:
                print(f"Frame processing error at {frame_count}:", e)
                total_frames_analyzed += 1

            frame_count += 1
            if frame_count % 50 == 0:
                print(f"Processed {frame_count}/{total_video_frames} frames...")

        cap.release()

        if total_frames_analyzed == 0:
            return {'success': False, 'error': 'No frames could be processed from the video'}
        if not all_potholes:
            return {'success': False, 'error': 'No potholes detected in the video'}

        # IoU dedupe
        def calculate_iou(box1, box2):
            x11, y11, x21, y21 = box1
            x12, y12, x22, y22 = box2
            xi1 = max(x11, x12); yi1 = max(y11, y12)
            xi2 = min(x21, x22); yi2 = min(y21, y22)
            inter = max(0, xi2 - xi1) * max(0, yi2 - yi1)
            area1 = (x21 - x11) * (y21 - y11)
            area2 = (x22 - x12) * (y22 - y12)
            union = area1 + area2 - inter
            return inter / union if union > 0 else 0

        unique_potholes = []
        for p in all_potholes:
            duplicate = False
            for ex in unique_potholes:
                if calculate_iou(p['bbox'], ex['bbox']) > 0.3:
                    duplicate = True
                    break
            if not duplicate:
                unique_potholes.append(p)

        cost_estimator.material_cost_per_liter = material_cost
        cost_estimator.labor_cost_per_hour = labor_cost
        cost_estimator.team_size = team_size
        cost_estimator.overhead_percentage = overhead
        cost_breakdown = cost_estimator.calculate_repair_cost(unique_potholes)

        # Create annotated summary frame
        result_image_url = None
        cap = cv2.VideoCapture(video_path)
        ret, first_frame = cap.read()
        cap.release()
        if ret and unique_potholes:
            result_frame = first_frame.copy()
            for i, pothole in enumerate(unique_potholes[:10]):
                x1, y1, x2, y2 = pothole['bbox']
                cv2.rectangle(result_frame, (x1, y1), (x2, y2), (0,255,0), 2)
                cv2.putText(result_frame, f"Pothole {i+1}", (x1, max(y1-10,0)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 2)
            try:
                annotated_result = upload_annotated_image(result_frame, f"video_summary_{filename}", 'results')
                if annotated_result.get('success'):
                    result_image_url = annotated_result['url']
                    cursor = db.get_cursor()
                    try:
                        cursor.execute("UPDATE media_files SET processed_file_url = %s WHERE media_id = %s", (annotated_result['url'], media_id))
                        db.get_connection().commit()
                    finally:
                        cursor.close()
            except Exception as e:
                print("Video annotated upload failed:", e)

        analysis_id = store_analysis_data(location_id, media_id, unique_potholes, cost_breakdown, material_cost, labor_cost, team_size, overhead)
        if not analysis_id:
            return {'success': False, 'error': 'Failed to store analysis data'}

        return {
            'success': True,
            'file_type': 'video',
            'potholes_detected': len(unique_potholes),
            'total_frames_analyzed': total_frames_analyzed,
            'total_video_frames': total_video_frames,
            'pothole_data': unique_potholes[:10],
            'cost_breakdown': cost_breakdown,
            'result_image': result_image_url,
            'location_data': {'location_name': request.form.get('location_name', ''), 'city': request.form.get('city', '')}
        }

    except Exception as e:
        print("Video processing error:", e)
        return {'success': False, 'error': f'Video processing failed: {str(e)}'}

def store_analysis_data(location_id, media_id, pothole_data, cost_breakdown, material_cost, labor_cost, team_size, overhead):
    try:
        total_volume = sum(p['volume_liters'] for p in pothole_data) if pothole_data else 0
        avg_width = sum(p['width_cm'] for p in pothole_data) / len(pothole_data) if pothole_data else 0
        avg_depth = sum(p['depth_cm'] for p in pothole_data) / len(pothole_data) if pothole_data else 0

        analysis_data = {
            'location_id': location_id,
            'media_id': media_id,
            'total_potholes': len(pothole_data),
            'total_volume_liters': total_volume,
            'average_width_cm': avg_width,
            'average_depth_cm': avg_depth
        }
        analysis_id = PotholeAnalysis.create(analysis_data)
        if not analysis_id:
            return None

        if pothole_data:
            PotholeDetails.create_batch(analysis_id, pothole_data)

        cost_data = {
            'analysis_id': analysis_id,
            'material_cost': cost_breakdown.get('material_cost', 0),
            'labor_cost': cost_breakdown.get('labor_cost', 0),
            'equipment_cost': cost_breakdown.get('equipment_cost', 0),
            'transport_cost': cost_breakdown.get('transport_cost', 0),
            'overhead_cost': cost_breakdown.get('overhead_cost', 0),
            'total_cost': cost_breakdown.get('total_cost', 0),
            'cost_parameters': {
                'material_cost_per_liter': material_cost,
                'labor_cost_per_hour': labor_cost,
                'team_size': team_size,
                'overhead_percentage': overhead
            }
        }
        CostAnalysis.create(cost_data)

        if 'time_breakdown' in cost_breakdown:
            time_data = {
                'analysis_id': analysis_id,
                'total_hours': cost_breakdown['time_breakdown'].get('total_hours', 0),
                'setup_time': cost_breakdown['time_breakdown'].get('setup_time', 0),
                'prep_time': cost_breakdown['time_breakdown'].get('prep_time', 0),
                'fill_time': cost_breakdown['time_breakdown'].get('fill_time', 0),
                'compact_time': cost_breakdown['time_breakdown'].get('compact_time', 0),
                'cleanup_time': cost_breakdown['time_breakdown'].get('cleanup_time', 0)
            }
            TimeEstimation.create(time_data)

        return analysis_id
    except Exception as e:
        print("Error storing analysis data:", e)
        return None

@app.route('/results/<filename>')
def get_result_image(filename):
    return jsonify({'success': False, 'error': 'Use Cloudinary URL directly'})

@app.route('/generate_report', methods=['POST'])
def generate_report():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=1*inch, bottomMargin=1*inch)
        elements = []
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=18, spaceAfter=30, alignment=1, textColor=colors.HexColor('#2563eb'))
        elements.append(Paragraph("Pothole Inspection Report", title_style))
        elements.append(Spacer(1, 20))

        details_data = [
            ['Report Date', datetime.now().strftime('%Y-%m-%d %H:%M:%S')],
            ['File Type', data.get('file_type', 'N/A')],
            ['Potholes Detected', str(data.get('potholes_detected', 0))],
        ]

        location_data = data.get('location_data', {})
        if location_data.get('location_name'):
            details_data.append(['Location', location_data['location_name']])
        if location_data.get('city'):
            details_data.append(['City', location_data['city']])

        details_table = Table(details_data, colWidths=[2*inch, 3*inch])
        details_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e293b')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8fafc')),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ]))
        elements.append(details_table)
        elements.append(Spacer(1, 30))

        cost_breakdown = data.get('cost_breakdown', {})
        material_cost = f"₹{cost_breakdown.get('material_cost', 0):.2f}"
        labor_cost = f"₹{cost_breakdown.get('labor_cost', 0):.2f}"
        equipment_transport = f"₹{(cost_breakdown.get('equipment_cost', 0) + cost_breakdown.get('transport_cost', 0)):.2f}"
        overhead_cost = f"₹{cost_breakdown.get('overhead_cost', 0):.2f}"
        total_cost = f"₹{cost_breakdown.get('total_cost', 0):.2f}"

        cost_data = [
            ['Cost Item', 'Amount (₹)'],
            ['Material Cost', material_cost],
            ['Labor Cost', labor_cost],
            ['Equipment & Transport', equipment_transport],
            ['Overhead', overhead_cost],
            ['TOTAL COST', total_cost]
        ]

        cost_table = Table(cost_data, colWidths=[3*inch, 2*inch])
        cost_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#10b981')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -2), colors.HexColor('#f8fafc')),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#1e293b')),
            ('TEXTCOLOR', (0, -1), (-1, -1), colors.whitesmoke),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ]))
        elements.append(Paragraph("Cost Breakdown", styles['Heading2']))
        elements.append(Spacer(1, 10))
        elements.append(cost_table)

        pothole_data = data.get('pothole_data', [])
        if pothole_data:
            elements.append(Spacer(1, 30))
            elements.append(Paragraph("Pothole Details", styles['Heading2']))
            elements.append(Spacer(1, 10))
            pothole_table_data = [['ID', 'Width (cm)', 'Depth (cm)', 'Volume (L)']]
            for pothole in pothole_data[:10]:
                pothole_table_data.append([
                    str(pothole.get('id', '')),
                    f"{pothole.get('width_cm', 0):.1f}",
                    f"{pothole.get('depth_cm', 0):.1f}",
                    f"{pothole.get('volume_liters', 0):.2f}"
                ])
            pothole_table = Table(pothole_table_data, colWidths=[0.5*inch, 1.2*inch, 1.2*inch, 1.2*inch])
            pothole_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3b82f6')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8fafc')),
                ('FONTSIZE', (0, 1), (-1, -1), 9),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
            ]))
            elements.append(pothole_table)

        doc.build(elements)
        buffer.seek(0)
        if buffer.getbuffer().nbytes == 0:
            raise Exception("Generated PDF is empty")

        print("PDF generated successfully, size:", buffer.getbuffer().nbytes)
        return send_file(buffer, as_attachment=True, download_name=f'pothole_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf', mimetype='application/pdf')

    except Exception as e:
        print("PDF generation error:", e)
        return jsonify({'success': False, 'error': f'PDF generation failed: {str(e)}'}), 500

@app.route('/history')
def get_history():
    try:
        cursor = db.get_cursor()
        query = """
            SELECT 
                pa.analysis_id,
                pa.total_potholes,
                pa.total_volume_liters,
                pa.analysis_date,
                l.location_name,
                l.city,
                l.latitude,
                l.longitude,
                mf.original_filename,
                mf.file_type,
                mf.processed_file_url as result_image_url,
                ca.total_cost
            FROM pothole_analysis pa
            LEFT JOIN locations l ON pa.location_id = l.location_id
            LEFT JOIN media_files mf ON pa.media_id = mf.media_id
            LEFT JOIN cost_analysis ca ON pa.analysis_id = ca.analysis_id
            ORDER BY pa.analysis_date DESC
            LIMIT 50
        """
        cursor.execute(query)
        history = cursor.fetchall()
        cursor.close()

        history_list = []
        for row in history:
            # Convert row to dict if necessary
            if hasattr(row, 'keys'):
                history_list.append(dict(row))
            else:
                # fallback to tuple mapping (best-effort)
                history_list.append({
                    'analysis_id': row[0],
                    'total_potholes': row[1],
                    'total_volume_liters': row[2],
                    'analysis_date': row[3],
                    'location_name': row[4],
                    'city': row[5],
                    'latitude': row[6],
                    'longitude': row[7],
                    'original_filename': row[8],
                    'file_type': row[9],
                    'result_image_url': row[10],
                    'total_cost': row[11] if len(row) > 11 else None
                })

        return jsonify({'success': True, 'history': history_list})
    except Exception as e:
        print("History error:", e)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.teardown_appcontext
def close_db(error):
    db.close()

if __name__ == '__main__':
    # Ensure uploads/results directories exist locally (not required when using Cloudinary)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['RESULTS_FOLDER'], exist_ok=True)

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
