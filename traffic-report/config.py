import os

# API Configuration
YOLO_ENDPOINT = os.getenv("YOLO_ENDPOINT", "").rstrip('/') + '/predict'
YOLO_API_KEY = os.getenv("YOLO_API_KEY", "")

QWEN_ENDPOINT = os.getenv("QWEN_ENDPOINT", "")
QWEN_API_KEY = os.getenv("QWEN_API_KEY", "")

# Detection Classes
TRAFFIC_CLASSES = {
    0: "person",
    1: "bicycle", 
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck"
}

# Analysis Prompts
TRAFFIC_ANALYSIS_PROMPT = """
Analyze this traffic scene image with the following detected objects: {detections}

Please provide a concise analysis in this format:

**Traffic Status:** [NORMAL/ABNORMAL]
**Traffic Flow:** [Light/Moderate/Heavy]
**Incident Analysis:** [If abnormal, describe any accidents, delays, or unusual situations and specify how many objects/vehicles are involved]
**Safety Assessment:** [Any safety concerns or violations observed]
**Recommendations:** [Brief optimization suggestions]

Keep each section to 1-2 sentences maximum.
"""