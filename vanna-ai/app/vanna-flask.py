from vanna.openai import OpenAI_Chat
from vanna.chromadb import ChromaDB_VectorStore
from openai import OpenAI
from loguru import logger
from vanna.flask import VannaFlaskApp
from ai import AIPipeline
import os

CHAT_MODEL = os.environ.get("CHAT_MODEL", "TinyLlama/TinyLlama-1.1B-Chat-v0.4")
CHAT_MODEL_BASE_URL = os.environ.get("CHAT_MODEL_BASE_URL", None)
TEMP = os.environ.get("TEMP", 0.4)
MAX_TOKENS = os.environ.get("MAX_TOKENS", 131072)
DB_PATH = os.environ.get("DB_PATH", "./db")
DATABASE_CONNECTION_STRING = os.environ.get("DATABASE_CONNECTION_STRING")
DB_TYPE = os.environ.get("DB_TYPE", "sqlite")
API_KEY = os.environ.get("OPENAI_API_KEY", "fake")

# Postgres settings
PG_HOST = os.environ.get("PG_HOST")
PG_DBNAME = os.environ.get("PG_DBNAME")
PG_USER = os.environ.get("PG_USER")
PG_PASSWORD = os.environ.get("PG_PASSWORD")
PG_PORT = int(os.environ.get("PG_PORT", 5432))


ai = AIPipeline(
    chat_model=CHAT_MODEL,
    chat_model_url=CHAT_MODEL_BASE_URL,
    max_tokens=MAX_TOKENS,
    temp=TEMP,
    db_path=DB_PATH,
    db_connection_string=DATABASE_CONNECTION_STRING,
    db_type=DB_TYPE,
    pg_host=PG_HOST,
    pg_dbname=PG_DBNAME,
    pg_user=PG_USER,
    pg_password=PG_PASSWORD,
    pg_port=PG_PORT,
    api_key=API_KEY,
)

vn = ai.init_vanna()

app = VannaFlaskApp(vn, allow_llm_to_see_data=True)

app.run()