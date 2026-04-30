from fastapi import APIRouter
from pydantic import BaseModel,Field
from typing import Dict
import yaml
from utils.detector import get_model_names

class ModelInfo(BaseModel):
    VEHICLE_MODEL : str = Field(..., description="Vehicle detection Model")
    LICENSE_MODEL : str = Field(..., description="License Plate detection Model")
    VEHICLE_MODEL_NAME : Dict = Field(..., description="Vehicle detection Model")
    LICENSE_MODEL_NAME : Dict = Field(..., description="License Plate detection Model")
    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "VEHICLE_MODEL": "./models/yolo11s.pt",
                    "LICENSE_MODEL": "./models/yolo11s_20epochs_best.pt",
                    "VEHICLE_MODEL_NAME": "model object names",
                    "LICENSE_MODEL_NAME": "model object names"
                },
                {
                    "VEHICLE_MODEL": "https://[kserve isvc url]/vehicle_detector",
                    "LICENSE_MODEL": "https://[kserve isvc url]/license_detector",
                    "VEHICLE_MODEL_NAME": "model object names",
                    "LICENSE_MODEL_NAME": "model object names"
                },
            ]
        }

router = APIRouter(
    prefix="/api/utils",
)

@router.get(
    "/models/info",
    tags=["Utils"],
    response_model=ModelInfo,
    summary="Return Model info",
    description="Return Model info from config.yaml"
)
def return_model_info():
    with open("./config.yaml", "r") as f:
        config = yaml.safe_load(f)

    MODEL_ENDPOINT = config["detectors"]["server_url"]
    VEHICLE_MODEL = config["detectors"]["vehicle_detector"]
    LICENSE_MODEL = config["detectors"]["license_detector"]
    VEHICLE_MODEL_NAME, LICENSE_MODEL_NAME = get_model_names()
    return ModelInfo(VEHICLE_MODEL=MODEL_ENDPOINT + '/' + VEHICLE_MODEL, LICENSE_MODEL=MODEL_ENDPOINT + '/' + LICENSE_MODEL,VEHICLE_MODEL_NAME=VEHICLE_MODEL_NAME,LICENSE_MODEL_NAME=LICENSE_MODEL_NAME)