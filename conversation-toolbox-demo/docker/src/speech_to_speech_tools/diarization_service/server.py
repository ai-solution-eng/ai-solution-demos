"""
Speaker Diarization Microservice
Wraps pyannote/speaker-diarization-community-1 behind a FastAPI endpoint.
Designed to run on a GPU node alongside vLLM.
"""

import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("diarization_service")

HF_TOKEN = os.environ.get("HF_TOKEN", "")
PIPELINE_NAME = os.environ.get("DIARIZATION_PIPELINE", "pyannote/speaker-diarization-community-1")
GPU_MEMORY_FRACTION = float(os.environ.get("GPU_MEMORY_FRACTION", "0.10"))

pipeline = None
pipeline_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pipeline, pipeline_ready
    logger.info(f"Loading diarization pipeline: {PIPELINE_NAME}")
    try:
        if GPU_MEMORY_FRACTION > 0 and torch.cuda.is_available():
            torch.cuda.set_per_process_memory_fraction(GPU_MEMORY_FRACTION)
            logger.info(f"GPU memory fraction set to {GPU_MEMORY_FRACTION}")

        from pyannote.audio import Pipeline

        pipeline = Pipeline.from_pretrained(
            PIPELINE_NAME,
            token=HF_TOKEN,
        )
        if torch.cuda.is_available():
            pipeline.to(torch.device("cuda"))
            logger.info("Pipeline moved to CUDA")
        pipeline_ready = True
        logger.info("Diarization pipeline loaded successfully")
    except Exception as e:
        logger.error(f"Failed to load diarization pipeline: {e}")
        pipeline_ready = False
    yield
    pipeline = None
    pipeline_ready = False
    logger.info("Diarization service shut down")


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    if not pipeline_ready or pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline not ready")
    return {"status": "ready", "pipeline": PIPELINE_NAME}


@app.post("/diarize")
async def diarize(
    file: UploadFile = File(...),
    num_speakers: int | None = Form(None),
    min_speakers: int | None = Form(None),
    max_speakers: int | None = Form(None),
):
    if not pipeline_ready or pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline not ready")

    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        kwargs = {}
        if num_speakers is not None:
            kwargs["num_speakers"] = num_speakers
        if min_speakers is not None:
            kwargs["min_speakers"] = min_speakers
        if max_speakers is not None:
            kwargs["max_speakers"] = max_speakers

        output = pipeline(tmp_path, **kwargs)

        segments = []
        for turn, _, speaker in output.itertracks(yield_label=True):
            segments.append(
                {
                    "speaker": speaker,
                    "start": round(turn.start, 3),
                    "end": round(turn.end, 3),
                }
            )

        exclusive_segments = []
        if hasattr(output, "exclusive_speaker_diarization"):
            for turn, _, speaker in output.exclusive_speaker_diarization.itertracks(yield_label=True):
                exclusive_segments.append(
                    {
                        "speaker": speaker,
                        "start": round(turn.start, 3),
                        "end": round(turn.end, 3),
                    }
                )

        num_speakers_detected = len({s["speaker"] for s in segments})
        duration = max((s["end"] for s in segments), default=0.0)

        return {
            "segments": segments,
            "exclusive_segments": exclusive_segments,
            "duration": duration,
            "num_speakers_detected": num_speakers_detected,
        }
    except Exception as e:
        logger.error(f"Diarization error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
