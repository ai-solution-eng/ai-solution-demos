import streamlit as st
import cv2
import numpy as np
from PIL import Image
import tempfile
import os
from detection_service import DetectionService
from analysis_service import AnalysisService

# Initialize services - will be done dynamically using session state
# detector = None
# analyzer = None

# Global settings storage
settings = {
    "yolo_endpoint": os.getenv("YOLO_ENDPOINT", "https://your-yolo-endpoint.com/predict"),
    "yolo_api_key": os.getenv("YOLO_API_KEY", "your-yolo-api-key-here"),
    "qwen_endpoint": os.getenv("QWEN_ENDPOINT", "https://your-qwen-endpoint.com/v1/chat/completions"),
    "qwen_api_key": os.getenv("QWEN_API_KEY", "your-qwen-api-key-here")
}

def update_settings(yolo_endpoint, yolo_api_key, qwen_endpoint, qwen_api_key):
    """Update API settings and reinitialize services"""
    # Update settings
    settings["yolo_endpoint"] = yolo_endpoint
    settings["yolo_api_key"] = yolo_api_key
    settings["qwen_endpoint"] = qwen_endpoint
    settings["qwen_api_key"] = qwen_api_key
    
    # Update environment variables
    os.environ["YOLO_ENDPOINT"] = yolo_endpoint.replace("/predict", "") if yolo_endpoint else ""
    os.environ["YOLO_API_KEY"] = yolo_api_key if yolo_api_key else ""
    os.environ["QWEN_ENDPOINT"] = qwen_endpoint if qwen_endpoint else ""
    os.environ["QWEN_API_KEY"] = qwen_api_key if qwen_api_key else ""
    
    # Force reinitialize services
    if 'detector' in st.session_state:
        del st.session_state.detector
    if 'analyzer' in st.session_state:
        del st.session_state.analyzer
    
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

def process_video(video_path):
    """Process video for traffic analysis (sample frames)"""
    try:
        # Open video
        cap = cv2.VideoCapture(video_path)
        
        # Get video properties
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        # Sample frames (every 30 frames or 1 second)
        sample_interval = max(1, int(fps))
        results = []
        
        frame_count = 0
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
                
            if frame_count % sample_interval == 0:
                # Convert BGR to RGB
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_frame = Image.fromarray(rgb_frame)
                
                # Process frame
                detections = get_detector().detect_objects(pil_frame)
                analysis = get_analyzer().analyze_traffic_scene(pil_frame, detections)
                
                # Store result
                results.append({
                    "frame": frame_count,
                    "timestamp": frame_count / fps,
                    "detections": detections,
                    "analysis": analysis
                })
                
                # Limit to 5 samples for demo
                if len(results) >= 5:
                    break
                    
            frame_count += 1
        
        cap.release()
        
        # Get a representative frame for display
        cap = cv2.VideoCapture(video_path)
        cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames // 2)  # Middle frame
        ret, frame = cap.read()
        cap.release()
        
        if ret:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_frame = Image.fromarray(rgb_frame)
            detections = get_detector().detect_objects(pil_frame)
            annotated_frame = get_detector().draw_detections(pil_frame, detections)
        else:
            annotated_frame = None
        
        # Format video results
        video_results = format_video_results(results)
        
        return annotated_frame, video_results
        
    except Exception as e:
        return None, f"Error processing video: {str(e)}"

def format_analysis_results(detections, analysis, yolo_valid=True, yolo_msg=""):
    """Format analysis results for display"""
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

def format_video_results(results):
    """Format video analysis results"""
    if not results:
        return "No analysis results available"
    
    result_text = "### Video Analysis Results\n\n"
    
    for i, result in enumerate(results, 1):
        result_text += f"**Frame {i}** *(t={result['timestamp']:.1f}s)*\n"
        
        # Detection count
        detections = result['detections']
        if detections:
            detection_counts = {}
            for detection in detections:
                cls = detection['class']
                detection_counts[cls] = detection_counts.get(cls, 0) + 1
            
            for cls, count in detection_counts.items():
                result_text += f"- **{cls.title()}:** {count}\n"
        else:
            result_text += "- No objects detected\n"
        
        # Analysis
        analysis = result['analysis']
        result_text += f"- **Traffic Flow:** {analysis.get('traffic_flow', 'Unknown')}\n"
        result_text += f"- **Safety Status:** {analysis.get('safety_concerns', 'OK')}\n\n"
    
    return result_text

# Create Streamlit interface
def create_interface():
    # Initialize session state if needed
    if 'initialized' not in st.session_state:
        st.session_state.initialized = True
    
    st.set_page_config(
        page_title="Smart Traffic Report",
        page_icon="🚦",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    st.title("🚦 Smart Traffic Report")
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
            
            if video_input and st.button("Analyze Video", type="primary"):
                with st.spinner("Analyzing video..."):
                    # Save uploaded video to temporary file
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as tmp_file:
                        tmp_file.write(video_input.read())
                        tmp_path = tmp_file.name
                    
                    try:
                        # Process the video
                        annotated_frame, video_results = process_video(tmp_path)
                        
                        # Store results in session state
                        st.session_state.video_results = (annotated_frame, video_results)
                    finally:
                        # Clean up temp file
                        os.unlink(tmp_path)
        
        with col2:
            st.subheader("Results")
            
            # Display results if available
            if hasattr(st.session_state, 'video_results') and st.session_state.video_results:
                annotated_frame, video_results = st.session_state.video_results
                
                if annotated_frame:
                    st.image(annotated_frame, caption="Sample Frame with Detections", use_column_width=True)
                
                if video_results:
                    st.markdown(video_results)
    
    with tab3:
        st.header("API Configuration")
        st.markdown("Configure your YOLO and Qwen2.5-VL model endpoints and API keys.")
        
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
        
        if st.button("Save Settings", type="primary"):
            result = update_settings(yolo_endpoint_input, yolo_key_input, qwen_endpoint_input, qwen_key_input)
            st.success(result)
        
        st.markdown("### Instructions:")
        st.markdown("1. **YOLO Endpoint**: Enter your YOLOv8 detection service URL ending with `/predict`")
        st.markdown("2. **YOLO API Key**: Enter your authentication key for the YOLO service")
        st.markdown("3. **Qwen Endpoint**: Enter your Qwen2.5-VL service URL ending with `/v1/chat/completions`")
        st.markdown("4. **Qwen API Key**: Enter your authentication key for the Qwen service")
        st.markdown("5. **Save Settings**: Click to apply the new configuration")
        
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
            
            **Testing Commands:**
            ```bash
            # Test endpoint connectivity
            curl -v https://your-endpoint.com/health
            
            # Check DNS resolution
            nslookup your-endpoint.com
            
            # Test from pod (if in Kubernetes)
            kubectl exec -it pod-name -- curl -v https://your-endpoint.com
            ```
            """)
            
            st.markdown("### Quick Fixes:")
            st.markdown("""
            1. **Use the 'Check API Status' button** above to test connectivity
            2. **Try different endpoints** if available (internal vs external)
            3. **Check deployment logs** for network-related errors
            4. **Contact your platform admin** for network policy adjustments
            """)
    
    # Footer
    st.markdown("---")
    st.markdown("### Instructions:")
    st.markdown("1. **Configure APIs**: Go to Settings tab to set up your model endpoints")
    st.markdown("2. **For Images**: Upload a traffic scene image to get real-time analysis")
    st.markdown("3. **For Videos**: Upload a traffic video to analyze multiple frames")
    st.markdown("4. **Results**: View detected objects and AI-generated traffic insights")
    
    st.markdown("*Powered by YOLOv8 for object detection and Qwen2.5-VL for scene analysis*")

# Check if API keys are set (only show warning in console for debugging)
if not os.getenv("YOLO_API_KEY") or not os.getenv("QWEN_API_KEY"):
    print("Warning: API keys not found. Please set environment variables:")
    print("- YOLO_ENDPOINT and YOLO_API_KEY")
    print("- QWEN_ENDPOINT and QWEN_API_KEY")

# Launch the Streamlit app
create_interface()