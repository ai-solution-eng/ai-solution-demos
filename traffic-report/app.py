import streamlit as st
import cv2
import numpy as np
from PIL import Image
import tempfile
import os
import time
from detection_service import DetectionService
from analysis_service import AnalysisService
from database_service import DatabaseService

# Initialize services - will be done dynamically using session state
# detector = None
# analyzer = None
# db_service = None

# Global settings storage
settings = {
    "yolo_endpoint": os.getenv("YOLO_ENDPOINT", "https://your-yolo-endpoint.com/predict"),
    "yolo_api_key": os.getenv("YOLO_API_KEY", "your-yolo-api-key-here"),
    "qwen_endpoint": os.getenv("QWEN_ENDPOINT", "https://your-qwen-endpoint.com/v1/chat/completions"),
    "qwen_api_key": os.getenv("QWEN_API_KEY", "your-qwen-api-key-here"),
    "db_url": os.getenv("DB_URL", "localhost"),
    "db_user": os.getenv("DB_USER", "postgres"),
    "db_password": os.getenv("DB_PASSWORD", ""),
    "db_name": os.getenv("DB_NAME", "traffic_analysis")
}

def update_settings(yolo_endpoint, yolo_api_key, qwen_endpoint, qwen_api_key, db_url=None, db_user=None, db_password=None, db_name=None):
    """Update API settings and reinitialize services"""
    # Update settings
    settings["yolo_endpoint"] = yolo_endpoint
    settings["yolo_api_key"] = yolo_api_key
    settings["qwen_endpoint"] = qwen_endpoint
    settings["qwen_api_key"] = qwen_api_key
    
    # Update database settings if provided
    if db_url is not None:
        settings["db_url"] = db_url
    if db_user is not None:
        settings["db_user"] = db_user
    if db_password is not None:
        settings["db_password"] = db_password
    if db_name is not None:
        settings["db_name"] = db_name
    
    # Update environment variables
    os.environ["YOLO_ENDPOINT"] = yolo_endpoint.replace("/predict", "") if yolo_endpoint else ""
    os.environ["YOLO_API_KEY"] = yolo_api_key if yolo_api_key else ""
    os.environ["QWEN_ENDPOINT"] = qwen_endpoint if qwen_endpoint else ""
    os.environ["QWEN_API_KEY"] = qwen_api_key if qwen_api_key else ""
    
    # Update database environment variables
    os.environ["DB_URL"] = settings["db_url"]
    os.environ["DB_USER"] = settings["db_user"]
    os.environ["DB_PASSWORD"] = settings["db_password"]
    os.environ["DB_NAME"] = settings["db_name"]
    
    # Force reinitialize services
    if 'detector' in st.session_state:
        del st.session_state.detector
    if 'analyzer' in st.session_state:
        del st.session_state.analyzer
    if 'db_service' in st.session_state:
        del st.session_state.db_service
    
    return "Settings updated successfully! You can now use the analysis features."

def get_detector():
    """Get detector instance with current environment variables"""
    if 'detector' not in st.session_state:
        st.session_state.detector = DetectionService()
    return st.session_state.detector

def get_analyzer():
    """Get analyzer instance with current environment variables"""
    if 'analyzer' not in st.session_state:
        st.session_state.analyzer = AnalysisService()
    return st.session_state.analyzer

def get_db_service():
    """Get database service instance with current environment variables"""
    if 'db_service' not in st.session_state:
        st.session_state.db_service = DatabaseService()
    return st.session_state.db_service

def process_image(image):
    """Process single image for traffic analysis"""
    try:
        # Convert to PIL Image if needed
        if isinstance(image, np.ndarray):
            pil_image = Image.fromarray(image)
        else:
            pil_image = image
        
        # Check API connectivity first
        detector = get_detector()
        analyzer = get_analyzer()
        
        # Validate endpoints
        yolo_valid, yolo_msg = detector.validate_endpoint()
        
        # Get detections
        detections = detector.detect_objects(pil_image)
        detection_success = len(detections) > 0 or yolo_valid
        
        # Draw detections on image (or original if no detections)
        if detections:
            annotated_image = detector.draw_detections(pil_image, detections)
        else:
            annotated_image = pil_image
        
        # Get analysis
        analysis = analyzer.analyze_traffic_scene(pil_image, detections)
        
        # Format results with error information
        results_text = format_analysis_results(detections, analysis, yolo_valid, yolo_msg)
        
        return annotated_image, results_text
        
    except Exception as e:
        error_msg = f"""## Error Processing Image
        
**Error:** {str(e)}

**Troubleshooting:**
1. Check if API endpoints are reachable
2. Verify API keys are correct
3. Ensure network connectivity
4. Try with a different image

**Note:** The application will show the original image when processing fails.
        """
        return image, error_msg

def process_video(video_path, interval_seconds=3):
    """Process video for traffic analysis with frame extraction at custom intervals"""
    try:
        # Open video
        cap = cv2.VideoCapture(video_path)
        
        # Get video properties
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps
        
        # Extract frames at specified interval
        frame_interval = int(fps * interval_seconds)
        results = []
        annotated_frames = []
        
        frame_count = 0
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
                
            if frame_count % frame_interval == 0:
                # Convert BGR to RGB
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_frame = Image.fromarray(rgb_frame)
                
                # Process frame
                detections = get_detector().detect_objects(pil_frame)
                analysis = get_analyzer().analyze_traffic_scene(pil_frame, detections)
                
                # Draw detections on frame
                annotated_frame = get_detector().draw_detections(pil_frame, detections)
                
                # Store result
                results.append({
                    "frame": frame_count,
                    "timestamp": frame_count / fps,
                    "detections": detections,
                    "analysis": analysis,
                    "annotated_frame": annotated_frame
                })
                
                # Limit to reasonable number of samples
                if len(results) >= 10:  # Up to 30 seconds of analysis
                    break
                    
            frame_count += 1
        
        cap.release()
        
        # Create video output with bounding boxes
        output_frames = create_annotated_video_frames(video_path, results)
        
        # Format video results summary
        video_results = f"**Analysis Summary:** Processed {len(results)} frames at {interval_seconds}-second intervals\\n\\n"
        
        return output_frames, video_results, results
        
    except Exception as e:
        return None, f"Error processing video: {str(e)}", []

def create_annotated_video_frames(video_path, analysis_results):
    """Create a list of annotated frames from video for display"""
    try:
        # Open video
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        annotated_frames = []
        
        for result in analysis_results:
            # Seek to the specific frame
            frame_number = result['frame']
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            
            ret, frame = cap.read()
            if ret:
                # Convert BGR to RGB
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_frame = Image.fromarray(rgb_frame)
                
                # Draw detections
                detections = result['detections']
                if detections:
                    annotated_frame = get_detector().draw_detections(pil_frame, detections)
                else:
                    annotated_frame = pil_frame
                
                annotated_frames.append({
                    'frame': annotated_frame,
                    'timestamp': result['timestamp'],
                    'frame_number': frame_number
                })
        
        cap.release()
        return annotated_frames
        
    except Exception as e:
        print(f"Error creating annotated frames: {e}")
        return []

def format_analysis_results(detections, analysis, yolo_valid=True, yolo_msg=""):
    """Format analysis results for display with clean markdown"""
    result_text = ""
    
    # API Status Section
    if not yolo_valid:
        result_text += "### ⚠️ API Connection Status\n"
        result_text += f"- **YOLO Detection API:** ❌ Failed - {yolo_msg}\n"
        result_text += "- **Impact:** Unable to detect vehicles and objects in real-time\n"
        result_text += "- **Recommendation:** Check network connectivity and API configuration in Settings tab\n\n"
    
    # Detection summary - count objects by type
    result_text += "### Detected Vehicles\n"
    if detections:
        object_counts = {}
        for detection in detections:
            obj_type = detection['class'].title()
            object_counts[obj_type] = object_counts.get(obj_type, 0) + 1
        
        for obj_type, count in object_counts.items():
            result_text += f"- **{obj_type}:** {count}\n"
    else:
        if yolo_valid:
            result_text += "- No traffic objects detected in this image\n"
        else:
            result_text += "- ❌ Detection failed due to API connectivity issues\n"
            result_text += "- *Configure YOLO API in Settings and ensure network connectivity*\n"
    
    # Analysis results
    result_text += "\n### AI Report\n"
    analysis_content = analysis.get('content', 'Analysis not available')
    
    # Check if analysis failed due to API issues
    if "timeout" in str(analysis_content).lower() or "connection" in str(analysis_content).lower():
        result_text += "⚠️ **Analysis API Status:** Connection failed\n\n"
        result_text += "**Fallback Analysis:**\n"
    
    # Format the analysis content with proper markdown
    if isinstance(analysis_content, str):
        # Clean up the analysis content and ensure proper formatting
        formatted_content = analysis_content.strip()
        
        # If it doesn't already have proper markdown structure, add it
        if not formatted_content.startswith('#') and not formatted_content.startswith('-'):
            # Split by common patterns and format as bullet points
            lines = formatted_content.split('\n')
            formatted_lines = []
            for line in lines:
                line = line.strip()
                if line and not line.startswith('-') and not line.startswith('#'):
                    if any(keyword in line.lower() for keyword in ['status:', 'flow:', 'analysis:', 'assessment:', 'recommendations:']):
                        formatted_lines.append(f"- **{line}**")
                    elif line:
                        formatted_lines.append(f"- {line}")
                elif line:
                    formatted_lines.append(line)
            formatted_content = '\n'.join(formatted_lines)
        
        result_text += formatted_content
    else:
        result_text += str(analysis_content)
    
    return result_text

def format_single_frame_results(frame_result, frame_index):
    """Format analysis results for a single frame with clean markdown"""
    if not frame_result:
        return "No analysis results available"
    
    timestamp = frame_result['timestamp']
    result_text = f"## Frame {frame_index} - {timestamp:.1f}s\n\n"
    
    # Detection count
    detections = frame_result['detections']
    result_text += "### Detected Objects\n"
    if detections:
        detection_counts = {}
        for detection in detections:
            cls = detection['class']
            detection_counts[cls] = detection_counts.get(cls, 0) + 1
        
        for cls, count in detection_counts.items():
            result_text += f"- **{cls.title()}:** {count}\n"
    else:
        result_text += "- No objects detected\n"
    
    # Full analysis report
    analysis = frame_result['analysis']
    analysis_content = analysis.get('content', 'Analysis not available')
    if isinstance(analysis_content, str):
        result_text += "\n### AI Analysis Report\n"
        result_text += analysis_content
    
    return result_text

def simulate_live_stream(frames, fps=2):
    """Simulate live streaming by yielding frames with delay"""
    for frame in frames:
        yield frame
        time.sleep(1.0 / fps)  # Control playback speed

def export_to_database(video_name, analysis_results):
    """Export video analysis results to database"""
    try:
        # Get database service
        db_service = get_db_service()
        
        # Save results to database
        success, message = db_service.save_analysis_results(video_name, analysis_results)
        
        if success:
            return True, message
        else:
            return False, message
    except Exception as e:
        return False, f"Error exporting to database: {str(e)}"

# Create Streamlit interface
def create_interface():
    # Initialize session state if needed
    if 'initialized' not in st.session_state:
        st.session_state.initialized = True
    
    st.set_page_config(
        page_title="Smart Traffic Report",
        page_icon="assets/HPE-logo-2025.png",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    # Display HPE logo and title
    col1, col2 = st.columns([1, 8])
    with col1:
        st.image("assets/HPE-logo-2025.png", width=80)
    with col2:
        st.title("Smart Traffic Report")
        st.markdown("*Powered by HPE AI Solutions*")
    st.markdown("Upload an image or video to analyze traffic conditions using YOLO detection and Qwen2.5-VL analysis.")
    
    # API Status indicator
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔍 Check API Status", key="api_status_check"):
            with st.spinner("Checking API connectivity..."):
                try:
                    detector = get_detector()
                    yolo_valid, yolo_msg = detector.validate_endpoint()
                    
                    if yolo_valid:
                        st.success(f"✅ YOLO API: {yolo_msg}")
                    else:
                        st.error(f"❌ YOLO API: {yolo_msg}")
                        
                    # Basic Qwen endpoint check
                    qwen_endpoint = os.getenv("QWEN_ENDPOINT", "")
                    if qwen_endpoint:
                        st.info("🔄 Qwen API: Configured (will be tested during analysis)")
                    else:
                        st.warning("⚠️ Qwen API: Not configured")
                    
                    # Database connection check
                    db_service = get_db_service()
                    db_valid, db_msg = db_service.validate_connection()
                    
                    if db_valid:
                        st.success(f"✅ Database: {db_msg}")
                    else:
                        st.warning(f"⚠️ Database: {db_msg}")
                        
                except Exception as e:
                    st.error(f"Error checking API status: {str(e)}")
    
    with col2:
        if not os.getenv("YOLO_API_KEY") or not os.getenv("QWEN_API_KEY"):
            st.warning("⚠️ Configure API keys in Settings tab for full functionality")
    
    # Create tabs
    tab1, tab2, tab3 = st.tabs(["Image Analysis", "Video Analysis", "Settings"])
    
    with tab1:
        st.header("Image Analysis")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Upload Traffic Image")
            image_input = st.file_uploader(
                "Choose an image...",
                type=['jpg', 'jpeg', 'png'],
                key="image_upload"
            )
            
            if image_input and st.button("Analyze Image", type="primary"):
                with st.spinner("Analyzing image..."):
                    try:
                        # Convert uploaded file to PIL Image
                        pil_image = Image.open(image_input)
                        
                        # Process the image
                        annotated_image, analysis_results = process_image(pil_image)
                        
                        # Store results in session state
                        st.session_state.image_results = (annotated_image, analysis_results)
                        
                        # Show connection warnings if APIs failed
                        if "❌ Detection failed due to API connectivity issues" in analysis_results:
                            st.warning("⚠️ YOLO API connection failed. Check network connectivity and API configuration.")
                        
                        if "Connection failed" in analysis_results:
                            st.warning("⚠️ Qwen Analysis API connection failed. Using fallback analysis.")
                            
                    except Exception as e:
                        st.error(f"Error processing image: {str(e)}")
                        st.info("Please try again or check your API configuration in the Settings tab.")
        
        with col2:
            st.subheader("Results")
            
            # Display results if available
            if hasattr(st.session_state, 'image_results') and st.session_state.image_results:
                annotated_image, analysis_results = st.session_state.image_results
                
                if annotated_image:
                    st.image(annotated_image, caption="Detection Results", use_column_width=True)
                
                if analysis_results:
                    st.markdown(analysis_results)
    
    with tab2:
        st.header("Video Analysis")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Upload Traffic Video")
            video_input = st.file_uploader(
                "Choose a video...",
                type=['mp4', 'avi', 'mov', 'mkv'],
                key="video_upload"
            )
            
            # Get video duration for interval validation
            video_duration = None
            if video_input:
                # Save uploaded video to temporary file to get duration
                with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as tmp_file:
                    tmp_file.write(video_input.getvalue())
                    tmp_path = tmp_file.name
                
                try:
                    cap = cv2.VideoCapture(tmp_path)
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    video_duration = int(total_frames / fps) if fps > 0 else 30
                    cap.release()
                finally:
                    os.unlink(tmp_path)
            
            # Analysis interval input
            if video_duration:
                interval_seconds = st.number_input(
                    f"Analysis Interval (seconds)",
                    min_value=1,
                    max_value=video_duration,
                    value=3,
                    step=1,
                    help=f"Extract frames every N seconds for analysis. Video duration: {video_duration}s"
                )
            else:
                interval_seconds = st.number_input(
                    "Analysis Interval (seconds)",
                    min_value=1,
                    max_value=300,
                    value=3,
                    step=1,
                    help="Extract frames every N seconds for analysis"
                )
            
            if video_input and st.button("Analyze Video", type="primary"):
                with st.spinner("Analyzing video..."):
                    # Save uploaded video to temporary file
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as tmp_file:
                        tmp_file.write(video_input.read())
                        tmp_path = tmp_file.name
                    
                    try:
                        # Process the video
                        annotated_frames, video_results, frame_results = process_video(tmp_path)
                        
                        # Store results in session state
                        st.session_state.video_results = (annotated_frames, video_results)
                        # Also store individual frame analysis results for frame-by-frame analysis
                        st.session_state.video_analysis_results = frame_results
                        # Store video name for database export
                        st.session_state.video_name = video_input.name
                    finally:
                        # Clean up temp file
                        os.unlink(tmp_path)
            
            # Export to database button - only show if we have results
            if hasattr(st.session_state, 'video_analysis_results') and st.session_state.video_analysis_results:
                st.markdown("---")
                st.subheader("Database Export")
                
                # Optional custom video name input
                custom_video_name = st.text_input(
                    "Video Name (for database)",
                    value=getattr(st.session_state, 'video_name', "traffic_video"),
                    help="Name to identify this video in the database"
                )
                
                if st.button("💾 Export to Database", type="primary"):
                    with st.spinner("Exporting analysis results to database..."):
                        # Get database service
                        db_service = get_db_service()
                        
                        # Validate database connection
                        db_valid, db_msg = db_service.validate_connection()
                        
                        if db_valid:
                            # Export results
                            success, message = export_to_database(
                                custom_video_name, 
                                st.session_state.video_analysis_results
                            )
                            
                            if success:
                                st.success(f"✅ {message}")
                            else:
                                st.error(f"❌ {message}")
                        else:
                            st.error(f"❌ Database connection failed: {db_msg}")
                            st.info("Please configure database connection in Settings tab.")
        
        with col2:
            st.subheader("Results")
            
            # Display results if available
            if hasattr(st.session_state, 'video_results') and st.session_state.video_results:
                annotated_frames, video_results = st.session_state.video_results
                
                if annotated_frames:
                    # Live streaming simulation controls
                    col2a, col2b = st.columns([1, 1])
                    with col2a:
                        if st.button("▶️ Start Live Stream Simulation", key="start_stream"):
                            st.session_state.streaming = True
                            st.session_state.current_frame = 0
                    
                    with col2b:
                        if st.button("⏹️ Stop Stream", key="stop_stream"):
                            st.session_state.streaming = False
                    
                    # Initialize streaming state
                    if 'streaming' not in st.session_state:
                        st.session_state.streaming = False
                    if 'current_frame' not in st.session_state:
                        st.session_state.current_frame = 0
                    
                    # Live streaming display
                    if st.session_state.streaming and annotated_frames:
                        st.markdown("### <span style='font-size: 12px;'>🔴</span> Live Traffic Stream", unsafe_allow_html=True)
                        
                        # Create placeholder for streaming
                        stream_placeholder = st.empty()
                        report_placeholder = st.empty()
                        
                        # Stream frames
                        for i, frame_data in enumerate(annotated_frames):
                            if not st.session_state.streaming:
                                break
                                
                            with stream_placeholder.container():
                                st.image(
                                    frame_data['frame'], 
                                    caption=f"<span style='font-size: 12px;'>🔴</span> LIVE - Frame at {frame_data['timestamp']:.1f}s", 
                                    use_column_width=True
                                )
                            
                            # Show current frame report
                            with report_placeholder.container():
                                if hasattr(st.session_state, 'video_analysis_results'):
                                    frame_result = st.session_state.video_analysis_results[i]
                                    frame_report = format_single_frame_results(frame_result, i+1)
                                    st.markdown(frame_report)
                            
                            time.sleep(2)  # 2 second delay between frames
                            
                        st.session_state.streaming = False
                        st.success("Stream completed!")
                    
                    else:
                        # Frame selection interface
                        st.markdown("### Select Frame for Analysis")
                        
                        # Frame selector
                        frame_options = [f"Frame {i+1} ({frame['timestamp']:.1f}s)" for i, frame in enumerate(annotated_frames)]
                        selected_frame_idx = st.selectbox(
                            "Choose a frame to analyze:",
                            range(len(frame_options)),
                            format_func=lambda x: frame_options[x],
                            key="frame_selector"
                        )
                        
                        # Display selected frame
                        if selected_frame_idx is not None and selected_frame_idx < len(annotated_frames):
                            frame_data = annotated_frames[selected_frame_idx]
                            st.image(
                                frame_data['frame'], 
                                caption=f"Selected Frame at {frame_data['timestamp']:.1f}s with YOLO Detections", 
                                use_column_width=True
                            )
                            
                            # Show report for selected frame only
                            if hasattr(st.session_state, 'video_analysis_results') and selected_frame_idx < len(st.session_state.video_analysis_results):
                                frame_result = st.session_state.video_analysis_results[selected_frame_idx]
                                frame_report = format_single_frame_results(frame_result, selected_frame_idx + 1)
                                st.markdown("### Frame Analysis Report")
                                st.markdown(frame_report)
    
    with tab3:
        st.header("API Configuration")
        st.markdown("Configure your YOLO and Qwen2.5-VL model endpoints and API keys.")
        
        # Create tabs for different settings
        settings_tab1, settings_tab2 = st.tabs(["Model APIs", "Database"])
        
        with settings_tab1:
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("YOLO Detection Settings")
                yolo_endpoint_input = st.text_input(
                    "YOLO Endpoint",
                    value=settings["yolo_endpoint"],
                    placeholder="https://your-yolo-endpoint.com/predict",
                    help="Full endpoint URL including /predict"
                )
                yolo_key_input = st.text_input(
                    "YOLO API Key",
                    value=settings["yolo_api_key"],
                    type="password",
                    placeholder="your-yolo-api-key-here",
                    help="Your YOLO API authentication key"
                )
            
            with col2:
                st.subheader("Qwen2.5-VL Analysis Settings")
                qwen_endpoint_input = st.text_input(
                    "Qwen Endpoint",
                    value=settings["qwen_endpoint"],
                    placeholder="https://your-qwen-endpoint.com/v1/chat/completions",
                    help="Full endpoint URL including /v1/chat/completions"
                )
                qwen_key_input = st.text_input(
                    "Qwen API Key",
                    value=settings["qwen_api_key"],
                    type="password",
                    placeholder="your-qwen-api-key-here",
                    help="Your Qwen API authentication key"
                )
            
            if st.button("Save API Settings", type="primary"):
                result = update_settings(yolo_endpoint_input, yolo_key_input, qwen_endpoint_input, qwen_key_input)
                st.success(result)
            
            st.markdown("### Instructions:")
            st.markdown("1. **YOLO Endpoint**: Enter your YOLOv8 detection service URL ending with `/predict`")
            st.markdown("2. **YOLO API Key**: Enter your authentication key for the YOLO service")
            st.markdown("3. **Qwen Endpoint**: Enter your Qwen2.5-VL service URL ending with `/v1/chat/completions`")
            st.markdown("4. **Qwen API Key**: Enter your authentication key for the Qwen service")
            st.markdown("5. **Save API Settings**: Click to apply the new configuration")
        
        with settings_tab2:
            st.subheader("Database Settings")
            st.markdown("Configure PostgreSQL database connection for storing analysis results.")
            
            col1, col2 = st.columns(2)
            
            with col1:
                db_url_input = st.text_input(
                    "Database Host",
                    value=settings["db_url"],
                    placeholder="localhost or db.example.com",
                    help="Database server hostname or IP address"
                )
                db_name_input = st.text_input(
                    "Database Name",
                    value=settings["db_name"],
                    placeholder="traffic_analysis",
                    help="Name of the database to use"
                )
            
            with col2:
                db_user_input = st.text_input(
                    "Database User",
                    value=settings["db_user"],
                    placeholder="postgres",
                    help="Username for database authentication"
                )
                db_password_input = st.text_input(
                    "Database Password",
                    value=settings["db_password"],
                    type="password",
                    placeholder="your-db-password",
                    help="Password for database authentication"
                )
            
            if st.button("Save Database Settings", type="primary"):
                result = update_settings(
                    settings["yolo_endpoint"], 
                    settings["yolo_api_key"], 
                    settings["qwen_endpoint"], 
                    settings["qwen_api_key"],
                    db_url_input,
                    db_user_input,
                    db_password_input,
                    db_name_input
                )
                st.success("Database settings updated successfully!")
                
                # Test connection
                with st.spinner("Testing database connection..."):
                    db_service = get_db_service()
                    db_valid, db_msg = db_service.validate_connection()
                    
                    if db_valid:
                        st.success(f"✅ Database connection successful: {db_msg}")
                        
                        # Create table if it doesn't exist
                        table_valid, table_msg = db_service.create_table_if_not_exists()
                        if table_valid:
                            st.success(f"✅ {table_msg}")
                        else:
                            st.error(f"❌ {table_msg}")
                    else:
                        st.error(f"❌ Database connection failed: {db_msg}")
            
            st.markdown("### Database Schema")
            st.markdown("""
            The application will create the following table in your PostgreSQL database:
            ```sql
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
            ```
            """)
            
            st.markdown("### Instructions:")
            st.markdown("1. **Database Host**: Enter your PostgreSQL server hostname or IP address")
            st.markdown("2. **Database Name**: Enter the name of the database to use")
            st.markdown("3. **Database User**: Enter the username for authentication")
            st.markdown("4. **Database Password**: Enter the password for authentication")
            st.markdown("5. **Save Database Settings**: Click to apply and test the connection")
        
        st.info("Settings are applied immediately and will be used for all subsequent analyses. The demo includes fallback detection data when APIs are unavailable.")
        
        # Troubleshooting section
        with st.expander("🔧 Troubleshooting Connection Issues"):
            st.markdown("### Common Connection Problems:")
            st.markdown("""
            **1. Timeout Errors (Errno 110):**
            - Check if the endpoint URLs are correct and accessible
            - Verify network connectivity from your deployment environment
            - Consider if corporate firewall/proxy is blocking external APIs
            - Try increasing timeout values in the code
            
            **2. DNS Resolution Issues:**
            - Verify the hostname can be resolved in your network
            - Check if you need to configure DNS servers
            - Test connectivity with `nslookup` or `dig` commands
            
            **3. EZUA/Kubernetes Deployment Issues:**
            - Check if NetworkPolicies allow external traffic
            - Verify Istio service mesh isn't blocking requests
            - Ensure proper proxy configuration if required
            - Check pod logs for detailed error messages
            
            **4. API Authentication:**
            - Verify API keys are correct and not expired
            - Check if API endpoints require specific headers
            - Ensure the API service is running and accessible
            
            **5. Database Connection Issues:**
            - Verify PostgreSQL server is running and accessible
            - Check if database user has proper permissions
            - Ensure database name exists
            - Verify firewall allows connections to PostgreSQL port (default 5432)
            
            **Testing Commands:**
            ```bash
            # Test endpoint connectivity
            curl -v https://your-endpoint.com/health
            
            # Check DNS resolution
            nslookup your-endpoint.com
            
            # Test from pod (if in Kubernetes)
            kubectl exec -it pod-name -- curl -v https://your-endpoint.com
            
            # Test database connection
            psql -h your-db-host -U your-db-user -d your-db-name
            ```
            """)
            
            st.markdown("### Quick Fixes:")
            st.markdown("""
            1. **Use the 'Check API Status' button** above to test connectivity
            2. **Try different endpoints** if available (internal vs external)
            3. **Check deployment logs** for network-related errors
            4. **Contact your platform admin** for network policy adjustments
            5. **For database issues**, verify PostgreSQL is running and accessible
            """)
    
    # Footer
    st.markdown("---")
    st.markdown("### Instructions:")
    st.markdown("1. **Configure APIs**: Go to Settings tab to set up your model endpoints")
    st.markdown("2. **For Images**: Upload a traffic scene image to get real-time analysis")
    st.markdown("3. **For Videos**: Upload a traffic video to analyze multiple frames")
    st.markdown("4. **Results**: View detected objects and AI-generated traffic insights")
    st.markdown("5. **Database Export**: Save video analysis results to PostgreSQL database")
    
    st.markdown("*Powered by YOLOv8 for object detection and Qwen2.5-VL for scene analysis*")

# Check if API keys are set (only show warning in console for debugging)
if not os.getenv("YOLO_API_KEY") or not os.getenv("QWEN_API_KEY"):
    print("Warning: API keys not found. Please set environment variables:")
    print("- YOLO_ENDPOINT and YOLO_API_KEY")
    print("- QWEN_ENDPOINT and QWEN_API_KEY")

# Launch the Streamlit app
create_interface()