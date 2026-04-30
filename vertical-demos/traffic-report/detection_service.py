import requests
import base64
import json
import cv2
import numpy as np
from PIL import Image
import io
import os
from config import TRAFFIC_CLASSES

class DetectionService:
    def __init__(self):
        yolo_base = os.getenv("YOLO_ENDPOINT", "")
        self.endpoint = yolo_base.rstrip('/') + '/predict' if yolo_base else ""
        self.api_key = os.getenv("YOLO_API_KEY", "")
        
    def validate_endpoint(self):
        """Check if the endpoint is reachable"""
        if not self.endpoint:
            return False, "No YOLO endpoint configured"
        
        try:
            # Try a simple HEAD request to check connectivity
            response = requests.head(self.endpoint.replace('/predict', ''), timeout=5)
            return True, "Endpoint reachable"
        except requests.exceptions.Timeout:
            return False, "Endpoint timeout - service may be slow or unreachable"
        except requests.exceptions.ConnectionError:
            return False, "Connection failed - check if endpoint URL is correct"
        except Exception as e:
            return False, f"Endpoint validation failed: {str(e)}"
        
    def encode_image(self, image):
        """Convert image to base64 string"""
        if isinstance(image, str):
            # If it's a file path
            with open(image, "rb") as img_file:
                return base64.b64encode(img_file.read()).decode('utf-8')
        elif isinstance(image, np.ndarray):
            # If it's a numpy array (OpenCV image)
            _, buffer = cv2.imencode('.jpg', image)
            return base64.b64encode(buffer).decode('utf-8')
        elif isinstance(image, Image.Image):
            # If it's a PIL Image
            buffered = io.BytesIO()
            image.save(buffered, format="JPEG")
            return base64.b64encode(buffered.getvalue()).decode('utf-8')
        
    def detect_objects(self, image):
        """Send image to YOLO endpoint and get detections"""
        try:
            # Get image dimensions
            if isinstance(image, str):
                with open(image, "rb") as f:
                    img_bytes = f.read()
                # Load image to get dimensions
                temp_img = Image.open(image)
                img_width, img_height = temp_img.size
            elif isinstance(image, np.ndarray):
                img_height, img_width = image.shape[:2]
                _, buffer = cv2.imencode('.jpg', image)
                img_bytes = buffer.tobytes()
            elif isinstance(image, Image.Image):
                img_width, img_height = image.size
                buffered = io.BytesIO()
                # Convert RGBA to RGB if needed
                if image.mode in ('RGBA', 'LA', 'P'):
                    image = image.convert('RGB')
                image.save(buffered, format="JPEG")
                img_bytes = buffered.getvalue()
            else:
                raise ValueError("Unsupported image format")
            
            # Prepare request with multipart/form-data
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }
            
            files = {
                "images": ("image.jpg", img_bytes, "image/jpeg")
            }
            
            # Make request to /predict endpoint with timeout and retry
            timeout = (10, 30)  # (connection timeout, read timeout)
            max_retries = 3
            
            for attempt in range(max_retries):
                try:
                    response = requests.post(
                        self.endpoint, 
                        headers=headers, 
                        files=files,
                        timeout=timeout
                    )
                    response.raise_for_status()
                    break  # Success, exit retry loop
                except requests.exceptions.Timeout:
                    print(f"Timeout on attempt {attempt + 1}/{max_retries}")
                    if attempt == max_retries - 1:
                        raise
                except requests.exceptions.ConnectionError:
                    print(f"Connection error on attempt {attempt + 1}/{max_retries}")
                    if attempt == max_retries - 1:
                        raise
                except requests.exceptions.RequestException as e:
                    print(f"Request error on attempt {attempt + 1}/{max_retries}: {e}")
                    if attempt == max_retries - 1:
                        raise
            
            # Parse response
            result = response.json()
            return self.parse_detections(result)
            
        except requests.exceptions.Timeout as e:
            print(f"Detection API timeout error: {e}")
            print(f"YOLO Endpoint: {self.endpoint}")
            print(f"API Key configured: {'Yes' if self.api_key else 'No'}")
            print("Suggestion: Check if the endpoint is reachable and try increasing timeout values")
            print("Returning empty detections - API call timed out")
            return []
        except requests.exceptions.ConnectionError as e:
            print(f"Detection API connection error: {e}")
            print(f"YOLO Endpoint: {self.endpoint}")
            print(f"API Key configured: {'Yes' if self.api_key else 'No'}")
            print("Suggestion: Verify the endpoint URL is correct and the service is running")
            print("Returning empty detections - API connection failed")
            return []
        except requests.exceptions.HTTPError as e:
            print(f"Detection API HTTP error: {e}")
            print(f"YOLO Endpoint: {self.endpoint}")
            print(f"API Key configured: {'Yes' if self.api_key else 'No'}")
            print("Suggestion: Check API key validity and endpoint authentication")
            print("Returning empty detections - API HTTP error")
            return []
        except Exception as e:
            print(f"Detection API unexpected error: {e}")
            print(f"YOLO Endpoint: {self.endpoint}")
            print(f"API Key configured: {'Yes' if self.api_key else 'No'}")
            print("Returning empty detections - API call failed")
            return []
    
    
    def parse_detections(self, result):
        """Parse YOLO response and filter traffic-related objects"""
        detections = []
        
        # Handle different YOLO API response formats
        if isinstance(result, dict):
            # Standard YOLO API format: {"predictions": [...]}
            if "predictions" in result:
                predictions = result["predictions"]
            elif "results" in result:
                predictions = result["results"]
            elif "detections" in result:
                predictions = result["detections"]
            else:
                predictions = []
        elif isinstance(result, list):
            # Direct array format
            predictions = result
        else:
            predictions = []
        
        # Process predictions
        for prediction in predictions:
            if isinstance(prediction, list):
                # BentoML nested format
                for detection in prediction:
                    detections.extend(self._parse_single_detection(detection))
            else:
                # Direct detection format
                detections.extend(self._parse_single_detection(prediction))
        
        return detections
    
    def _parse_single_detection(self, detection):
        """Parse a single detection object"""
        if not isinstance(detection, dict):
            return []
        
        # Extract class ID
        class_id = detection.get("class", detection.get("class_id", detection.get("label", -1)))
        
        # Extract confidence
        confidence = detection.get("confidence", detection.get("score", 0.0))
        
        # Extract bounding box - handle multiple formats
        bbox = []
        if "box" in detection:
            box = detection["box"]
            if isinstance(box, dict):
                # Format: {"x1": ..., "y1": ..., "x2": ..., "y2": ...}
                if all(k in box for k in ['x1', 'y1', 'x2', 'y2']):
                    bbox = [box['x1'], box['y1'], box['x2'], box['y2']]
                # Format: {"x": ..., "y": ..., "w": ..., "h": ...}
                elif all(k in box for k in ['x', 'y', 'w', 'h']):
                    x, y, w, h = box['x'], box['y'], box['w'], box['h']
                    bbox = [x, y, x + w, y + h]
            elif isinstance(box, list) and len(box) == 4:
                bbox = box
        elif "bbox" in detection:
            bbox = detection["bbox"]
        elif "bounding_box" in detection:
            bbox = detection["bounding_box"]
        
        # Filter for traffic-related classes and valid detections
        if (class_id in TRAFFIC_CLASSES and 
            confidence > 0.3 and 
            len(bbox) == 4 and 
            all(isinstance(coord, (int, float)) for coord in bbox)):
            
            return [{
                "class": TRAFFIC_CLASSES[class_id],
                "confidence": confidence,
                "bbox": bbox
            }]
        
        return []
    
    def draw_detections(self, image, detections):
        """Draw bounding boxes on image"""
        # Convert input image to PIL for consistent handling
        if isinstance(image, str):
            pil_image = Image.open(image)
        elif isinstance(image, np.ndarray):
            pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        elif isinstance(image, Image.Image):
            pil_image = image.copy()
        else:
            raise ValueError("Unsupported image format")
        
        # Convert PIL to OpenCV for drawing
        img = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
        img_height, img_width = img.shape[:2]
        
        # Calculate scaling factors based on image size for consistent appearance
        # Base these on a reference size (e.g., 1000x1000)
        base_size = 1000
        scale_factor = min(img_width, img_height) / base_size
        
        # Scale line thickness and font parameters based on image size
        LINE_THICKNESS = max(1, int(3 * scale_factor))
        FONT_SCALE = max(0.3, 0.6 * scale_factor)
        FONT_THICKNESS = max(1, int(2 * scale_factor))
        
        for detection in detections:
            bbox = detection["bbox"]
            if len(bbox) == 4:
                x1, y1, x2, y2 = bbox
                
                # Clip coordinates to image bounds
                x1 = max(0, min(int(x1), img_width - 1))
                y1 = max(0, min(int(y1), img_height - 1))
                x2 = max(0, min(int(x2), img_width - 1))
                y2 = max(0, min(int(y2), img_height - 1))
                
                # Ensure x2 > x1 and y2 > y1
                if x2 <= x1 or y2 <= y1:
                    continue
                
                # Draw rectangle with constant line thickness
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), LINE_THICKNESS)
                
                # Draw label with constant font size
                label = f"{detection['class']}: {detection['confidence']:.2f}"
                label_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, FONT_SCALE, FONT_THICKNESS)[0]
                
                # Draw background rectangle for label
                cv2.rectangle(img, (x1, y1 - label_size[1] - 10), 
                             (x1 + label_size[0], y1), (0, 255, 0), -1)
                
                # Draw label text with constant font parameters
                cv2.putText(img, label, (x1, y1 - 5), 
                           cv2.FONT_HERSHEY_SIMPLEX, FONT_SCALE, (0, 0, 0), FONT_THICKNESS)
        
        # Convert back to PIL Image for consistent return type
        return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))