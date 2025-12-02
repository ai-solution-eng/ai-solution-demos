import os
import time
from PIL import Image
import numpy as np
from database_service import DatabaseService

def generate_mock_detections():
    """Generate mock detection results similar to what YOLO would produce"""
    vehicle_classes = ["car", "truck", "bus", "motorcycle"]
    detections = []
    
    # Generate random number of vehicles (3-10)
    num_vehicles = np.random.randint(3, 11)
    
    for i in range(num_vehicles):
        # Random vehicle class
        vehicle_class = np.random.choice(vehicle_classes)
        
        # Random bounding box (x1, y1, x2, y2) in 1080p resolution
        x1 = np.random.randint(0, 1800)
        y1 = np.random.randint(0, 900)
        w = np.random.randint(100, 400)
        h = np.random.randint(100, 300)
        x2 = min(x1 + w, 1920)
        y2 = min(y1 + h, 1080)
        
        # Random confidence score
        confidence = np.random.uniform(0.5, 0.99)
        
        detections.append({
            "class": vehicle_class,
            "confidence": confidence,
            "bbox": [x1, y1, x2, y2]
        })
    
    return detections

def generate_mock_analysis(detections):
    """Generate mock analysis results similar to what Qwen VLM would produce"""
    # Count vehicles by type
    vehicle_counts = {}
    for detection in detections:
        vehicle_type = detection["class"]
        vehicle_counts[vehicle_type] = vehicle_counts.get(vehicle_type, 0) + 1
    
    total_vehicles = sum(vehicle_counts.values())
    
    # Determine traffic status based on vehicle count
    if total_vehicles < 5:
        traffic_status = "NORMAL"
        traffic_flow = "Light"
        incident_analysis = f"Normal traffic flow with {total_vehicles} vehicles present."
        safety_assessment = "No immediate safety concerns detected."
        recommendations = "Maintain current traffic management."
    elif total_vehicles < 10:
        traffic_status = "NORMAL"
        traffic_flow = "Moderate"
        incident_analysis = f"Moderate traffic density with {total_vehicles} vehicles."
        safety_assessment = "Monitor for potential congestion."
        recommendations = "Prepare for possible traffic management if volume increases."
    else:
        traffic_status = "ABNORMAL"
        traffic_flow = "Heavy"
        incident_analysis = f"Heavy traffic congestion with {total_vehicles} vehicles present."
        safety_assessment = "Monitor closely for developing incidents."
        recommendations = "Implement traffic flow optimization measures."
    
    # Format the analysis content
    analysis_content = f"""
## Traffic Analysis Report

**Traffic Status:** {traffic_status}

**Traffic Flow:** {traffic_flow}

**Incident Analysis:** {incident_analysis}

**Safety Assessment:** {safety_assessment}

**Recommendations:** {recommendations}
"""
    
    return {"content": analysis_content}

def generate_mock_frame_results(num_frames=5):
    """Generate mock frame analysis results for testing database export"""
    results = []
    
    for i in range(num_frames):
        # Generate random frame number and timestamp
        frame_num = i * 30  # Simulate frames at 1-second intervals (30fps)
        timestamp = frame_num / 30.0
        
        # Generate mock detections for this frame
        detections = generate_mock_detections()
        
        # Generate mock analysis for this frame
        analysis = generate_mock_analysis(detections)
        
        # Create a mock annotated frame (not actually used for database export)
        mock_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        mock_frame[:] = (np.random.randint(100, 200), np.random.randint(100, 200), np.random.randint(100, 200))
        
        # Store result
        results.append({
            "frame": frame_num,
            "timestamp": timestamp,
            "detections": detections,
            "analysis": analysis,
            "annotated_frame": Image.fromarray(mock_frame)
        })
    
    return results

def test_database_export():
    """Test the database export functionality with mock data"""
    print("Starting database export test...")
    
    # 1. Set environment variables for database connection
    # You can modify these values as needed
    os.environ["DB_URL"] = "localhost"
    os.environ["DB_USER"] = "postgres"
    os.environ["DB_PASSWORD"] = "admin"
    os.environ["DB_NAME"] = "traffic_analysis_test"
    
    # 2. Generate mock video analysis results
    print("Generating mock video analysis results...")
    mock_results = generate_mock_frame_results(5)
    
    # 3. Initialize database service
    print("Initializing database service...")
    db_service = DatabaseService()
    
    # 4. Test database connection
    print("Testing database connection...")
    db_valid, db_msg = db_service.validate_connection()
    print(f"Database connection: {db_valid}, Message: {db_msg}")
    
    if not db_valid:
        print("Database connection failed. Exiting test.")
        return
    
    # 5. Create table if it doesn't exist
    print("Creating table if it doesn't exist...")
    table_valid, table_msg = db_service.create_table_if_not_exists()
    print(f"Table creation: {table_valid}, Message: {table_msg}")
    
    if not table_valid:
        print("Table creation failed. Exiting test.")
        return
    
    # 6. Export mock results to database
    print("Exporting mock results to database...")
    video_name = f"test_video_{int(time.time())}"
    success, message = db_service.save_analysis_results(video_name, mock_results)
    
    print(f"Export result: {success}, Message: {message}")
    
    # 7. Retrieve the results to verify
    if success:
        print("Retrieving saved results from database...")
        get_success, get_message, df = db_service.get_analysis_results(video_name)
        
        if get_success:
            print(f"Retrieved {len(df)} records:")
            print(df[['video_name', 'frame_id', 'traffic_status', 'traffic_flow']].head())
        else:
            print(f"Failed to retrieve results: {get_message}")
    
    # 8. Close database connection
    db_service.close()
    print("Database test completed.")

if __name__ == "__main__":
    test_database_export()