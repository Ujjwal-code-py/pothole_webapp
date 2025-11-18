import cv2
import numpy as np
from ultralytics import YOLO

class PotholeDepthEstimator:
    def __init__(self, model_path="models/best.pt"):
        self.model = YOLO(model_path)

    def calculate_pothole_dimensions(self, image_path):
        """
        Detect potholes and estimate dimensions from an image file.
        Returns list of pothole data and the annotated image.
        """
        image = cv2.imread(image_path)
        if image is None:
            return None

        results = self.model.predict(image)[0]

        potholes = []
        annotated_image = image.copy()

        for i, box in enumerate(results.boxes.xyxy):
            x1, y1, x2, y2 = map(int, box.tolist())

            width_pixels = x2 - x1
            height_pixels = y2 - y1

            # Depth estimation (fallback values)
            depth_info = self.estimate_depth(width_pixels, height_pixels)

            # Convert width in cm
            width_cm = depth_info["width_cm"]
            depth_cm = depth_info["depth_cm"]

            # Convert width to meters
            width_m = width_cm / 100.0
            depth_m = depth_cm / 100.0

            # Cylindrical volume approximation
            radius_m = width_m / 2
            volume_m3 = 3.14159 * (radius_m ** 2) * depth_m
            volume_liters = volume_m3 * 1000  # m^3 → L



            pothole_info = {
                "id": i + 1,
                "bbox": [x1, y1, x2, y2],
                "width_cm": round(width_cm, 2),
                "depth_cm": round(depth_cm, 2),
                "volume_liters": round(volume_liters, 2)
            }

            potholes.append(pothole_info)

            # Draw bounding box
            cv2.rectangle(annotated_image, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(
                annotated_image,
                f"Pothole {i+1}",
                (x1, max(y1 - 10, 0)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2
            )

        return potholes, annotated_image

    def calculate_pothole_dimensions_from_array(self, frame):
        """
        Detect potholes and estimate dimensions directly from a frame array (video).
        """
        results = self.model.predict(frame)[0]
        potholes = []

        for i, box in enumerate(results.boxes.xyxy):
            x1, y1, x2, y2 = map(int, box.tolist())

            width_pixels = x2 - x1
            height_pixels = y2 - y1

            depth_info = self.estimate_depth(width_pixels, height_pixels)
            width_cm = depth_info["width_cm"]
            depth_cm = depth_info["depth_cm"]

            width_m = width_cm / 100.0
            depth_m = depth_cm / 100.0

            radius_m = width_m / 2
            volume_m3 = 3.14159 * radius_m * radius_m * depth_m
            volume_liters = volume_m3 * 1000

            # ⭐ Ensure minimum volume = 1 liter
            if volume_liters < 1:
                volume_liters = 1

            pothole_info = {
                "id": i + 1,
                "bbox": [x1, y1, x2, y2],
                "width_cm": round(width_cm, 2),
                "depth_cm": round(depth_cm, 2),
                "volume_liters": round(volume_liters, 2)
            }

            potholes.append(pothole_info)

        return potholes, frame

    # --------------------------
    # SIMPLE DEPTH ESTIMATION
    # --------------------------
    def estimate_depth(self, width_px, height_px):
        """
        Rough depth estimation based on object size in frame.
        Modify this logic as needed.
        """

        # Base rule-of-thumb conversion (adjustable)
        width_cm = width_px * 0.2     # 0.2 cm per pixel -> Example scale
        depth_cm = height_px * 0.08   # 0.08 cm per pixel -> Example scale

        # Minimum fallback
        if width_cm < 5:
            width_cm = 5
        if depth_cm < 3:
            depth_cm = 3

        return {
            "width_cm": width_cm,
            "depth_cm": depth_cm,
            "final_depth_m": depth_cm / 100.0
        }
