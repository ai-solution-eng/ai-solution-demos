# Load dependencies
import os
import jpype
import jaydebeapi
import pandas as pd
import streamlit as st
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langchain.output_parsers import StructuredOutputParser, ResponseSchema
from langchain.prompts import ChatPromptTemplate
from dotenv import load_dotenv
import sys

# ---- Load variables in .env file ----
load_dotenv()

# ---- DB CONFIG ----
db_user = os.getenv("DB_USER")
db_password = os.getenv("DB_PASSWORD")
db_jdbc_url = os.getenv("JDBC_DATABASE_URL")

# ---- LLM CONFIG ----
llm_url = os.getenv("LLM_URL")
llm_model = os.getenv("LLM_MODEL")
llm_api_key = os.getenv("LLM_API_KEY")

# ---- PATH TO DB DRIVERS ----
jvm_path = os.getenv("JVM_PATH")
jdbc_jar = os.getenv("JDBC_JAR_PATH")
mongo_jar = os.getenv("MONGO_JAR_PATH")

if not jpype.isJVMStarted():
    jpype.startJVM(jvm_path, "-ea", f"-Djava.class.path={jdbc_jar}:{mongo_jar}")

JDBC_DRIVERS = {
    "informix": {
        "jar": jdbc_jar,
        "classes": ["com.informix.jdbc.IfxDriver", "com.informix.jdbcx.IfxDriver"],
        "url": db_jdbc_url,
        "user": db_user,
        "password": db_password,
    }
}


def connect(db_type):
    cfg = JDBC_DRIVERS[db_type]
    driver_loaded = False
    for drv_class in cfg["classes"]:
        try:
            jpype.JClass(drv_class)
            driver_class = drv_class
            driver_loaded = True
            break
        except Exception:
            continue
    if not driver_loaded:
        raise Exception(f"Could not load JDBC driver class for {db_type}")
    conn = jaydebeapi.connect(driver_class, cfg["url"], [cfg["user"], cfg["password"]])
    return conn


def fetch_data(db_type, query):
    conn = connect(db_type)
    curs = conn.cursor()
    curs.execute(query)
    rows = curs.fetchall()
    columns = [desc for desc in curs.description]
    curs.close()
    conn.close()

    # Convert Java objects (java.lang.String, etc.) to Python str
    def convert(value):
        if isinstance(value, jpype.java.lang.String):
            return str(value)
        return value

    clean_rows = [[convert(v) for v in row] for row in rows]

    print(f"✅ Extracted: {len(clean_rows)} rows")
    return [x[0] for x in columns], clean_rows


# ---- STREAMLIT APP ----
st.title("🏥 Resumen de la Visita")

# Input for INTERNACION_ID
internacion_id = st.text_input("Teclear Internación ID:", "1020548")

if st.button("Ejecutar Consulta"):
    if not internacion_id:
        st.warning("Please enter an Internación ID.")
    else:
        query = f"""
        SELECT 
        a.hc, 
        a.internacion_id, 
        a.tipo, 
        a.xml, 
        a.ts, 
        b.nombre 
        FROM hce:rm_internacion a 
        JOIN passing_view b ON a.usr_id = b.usuario 
        WHERE a.internacion_id = {internacion_id}
        ORDER BY a.ts; 
        """
        column_names, rows = fetch_data("informix", query)

        st.session_state["column_names"] = column_names
        st.session_state["rows"] = rows

# Show query results if they exist
if "rows" in st.session_state:
    df = pd.DataFrame(
        st.session_state["rows"], columns=st.session_state["column_names"]
    )
    st.dataframe(df)

# Only show "Create Summary" button if query results exist
if "rows" in st.session_state and st.button("Crear Resumen"):
    column_names = st.session_state["column_names"]
    rows = st.session_state["rows"]

    # ----- STRUCTURED OUTPUT -----
    response_schemas = [
        ResponseSchema(
            name="Datos Generales de Identificación",
            description="Nombre completo del paciente, edad y número de identificación...",
        ),
        ResponseSchema(
            name="Antecedentes Relevantes",
            description="Información médica pasada relevante...",
        ),
        ResponseSchema(
            name="Motivo del Ingreso/Tratamiento",
            description="Razón principal de ingreso...",
        ),
        ResponseSchema(
            name="Diagnóstico Principal",
            description="Diagnóstico principal y secundarios...",
        ),
        ResponseSchema(
            name="Métodos Diagnósticos y Tratamientos",
            description="Estudios y tratamientos aplicados...",
        ),
        ResponseSchema(
            name="Evolución del Paciente",
            description="Cómo respondió el paciente al tratamiento...",
        ),
        ResponseSchema(name="Complicaciones", description="Complicaciones médicas..."),
        ResponseSchema(
            name="Condición al Alta", description="Estado al momento del egreso..."
        ),
        ResponseSchema(
            name="Recomendaciones para Cuidados Futuros",
            description="Instrucciones de tratamiento ambulatorio, etc...",
        ),
    ]
    output_parser = StructuredOutputParser.from_response_schemas(response_schemas)
    format_instructions = output_parser.get_format_instructions()

    fields = "\n\n".join(
        "\n".join(f"{col}: {val}" for col, val in zip(column_names, row))
        for row in rows
    )

    template = f"""Create a structured output following a patient visit at the hospital using the history of the visit reported below:

    {fields}

    Instructions:
    - Make sure all the tags of the structured output are present in your response and follow this exact format:
    {format_instructions.replace('{', '{{').replace('}', '}}')}
    """

    llm = ChatNVIDIA(base_url=llm_url, model=llm_model, api_key=llm_api_key)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a medical assistant that extracts structured visit summaries.",
            ),
            ("human", template),
        ]
    )

    messages = prompt.format_messages()
    response = llm.invoke(messages)
    output_dict = output_parser.parse(response.content)

    # Show editable text areas for each structured field
    summary_text = "\n\n".join(f"{k}:\n{v.strip()}" for k, v in output_dict.items())
    st.text_area("Resumen estructurado", summary_text, height=400)
