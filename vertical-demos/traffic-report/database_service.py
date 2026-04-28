import os
import psycopg2
from psycopg2 import sql
import pandas as pd

class DatabaseService:
    def __init__(self):
        self.db_url = os.getenv("DB_URL", "")
        self.db_user = os.getenv("DB_USER", "")
        self.db_password = os.getenv("DB_PASSWORD", "")
        self.db_name = os.getenv("DB_NAME", "")
        self.connection = None
        
    def connect(self):
        """Establish connection to PostgreSQL database"""
        try:
            # First try to connect to the specified database
            try:
                self.connection = psycopg2.connect(
                    host=self.db_url,
                    database=self.db_name,
                    user=self.db_user,
                    password=self.db_password
                )
                return True, "Connected to database successfully"
            except psycopg2.OperationalError as e:
                # If database doesn't exist, try to create it
                if "does not exist" in str(e):
                    print(f"Database {self.db_name} does not exist. Attempting to create it...")
                    return self.create_database()
                else:
                    raise e
        except Exception as e:
            print(f"Database connection error: {e}")
            return False, f"Failed to connect to database: {str(e)}"
    
    def create_database(self):
        """Create the database if it doesn't exist"""
        try:
            # Connect to default 'postgres' database to create our database
            conn = psycopg2.connect(
                host=self.db_url,
                database="postgres",  # Connect to default database
                user=self.db_user,
                password=self.db_password
            )
            conn.autocommit = True  # Required for CREATE DATABASE
            cursor = conn.cursor()
            
            # Check if database already exists
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (self.db_name,))
            exists = cursor.fetchone()
            
            if not exists:
                # Create database
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.db_name)))
                print(f"Database {self.db_name} created successfully")
            
            cursor.close()
            conn.close()
            
            # Now connect to the newly created database
            self.connection = psycopg2.connect(
                host=self.db_url,
                database=self.db_name,
                user=self.db_user,
                password=self.db_password
            )
            return True, f"Database {self.db_name} created and connected successfully"
        except Exception as e:
            print(f"Error creating database: {e}")
            return False, f"Failed to create database: {str(e)}"
    
    def validate_connection(self):
        """Check if database connection is valid"""
        if not self.db_url or not self.db_user or not self.db_password or not self.db_name:
            return False, "Database connection parameters are not configured"
        
        try:
            # Try to connect and execute a simple query
            success, msg = self.connect()
            if not success:
                return False, msg
                
            cursor = self.connection.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True, "Database connection successful"
        except Exception as e:
            return False, f"Database connection failed: {str(e)}"
    
    def create_table_if_not_exists(self):
        """Create traffic analysis table if it doesn't exist"""
        try:
            if not self.connection:
                success, msg = self.connect()
                if not success:
                    return False, msg
            
            cursor = self.connection.cursor()
            
            # Create table if it doesn't exist
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS traffic_analysis (
                    id SERIAL PRIMARY KEY,
                    video_name VARCHAR(255),
                    frame_id VARCHAR(50),
                    traffic_status VARCHAR(50),
                    traffic_flow VARCHAR(50),
                    incident_analysis TEXT,
                    safety_assessment TEXT,
                    recommendations TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            self.connection.commit()
            cursor.close()
            return True, "Table created or already exists"
        except Exception as e:
            print(f"Error creating table: {e}")
            return False, f"Failed to create table: {str(e)}"
    
    def save_analysis_results(self, video_name, analysis_results):
        """Save analysis results to database"""
        try:
            if not self.connection:
                success, msg = self.connect()
                if not success:
                    return False, msg
            
            # Create table if it doesn't exist
            table_success, table_msg = self.create_table_if_not_exists()
            if not table_success:
                return False, table_msg
            
            cursor = self.connection.cursor()
            
            # Insert records for each frame analysis
            for result in analysis_results:
                # Extract analysis data
                frame_id = f"frame_{result['frame']}"
                
                # Extract analysis content
                analysis = result.get('analysis', {})
                content = analysis.get('content', '')
                
                # Parse content to extract structured data
                traffic_status = self._extract_field(content, "Traffic Status")
                traffic_flow = self._extract_field(content, "Traffic Flow")
                incident_analysis = self._extract_field(content, "Incident Analysis")
                safety_assessment = self._extract_field(content, "Safety Assessment")
                recommendations = self._extract_field(content, "Recommendations")
                
                # Insert data
                cursor.execute("""
                    INSERT INTO traffic_analysis 
                    (video_name, frame_id, traffic_status, traffic_flow, 
                     incident_analysis, safety_assessment, recommendations)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    video_name,
                    frame_id,
                    traffic_status,
                    traffic_flow,
                    incident_analysis,
                    safety_assessment,
                    recommendations
                ))
            
            self.connection.commit()
            cursor.close()
            return True, f"Successfully saved {len(analysis_results)} analysis results to database"
        except Exception as e:
            print(f"Error saving to database: {e}")
            return False, f"Failed to save analysis results: {str(e)}"
    
    def _extract_field(self, content, field_name):
        """Extract field value from analysis content"""
        try:
            if not content:
                return ""
                
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if field_name.lower() in line.lower():
                    # If this is a header line, return the next line(s)
                    if i + 1 < len(lines):
                        # Check if next line is empty or another header
                        next_line = lines[i + 1].strip()
                        if not next_line or ":" in next_line:
                            # Extract from current line after colon
                            parts = line.split(':', 1)
                            return parts[1].strip() if len(parts) > 1 else ""
                        else:
                            # Return content until next header
                            result = []
                            j = i + 1
                            while j < len(lines) and not any(header in lines[j].lower() for header in 
                                  ["traffic status", "traffic flow", "incident analysis", 
                                   "safety assessment", "recommendations"]):
                                if lines[j].strip():
                                    result.append(lines[j].strip())
                                j += 1
                            return " ".join(result)
            
            # If field not found as header, look for it in the content
            for line in lines:
                if field_name.lower() in line.lower():
                    parts = line.split(':', 1)
                    return parts[1].strip() if len(parts) > 1 else ""
            
            return ""
        except Exception as e:
            print(f"Error extracting field {field_name}: {e}")
            return ""
    
    def get_analysis_results(self, video_name=None, limit=100):
        """Get analysis results from database"""
        try:
            if not self.connection:
                success, msg = self.connect()
                if not success:
                    return False, msg, None
            
            cursor = self.connection.cursor()
            
            if video_name:
                cursor.execute("""
                    SELECT * FROM traffic_analysis 
                    WHERE video_name = %s
                    ORDER BY id DESC
                    LIMIT %s
                """, (video_name, limit))
            else:
                cursor.execute("""
                    SELECT * FROM traffic_analysis 
                    ORDER BY id DESC
                    LIMIT %s
                """, (limit,))
            
            columns = [desc[0] for desc in cursor.description]
            results = cursor.fetchall()
            cursor.close()
            
            # Convert to pandas DataFrame for easier handling
            df = pd.DataFrame(results, columns=columns)
            return True, f"Retrieved {len(results)} records", df
        except Exception as e:
            print(f"Error retrieving from database: {e}")
            return False, f"Failed to retrieve analysis results: {str(e)}", None
    
    def close(self):
        """Close database connection"""
        if self.connection:
            self.connection.close()
            self.connection = None