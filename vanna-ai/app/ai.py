# run_pipeline.py
import os
from loguru import logger

from vanna.openai import OpenAI_Chat
from vanna.chromadb import ChromaDB_VectorStore
from openai import OpenAI, AuthenticationError

# LlamaIndex OpenAI-like import moved across versions
try:
    from llama_index.llms.openai_like import OpenAILike
except Exception:
    from llama_index.llms.openai_like.base import OpenAILike


class MyVanna(ChromaDB_VectorStore, OpenAI_Chat):
    def __init__(self, config=None, client=None):
        config = config or {}
        ChromaDB_VectorStore.__init__(self, config=config)
        OpenAI_Chat.__init__(self, client=client, config=config)


class AIPipeline:
    def __init__(
        self,
        chat_model,
        chat_model_url,
        max_tokens=600,
        temp=0.2,
        streaming=False,
        db_path="./db",
        db_connection_string=None,
        db_type="sqlite",
        # Postgres params
        pg_host=None,
        pg_dbname=None,
        pg_user=None,
        pg_password=None,
        pg_port=5432,
        # Auth
        api_key=None,
    ):
        self.chat_model_url = chat_model_url
        self.chat_model = chat_model
        self.temp = temp
        self.max_tokens = max_tokens
        self.db_path = db_path
        self.streaming = streaming
        self.db_connection_string = db_connection_string
        self.db_type = db_type

        self.pg_host = pg_host
        self.pg_dbname = pg_dbname
        self.pg_user = pg_user
        self.pg_password = pg_password
        self.pg_port = pg_port

        self.api_key = api_key or os.getenv("OPENAI_API_KEY")

    # ------------ LLM helpers ------------
    def get_models(self):
        """Optional: list models from the first endpoint; not used if you pass chat_model."""
        models_map = {}
        for url in [u.strip() for u in str(self.chat_model_url).split(",") if u.strip()]:
            try:
                client = OpenAI(base_url=url, api_key=self.api_key)
                for m in client.models.list().data:
                    models_map[m.id] = url
            except Exception as e:
                logger.warning(f"Failed to list models from {url}: {e}")
        return models_map

    def load_llm(self, url=None):
        modelpath = str(self.chat_model_url.split(",")[0] if url is None else url)
        logger.info(f"Using OpenAI-compatible LLM endpoint (OpenAILike): {modelpath}")
        llm = OpenAILike(model="", api_base=modelpath, api_key=self.api_key)
        return llm

    def get_vanna_llm(self):
        modelpath = str(self.chat_model_url.split(",")[0])
        logger.info(f"Using OpenAI-compatible LLM endpoint (OpenAI SDK): {modelpath}")
        return OpenAI(base_url=modelpath, api_key=self.api_key)

    # ------------ Vanna init & training ------------
    def init_vanna(self):
        vn = MyVanna(
            client=self.get_vanna_llm(),
            config={"model": self.chat_model, "path": self.db_path, "max_tokens": self.max_tokens},
        )

        match self.db_type:
            case "sqlite":
                if not self.db_connection_string:
                    raise RuntimeError("db_connection_string required for sqlite")
                logger.info(f"connecting to sqlite db {self.db_connection_string}")
                vn.connect_to_sqlite(self.db_connection_string)
                training_data = vn.get_training_data()
                if training_data.count().id == 0:
                    logger.info("empty vector db, initializing..")
                    df_ddl = vn.run_sql("SELECT type, sql FROM sqlite_master WHERE sql is not null")
                    for ddl in df_ddl["sql"].to_list():
                        vn.train(ddl=ddl)
                else:
                    logger.info(f"vector db already initialized: {training_data.count().id} records")

            case "mssql":
                if not self.db_connection_string:
                    raise RuntimeError("db_connection_string (ODBC) required for mssql")
                logger.info(f"connecting to mssql db {self.db_connection_string}")
                vn.connect_to_mssql(odbc_conn_str=self.db_connection_string)
                training_data = vn.get_training_data()
                if training_data.count().id == 0:
                    logger.info("empty vector db, initializing..")
                    df_info = vn.run_sql("SELECT * FROM INFORMATION_SCHEMA.COLUMNS")
                    plan = vn.get_training_plan_generic(df_info)
                    vn.train(plan=plan)
                else:
                    logger.info(f"vector db already initialized: {training_data.count().id} records")

            case "postgres":
                logger.info(f"connecting to postgres db host={self.pg_host}, dbname={self.pg_dbname}")
                vn.connect_to_postgres(
                    host=self.pg_host,
                    dbname=self.pg_dbname,
                    user=self.pg_user,
                    password=self.pg_password,
                    port=self.pg_port,
                )
                training_data = vn.get_training_data()
                if training_data.count().id == 0:
                    logger.info("empty vector db, initializing..")
                    # Include required columns for get_training_plan_generic
                    df_info = vn.run_sql(
                        """
                        SELECT
                            table_catalog,
                            table_schema,
                            table_name,
                            column_name,
                            data_type,
                            is_nullable,
                            column_default,
                            ordinal_position
                        FROM information_schema.columns
                        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                        ORDER BY table_catalog, table_schema, table_name, ordinal_position
                        """
                    )
                    plan = vn.get_training_plan_generic(df_info)
                    vn.train(plan=plan)
                else:
                    logger.info(f"vector db already initialized: {training_data.count().id} records")

            case _:
                raise RuntimeError(f"Unsupported db_type: {self.db_type}")

        return vn

    # ------------ Generation ------------
    def generate_response(self, query, system_prompt, model, model_args, streaming=True):
        generate_kwargs = {
            "temperature": (model_args.get("model_temperature") if model_args.get("model_temperature") is not None else self.temp),
            "top_p": 0.5,
            "max_tokens": (model_args.get("max_output_tokens") if model_args.get("max_output_tokens") is not None else self.max_tokens),
        }
        logger.info(f"Submitted Args: {model_args}")

        if model:
            models = self.get_models()
            llm = self.load_llm(models.get(model))
            llm.model = model
        else:
            llm = self.load_llm()
            llm.model = self.chat_model

        vn = self.init_vanna()

        logger.info(f"Querying with: {query}")
        sql_query = vn.generate_sql(question=query)
        df_html = str(vn.run_sql(sql_query).to_html())
        context_str = df_html  # already HTML

        text_qa_template = f"""
<|begin_of_text|><|start_header_id|>user<|end_header_id|>
Context information is below.
---------------------
{context_str}
---------------------
Using the context information, answer the question: {query}
{system_prompt}
<|eot_id|><|start_header_id|>assistant<|end_header_id|>
""".strip()

        if streaming or self.streaming:
            logger.info(f"Requesting model {llm.model} for streaming")
            context = (context_str, sql_query)
            return llm.stream_complete(text_qa_template, formatted=True, **generate_kwargs), context
        else:
            logger.info(f"Requesting model {llm.model} for completion")
            out = llm.complete(text_qa_template, **generate_kwargs)
            text = getattr(out, "text", out)  # some versions return .text
            context = (context_str, sql_query)
            return text, context


if __name__ == "__main__":
    # Build pipeline from explicit args (env var OPENAI_API_KEY must be set)
    pipeline = AIPipeline(
        chat_model="openai/gpt-oss-20b",
        chat_model_url="https://gpt-oss-20b-predictor-andrew-mendez-94deaedd.ingress.pcai0108.sv11.hpecolo.net/v1",
        db_type="postgres",
        pg_host="postgresql.postgresql.svc.cluster.local",
        pg_dbname="testdb",
        pg_user="postgres",
        pg_password="6UQJnf8Bh7",
        pg_port=5432,
    )

    try:
        response, (context_html, sql_used) = pipeline.generate_response(
            query="Can you list the columns in the database",
            system_prompt="you are a helpful assistant.",
            model=None,
            model_args={"model_temperature": 0.1, "max_output_tokens": 1600},
            streaming=False,
        )
        print("=== SQL used ===")
        print(sql_used)
        print("\n=== Answer ===")
        print(response)
    except AuthenticationError as e:
        logger.error("Authentication failed against the LLM endpoint. Ensure OPENAI_API_KEY is a valid token.")
        raise