from __future__ import annotations

import importlib
import subprocess
import sys

import pandas as pd
import numpy as np


def _ensure_pkg_resources() -> None:
    try:
        importlib.import_module("pkg_resources")
        return
    except ModuleNotFoundError:
        pass

    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "setuptools==70.3.0"]
    )
    importlib.import_module("pkg_resources")


_ensure_pkg_resources()

import bentoml
from bentoml.io import JSON

MODEL_TAG = "predictive_maintenance_rf:latest"
FEATURE_COLUMNS = [
    "Air temperature [K]",
    "Process temperature [K]",
    "Rotational speed [rpm]",
    "Torque [Nm]",
    "Tool wear [min]",
]

model_ref = bentoml.sklearn.get(MODEL_TAG)
local_model = bentoml.sklearn.load_model(MODEL_TAG)
model_runner = model_ref.to_runner()
svc = bentoml.Service("predictive-maintenance-service", runners=[model_runner])

try:
    positive_class_idx = int(np.where(local_model.classes_ == 1)[0][0])
except Exception:
    positive_class_idx = 1


@svc.api(input=JSON(), output=JSON())
def predict(request: dict) -> dict:
    row = {
        "Air temperature [K]": float(request["air_temperature_k"]),
        "Process temperature [K]": float(request["process_temperature_k"]),
        "Rotational speed [rpm]": float(request["rotational_speed_rpm"]),
        "Torque [Nm]": float(request["torque_nm"]),
        "Tool wear [min]": float(request["tool_wear_min"]),
    }

    frame = pd.DataFrame([row], columns=FEATURE_COLUMNS)
    prediction = int(model_runner.predict.run(frame)[0])
    proba = model_runner.predict_proba.run(frame)[0]
    probability = float(proba[positive_class_idx])

    return {
        "machine_failure_prediction": prediction,
        "machine_failure_probability": probability,
        "model_classes": [int(c) for c in local_model.classes_],
    }
