import json
import os

from vanna.openai import OpenAI_Chat
from vanna.chromadb import ChromaDB_VectorStore
from fastapi.encoders import jsonable_encoder
from iso639 import languages
from llama_index.llms.openai_like import OpenAILike
from loguru import logger
from llama_index.llms.openai_like import OpenAILike
from openai import OpenAI
from models import (ConfigItem, DefaultConfig, Event, GeneratePostResponse,
                    ReferenceData, ValueType)


class MyVanna(ChromaDB_VectorStore, OpenAI_Chat):
    def __init__(self, config={}, client=None):
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
        db_type='sqlite'
    ):
        self.chat_model_url = chat_model_url
        self.chat_model = (
            chat_model if chat_model else list(self.get_models().keys())[0]
        )
        self.temp = temp
        self.max_tokens = max_tokens
        self.db_path = db_path
        self.streaming = streaming
        self.db_connection_string = db_connection_string
        self.db_type = db_type

    def get_config(self):
        config = DefaultConfig(
            [
                ConfigItem(
                    name="modelTemperature",
                    friendlyName="Model Temperature",
                    minValue=0.0,
                    maxValue=1.0,
                    valueType=ValueType.FLOAT,
                    defaultValue=self.temp,
                    description="The randomness of the LLM response. Higher is more random, lower is more deterministic",
                ),
                ConfigItem(
                    name="maxOutputTokens",
                    friendlyName="Maximum Output Tokens",
                    minValue=0,
                    maxValue=5000,
                    valueType=ValueType.INT,
                    defaultValue=self.max_tokens,
                    description="The Maximum number of tokens for the LLM to generate ",
                )
            ]
        )
        return config

    def init_vanna(self):
        vn = MyVanna(client=self.get_vanna_llm(), config={'model': self.chat_model, 'path': self.db_path, 'max_tokens': self.max_tokens})
        match self.db_type:
            case 'sqlite':
                logger.info(f"connecting to sqlite db {self.db_connection_string}")
                vn.connect_to_sqlite(self.db_connection_string)
                training_data = vn.get_training_data()
                if training_data.count().id == 0:
                    logger.info("empty vector db, initializing..")
                    df_ddl = vn.run_sql("SELECT type, sql FROM sqlite_master WHERE sql is not null")
                    for ddl in df_ddl['sql'].to_list():
                        vn.train(ddl=ddl)
                else:
                    logger.info("vector db already initialized, skipping..")
                    logger.info(f"vector db has {training_data.count().id} records")
            case 'mssql':
                logger.info(f"connecting to mssql db {self.db_connection_string}")
                vn.connect_to_mssql(odbc_conn_str=self.db_connection_string)
                training_data = vn.get_training_data()
                if training_data.count().id == 0:
                    logger.info("empty vector db, initializing..")
                    df_information_schema = vn.run_sql("SELECT * FROM INFORMATION_SCHEMA.COLUMNS")
                    plan = vn.get_training_plan_generic(df_information_schema)
                    vn.train(plan=plan)
                else:
                    logger.info("vector db already initialized, skipping..")
                    logger.info(f"vector db has {training_data.count().id} records")
        return vn

    def get_models(self):
        model_urls = str(self.chat_model_url).split(",")
        available_models = {}
        if len(model_urls) >= 1:
            for url in model_urls:
                client = OpenAI(base_url=url, api_key="fake")
                models = client.models.list().data
                for model in models:
                    available_models[model.id] = url
        else:
            client = OpenAI(base_url=model_urls[0], api_key="fake")
            models = client.models.list().data
            for model in models:
                available_models[model.id] = model_urls[0]
        return available_models

    def load_llm(self, url=None):
        if url is None:
            modelpath = str(self.chat_model_url.split(",")[0])
        else:
            modelpath = url
        logger.info(f"Using OpenAPI-compatible LLM endpoint: {modelpath}")

        llm = OpenAILike(model="", api_base=modelpath, api_key="fake")
        return llm

    def get_vanna_llm(self):
        modelpath = str(self.chat_model_url.split(",")[0])
        logger.info(f"Using OpenAPI-compatible LLM endpoint: {modelpath}")

        llm = OpenAI(base_url=modelpath)
        return llm

    def output_stream(self, llm_stream, context):
        references = self.format_references(context)
        resp = GeneratePostResponse(event=Event.reference, data=references)
        yield f"{json.dumps(jsonable_encoder(resp))}\n"
        for chunk in llm_stream:
            stuff = GeneratePostResponse(event=Event.answer, data=chunk.delta)
            yield f"{json.dumps(jsonable_encoder(stuff))}\n"

    def generate_response(
        self, query, system_prompt, model, model_args, streaming=True
    ):
        generate_kwargs = {
            "temperature": (
                model_args["model_temperature"]
                if model_args["model_temperature"] is not None
                else self.temp
            ),
            "top_p": 0.5,
            "max_tokens": (
                model_args["max_output_tokens"]
                if model_args["max_output_tokens"] is not None
                else self.max_tokens
            ),
        }
        logger.info(f"Submitted Args: {model_args}")

        if model:
            models = self.get_models()
            llm = self.load_llm(models[model])
            llm.model = model
        else:
            llm = self.load_llm()
            llm.model = self.chat_model
        vn = self.init_vanna()
        logger.info(f"Querying with: {query}")
        sql_query = vn.generate_sql(question=query)
        context_str = str(vn.run_sql(sql_query).to_html())
        context_str = context_str.replace('<table border="1" class="dataframe">', '<table border="1" class="dataframe">')
        system_prompt = ""
        logger.info(f"system prompt: {system_prompt}")
        logger.info(f"Context: {context_str}")
        text_qa_template_str_llama3 = f"""
            <|begin_of_text|><|start_header_id|>user<|end_header_id|>
            Context information is
            below.
            ---------------------
            {context_str}
            ---------------------
            Using
            the context information, answer the question: {query}
            {system_prompt}
            <|eot_id|><|start_header_id|>assistant<|end_header_id|>
            """
        if self.streaming:
            logger.info(f"Requesting model {llm.model} for streaming")
            context = (context_str, sql_query)
            return (
                llm.stream_complete(
                    text_qa_template_str_llama3, formatted=True, **generate_kwargs
                ),
                context,
            )
        else:
            logger.info(f"Requesting model {llm.model} for completion")
            output_response = llm.complete(text_qa_template_str_llama3)
            context = (context_str, sql_query)
            return output_response, context

    def format_references(self, source_nodes):
        text, query = source_nodes
        refs_to_return = []
        ref = ReferenceData(
                source='Database',
                text=text,
                similarityScore=None,
                page=None,
                url=None,
                metadata={"SQL Query": query},
            )
        refs_to_return.append(ref)
        return refs_to_return
