from fastapi import APIRouter
from fastapi import UploadFile, Form, Query

from fastapi.responses import StreamingResponse
import numpy as np
import cv2 as cv

from utils.detector import vehicle_detect_bytes,license_detect_bytes

router = APIRouter(
    prefix="/api/image",
)

@router.post(
    "/vehicles/detect/annotatedImage",
    tags=["Vehicles Detection"],
    response_class=StreamingResponse,
    summary="Detect Vehicles in Image",
    description="Get vehicle Image, Return Annotated Image of vehicle detection by Yolo",
)
def get_image_return_vehicle_annotatedImage(image: UploadFile,conf: float = Form(0.25),classes: list[int] = Query()):
    file = image.file.read()
    image_np = np.frombuffer(file, np.uint8)
    input_image = cv.imdecode(image_np, cv.IMREAD_COLOR) # bytes to cv image
    return_bytes = vehicle_detect_bytes(input_image,conf,classes)

    return StreamingResponse(content=return_bytes,media_type="image/jpeg")

@router.post(
    "/plates/detect/annotatedImage",
    tags=["Plates Detection"],
    response_class=StreamingResponse,
    summary="Detect license plates in Image",
    description="Get vehicle Image , Return Annotated Image of license detection by Yolo ( No cropping )",
)
def get_image_return_plate_annotatedImage(image: UploadFile,conf: float = Form(0.25)):
    file = image.file.read()
    image_np = np.frombuffer(file, np.uint8)
    input_image = cv.imdecode(image_np, cv.IMREAD_COLOR) 
    return_bytes = license_detect_bytes(input_image,conf)

    return StreamingResponse(content=return_bytes,media_type="image/jpeg")
