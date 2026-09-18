from __future__ import annotations

import argparse
import importlib
from pathlib import Path
import subprocess
import sys

import joblib


def _ensure_pkg_resources() -> None:
    """Install setuptools on demand because pkg_resources is provided by setuptools."""
    try:
        importlib.import_module("pkg_resources")
        return
    except ModuleNotFoundError:
        pass

    print("pkg_resources not found. Installing setuptools in the current interpreter...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "setuptools>=68,<81"]
    )
    importlib.import_module("pkg_resources")


def main() -> None:
    _ensure_pkg_resources()
    import bentoml

    parser = argparse.ArgumentParser(description="Import a joblib sklearn model into BentoML model store")
    parser.add_argument(
        "--model-path",
        default="random_forest_model.pkl",
        help="Path to the exported joblib model file",
    )
    parser.add_argument(
        "--model-name",
        default="predictive_maintenance_rf",
        help="Model name in BentoML model store",
    )
    args = parser.parse_args()

    model_path = Path(args.model_path)
    if not model_path.exists():
        raise FileNotFoundError(f"Model file not found: {model_path}")

    model = joblib.load(model_path)

    tag = bentoml.sklearn.save_model(
        args.model_name,
        model,
        signatures={
            "predict": {"batchable": True},
            "predict_proba": {"batchable": True},
        },
    )
    print(f"Saved BentoML model: {tag}")


if __name__ == "__main__":
    main()
