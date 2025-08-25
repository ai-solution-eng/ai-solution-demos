from __future__ import annotations

import asyncio
import os
from typing import List

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from sse_starlette.sse import EventSourceResponse
from starlette.responses import StreamingResponse

from ai import AIPipeline
from models import (ConfigGetResponse, GeneratePostRequest,
                    GeneratePostResponse, SourcesGetResponse)

DEFAULT_CHAT_MODEL = os.environ.get("CHAT_MODEL", None)
CHAT_MODEL_BASE_URL = os.environ.get("CHAT_MODEL_BASE_URL", None)
STREAMING = os.environ.get("STREAMING", True)
TEMP = float(os.environ.get("TEMP", 0.2))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", 512))
DB_PATH = os.environ.get("DB_PATH", "./db")
DB_CONNECTION_STRING = os.environ.get("DB_CONNECTION_STRING", None)
DB_TYPE = os.environ.get("DB_TYPE", "sqlite")
API_KEY = os.environ.get("OPENAI_API_KEY", "fake")

# Postgres settings
PG_HOST = os.environ.get("PG_HOST")
PG_DBNAME = os.environ.get("PG_DBNAME")
PG_USER = os.environ.get("PG_USER")
PG_PASSWORD = os.environ.get("PG_PASSWORD")
PG_PORT = int(os.environ.get("PG_PORT", 5432))


ai = AIPipeline(
    chat_model=DEFAULT_CHAT_MODEL,
    chat_model_url=CHAT_MODEL_BASE_URL,
    max_tokens=MAX_TOKENS,
    temp=TEMP,
    db_path=DB_PATH,
    streaming=STREAMING,
    db_connection_string=DB_CONNECTION_STRING,
    db_type=DB_TYPE,
    pg_host=PG_HOST,
    pg_dbname=PG_DBNAME,
    pg_user=PG_USER,
    pg_password=PG_PASSWORD,
    pg_port=PG_PORT,
    api_key=API_KEY,
)

app = FastAPI(
    title="Retrieval Augmented Generation API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

llm = ai.load_llm()


@app.get("/config", response_model=ConfigGetResponse)
def get_config() -> ConfigGetResponse:
    config = ai.get_config()
    models = list(ai.get_models().keys())
    logger.info(f"config: {config}")
    logger.info(f"models: {models}")
    return ConfigGetResponse(defaultConfig=config, models=models)


@app.post("/generate", response_model=GeneratePostResponse)
def post_generate(body: GeneratePostRequest) -> GeneratePostResponse:
    system_prompt = None
    if body.config:
        config = body.config
        cutoff = config["similarityCutoff"] if "similarityCutoff" in config else None
        cutoff = cutoff / 100 if cutoff and cutoff > 1.0 else cutoff
        top_k = config["topK"] if "topK" in config else None
        temp = config["modelTemperature"] if "modelTemperature" in config else None
        max_tokens = config["maxOutputTokens"] if "maxOutputTokens" in config else None
        system_prompt = config["systemPrompt"] if "systemPrompt" in config else None
    else:
        cutoff = None
        top_k = None
        temp = None
        max_tokens = None

    model = body.model
    model_options = {
        "cutoff": cutoff,
        "top_k": top_k,
        "model": model,
        "tags": body.tags,
        "model_temperature": temp,
        "max_output_tokens": max_tokens,
    }
    (
        response,
        output_nodes,
    ) = ai.generate_response(
        body.query, system_prompt, model, model_options
    )
    return StreamingResponse(
        ai.output_stream(response, output_nodes), media_type="application/x-ndjson"
    )


@app.get("/sources", response_model=List[SourcesGetResponse])
def get_sources() -> List[SourcesGetResponse]:
    return []


if __name__ == "__main__":
    uvicorn.run(
        "main:app", host="0.0.0.0", port=5001, log_level="debug", reload=True, workers=1
    )