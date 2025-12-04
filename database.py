import psycopg2
import os
import threading
from dotenv import load_dotenv
load_dotenv()

class Database:
    def __init__(self):
        self.db_name = os.getenv("DB_NAME")
        self.db_user = os.getenv("DB_USER")
        self.db_password = os.getenv("DB_PASSWORD")
        self.db_host = os.getenv("DB_HOST", "localhost")
        self.db_port = os.getenv("DB_PORT", "5432")

        self.thread_local = threading.local()

        # Debug print
        print("✅ DB CONFIGURATION")
        print("DB:", self.db_name, self.db_user, self.db_host, self.db_port)

    def get_connection(self):
        if not hasattr(self.thread_local, 'connection'):
            self.thread_local.connection = psycopg2.connect(
                dbname=self.db_name,
                user=self.db_user,
                password=self.db_password,
                host=self.db_host,
                port=self.db_port
            )
            self.create_tables()
        return self.thread_local.connection

    def connect(self):
        return self.get_connection()

    def create_tables(self):
        conn = self.get_connection()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS locations (
                location_id SERIAL PRIMARY KEY,
                location_name TEXT NOT NULL,
                latitude FLOAT,
                longitude FLOAT,
                city TEXT,
                additional_notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS media_files (
                media_id SERIAL PRIMARY KEY,
                original_filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                original_file_url TEXT,
                processed_file_url TEXT,
                file_size INTEGER,
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS pothole_analysis (
                analysis_id SERIAL PRIMARY KEY,
                location_id INTEGER REFERENCES locations(location_id),
                media_id INTEGER REFERENCES media_files(media_id),
                total_potholes INTEGER NOT NULL,
                total_volume_liters FLOAT,
                average_width_cm FLOAT,
                average_depth_cm FLOAT,
                analysis_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS pothole_details (
                pothole_detail_id SERIAL PRIMARY KEY,
                analysis_id INTEGER REFERENCES pothole_analysis(analysis_id),
                pothole_number INTEGER,
                width_cm FLOAT,
                depth_cm FLOAT,
                volume_liters FLOAT,
                confidence_score FLOAT,
                bounding_box TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS cost_analysis (
                cost_id SERIAL PRIMARY KEY,
                analysis_id INTEGER REFERENCES pothole_analysis(analysis_id),
                material_cost FLOAT,
                labor_cost FLOAT,
                equipment_cost FLOAT,
                transport_cost FLOAT,
                overhead_cost FLOAT,
                total_cost FLOAT,
                cost_parameters TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS time_estimation (
                time_id SERIAL PRIMARY KEY,
                analysis_id INTEGER REFERENCES pothole_analysis(analysis_id),
                total_hours FLOAT,
                setup_time FLOAT,
                prep_time FLOAT,
                fill_time FLOAT,
                compact_time FLOAT,
                cleanup_time FLOAT
            )
        """)

        conn.commit()
        print("✅ PostgreSQL tables created")

    def get_cursor(self):
        return self.get_connection().cursor()

    def close(self):
        if hasattr(self.thread_local, 'connection'):
            self.thread_local.connection.close()
            del self.thread_local.connection
            print("✅ PostgreSQL connection closed")

db = Database()
