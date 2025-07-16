import requests
import base64
import json
import cv2
import numpy as np
from PIL import Image
import io
import os
from config import TRAFFIC_ANALYSIS_PROMPT

class AnalysisService:
    def __init__(self):
        self.endpoint = os.getenv("QWEN_ENDPOINT", "")
        self.api_key = os.getenv("QWEN_API_KEY", "")
        
    def encode_image(self, image):
        """Convert image to base64 string"""
        if isinstance(image, str):
            with open(image, "rb") as img_file:
                return base64.b64encode(img_file.read()).decode('utf-8')
        elif isinstance(image, np.ndarray):
            _, buffer = cv2.imencode('.jpg', image)
            return base64.b64encode(buffer).decode('utf-8')
        elif isinstance(image, Image.Image):
            buffered = io.BytesIO()
            # Convert RGBA to RGB if needed
            if image.mode in ('RGBA', 'LA', 'P'):
                image = image.convert('RGB')
            image.save(buffered, format="JPEG")
            return base64.b64encode(buffered.getvalue()).decode('utf-8')
    
    def analyze_traffic_scene(self, image, detections):
        """Analyze traffic scene using Qwen2.5-VL"""
        try:
            # Encode image
            img_b64 = self.encode_image(image)
            
            # Format detection information
            detection_summary = self.format_detections(detections)
            prompt = TRAFFIC_ANALYSIS_PROMPT.format(detections=detection_summary)
            
            # Prepare request
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "Qwen/Qwen2.5-VL-7B-Instruct",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{img_b64}"
                                }
                            }
                        ]
                    }
                ],
                "max_tokens": 500,
                "temperature": 0.7
            }
            
            # Make request with timeout and retry
            timeout = (10, 60)  # (connection timeout, read timeout) - longer for analysis
            max_retries = 2
            
            for attempt in range(max_retries):
                try:
                    response = requests.post(
                        self.endpoint, 
                        headers=headers, 
                        json=payload,
                        timeout=timeout
                    )
                    response.raise_for_status()
                    break  # Success, exit retry loop
                except requests.exceptions.Timeout:
                    print(f"Analysis timeout on attempt {attempt + 1}/{max_retries}")
                    if attempt == max_retries - 1:
                        raise
                except requests.exceptions.ConnectionError:
                    print(f"Analysis connection error on attempt {attempt + 1}/{max_retries}")
                    if attempt == max_retries - 1:
                        raise
                except requests.exceptions.RequestException as e:
                    print(f"Analysis request error on attempt {attempt + 1}/{max_retries}: {e}")
                    if attempt == max_retries - 1:
                        raise
            
            # Parse response
            result = response.json()
            return self.parse_analysis(result)
            
        except Exception as e:
            print(f"Analysis error: {e}")
            # Provide intelligent fallback based on detection counts
            return self.generate_fallback_analysis(detections)
    
    def format_detections(self, detections):
        """Format detections for analysis prompt"""
        if not detections:
            return "No objects detected"
        
        # Count objects by type
        object_counts = {}
        for detection in detections:
            obj_type = detection['class']
            object_counts[obj_type] = object_counts.get(obj_type, 0) + 1
        
        summary = []
        for obj_type, count in object_counts.items():
            summary.append(f"- {obj_type.title()}: {count}")
        
        return "\n".join(summary)
    
    def parse_analysis(self, result):
        """Parse Qwen2.5-VL response into structured format"""
        try:
            # Extract content from response
            if "choices" in result and len(result["choices"]) > 0:
                content = result["choices"][0]["message"]["content"]
            else:
                content = str(result)
            
            # Return the content directly for display
            return {
                "content": content
            }
            
        except Exception as e:
            print(f"Parse error: {e}")
            return self.generate_fallback_analysis([])
    
    def generate_fallback_analysis(self, detections):
        """Generate intelligent fallback analysis based on detection counts"""
        if not detections:
            return {
                "content": "**Traffic Status:** UNKNOWN\n**Traffic Flow:** Unable to determine\n**Incident Analysis:** No objects detected for analysis\n**Safety Assessment:** Cannot assess without detection data\n**Recommendations:** Ensure proper camera positioning and lighting"
            }
        
        # Count objects by type
        object_counts = {}
        total_vehicles = 0
        
        for detection in detections:
            obj_type = detection['class']
            object_counts[obj_type] = object_counts.get(obj_type, 0) + 1
            if obj_type in ['car', 'truck', 'bus', 'motorcycle']:
                total_vehicles += 1
        
        # Generate analysis based on vehicle counts
        if total_vehicles == 0:
            traffic_status = "NORMAL"
            traffic_flow = "Light"
            incident_analysis = "No vehicles detected in the scene"
            safety_assessment = "Scene appears clear"
            recommendations = "Continue monitoring"
        
        elif total_vehicles <= 5:
            traffic_status = "NORMAL"
            traffic_flow = "Light"
            incident_analysis = f"Normal traffic flow with {total_vehicles} vehicles present"
            safety_assessment = "No immediate safety concerns detected"
            recommendations = "Maintain current traffic management"
        
        elif total_vehicles <= 15:
            traffic_status = "NORMAL"
            traffic_flow = "Moderate"
            incident_analysis = f"Moderate traffic density with {total_vehicles} vehicles"
            safety_assessment = "Monitor for potential congestion"
            recommendations = "Prepare for possible traffic management if volume increases"
        
        else:
            # High vehicle count suggests abnormal situation
            traffic_status = "ABNORMAL"
            traffic_flow = "Heavy"
            
            # Check for potential accident indicators
            cars = object_counts.get('car', 0)
            trucks = object_counts.get('truck', 0)
            
            if trucks > 5 and cars > 10:
                incident_analysis = f"High vehicle concentration detected: {cars} cars and {trucks} trucks. Potential multi-vehicle incident involving {min(8, total_vehicles//3)} vehicles causing traffic backup."
                safety_assessment = "Critical situation requiring immediate attention. Emergency response may be needed."
                recommendations = "Deploy traffic control personnel, assess for emergency services, establish traffic diversions"
            else:
                incident_analysis = f"Heavy traffic congestion with {total_vehicles} vehicles present"
                safety_assessment = "Monitor closely for developing incidents"
                recommendations = "Implement traffic flow optimization measures"
        
        return {
            "content": f"""
## Traffic Analysis Report

### **Traffic Status:** {traffic_status}

### **Traffic Flow:** {traffic_flow}

### **Incident Analysis:**
{incident_analysis}

### **Safety Assessment:**
{safety_assessment}

### **Recommendations:**
{recommendations}
"""
        }