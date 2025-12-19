import os
import typing_extensions
from typing import Annotated, Dict, List, Literal, TypedDict
from dotenv import load_dotenv

# --- LangChain/LangGraph Dependencies ---
from langchain_core.messages import AIMessage, SystemMessage, BaseMessage
from langchain_core.tools import tool
from langchain_nvidia_ai_endpoints import ChatNVIDIA, NVIDIAEmbeddings
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages
from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance
import gradio as gr

load_dotenv()

# --- Global Components ---
llm = None
embedder = None
local_vector_store = None
rag_graph = None

# Default values
RAG_COLLECTION_NAME = os.getenv("RAG_COLLECTION", "rag-collection")
SYSTEM_INSTRUCTION = os.getenv(
    "SYSTEM_INSTRUCTION",
    """
You are an AI Assistant and have tools available for RAG. The tools allow you to search for relevant content in PDFs data. 

When a user interacts with you, think whether it is necessary to retrive information using the tools available to you. 
If the context that you find, is relevant to the question, use it to answer. Alternatively, if no retrieved information 
is relevant to the question, use your own knowledge to provide an educated answer. Make sure to inform the user that you used 
your internal knowledge when you answered.

Return final answer only after tool completion.
""",
)
qdrant_mlis_url = os.getenv("QDRANT_URL", "qdrant.qdrant.svc.cluster.local:6333")
qdrant_mlis_token = os.getenv("QDRANT_TOKEN", None)


# --- Agent State Definition ---
class AgenticState(TypedDict):
    messages: Annotated[list, add_messages]


def ingest_pdf(pdf_file: str) -> list:
    loader = PyPDFLoader(pdf_file)
    pages = [doc.page_content for doc in loader.lazy_load()]
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    return text_splitter.split_text("\n".join(pages))


def process_pdf(pdf_files):
    global local_vector_store, RAG_COLLECTION_NAME

    if local_vector_store is None:
        return "❌ Error: Please initialize settings first on the Settings tab."
    if not pdf_files:
        return "⚠️ Please upload at least one PDF."

    processed_details = ""
    for file in pdf_files:
        try:
            chunks = ingest_pdf(file.name)
            docs = [
                Document(
                    page_content=c, metadata={"source": os.path.basename(file.name)}
                )
                for c in chunks
            ]
            local_vector_store.add_documents(docs)
            processed_details += (
                f"✅ {os.path.basename(file.name)}: {len(chunks)} chunks added.\n"
            )
        except Exception as e:
            processed_details += f"❌ Error {os.path.basename(file.name)}: {str(e)}\n"

    return processed_details


@tool
def retrieve_context_from_datastore(query: str) -> list:
    """Retrieve the context related to a query in a vector store."""
    if local_vector_store is None:
        return []
    return local_vector_store.similarity_search(query, k=5)


rag_tools = [retrieve_context_from_datastore]
rag_tool_node = ToolNode(rag_tools)


def reinitialize_system(
    llm_model,
    llm_url,
    llm_key,
    emb_model,
    emb_url,
    emb_key,
    collection_name,
    sys_prompt,
):
    global llm, llm_with_tools, embedder, local_vector_store, rag_graph, SYSTEM_INSTRUCTION, RAG_COLLECTION_NAME
    try:
        SYSTEM_INSTRUCTION = sys_prompt
        RAG_COLLECTION_NAME = collection_name

        # 1. Re-init Embedder
        embedder = NVIDIAEmbeddings(
            model=emb_model, base_url=emb_url, api_key=emb_key, truncate="END"
        )

        # 2. Re-init Vector Store Connection
        client = QdrantClient(url=qdrant_mlis_url, api_key=qdrant_mlis_token)
        # Create a dummy vector to check dimensionality
        dummy_vec = embedder.embed_query("test")

        if not client.collection_exists(collection_name):
            client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=len(dummy_vec), distance=Distance.COSINE
                ),
            )

        local_vector_store = QdrantVectorStore(
            client=client, collection_name=collection_name, embedding=embedder
        )

        # 3. Re-init LLM
        llm = ChatNVIDIA(
            model=llm_model, base_url=llm_url, api_key=llm_key, temperature=0.1
        )
        llm_with_tools = llm.bind_tools(rag_tools)
        # 4. Re-build Graph
        rag_graph = build_rag_graph()

        return "✅ System re-initialized! Models and Vector Store are ready."
    except Exception as e:
        return f"❌ Initialization Error: {str(e)}"


def rag_chatbot(state: AgenticState) -> AgenticState:
    """The chatbot with tools. This is a wrapper around the model's chat interface."""
    global llm_with_tools
    WELCOME_MSG = "Hello, I am here to assist you with any questions you have."

    # On the very first turn, if there are no messages yet, send only the system instruction as a SystemMessage.
    messages = state.get("messages", [])

    if messages:
        # # Ensure messages are BaseMessage objects
        response = llm_with_tools.invoke([SYSTEM_INSTRUCTION] + messages)

        # Check if and which tools were called.
        print("DEBUG tool_calls:", response.tool_calls)

    else:
        response = AIMessage(content=WELCOME_MSG)

    return state | {"messages": [response]}


def maybe_exit_user(state: AgenticState) -> Literal["rag_chatbot_node", "__end__"]:
    """Route to the chatbot unless the user is exiting the app."""

    if state.get("finished", False):
        return END  # This is the end node
    else:
        return "rag_chatbot_node"


def should_use_tool(
    state: AgenticState,
) -> Literal["rag_tool_node", "__end__"]:
    """Route between chat and tool nodes if a tool call is made."""
    if not (msgs := state.get("messages", [])):
        raise ValueError(f"No messages found when parsing state: {state}")

    msg = msgs[-1]
    if state.get("finished", False):
        # If there is nothing to do exit the app.
        return END

    elif hasattr(msg, "tool_calls") and len(msg.tool_calls) > 0:
        if any(
            tool["name"] in rag_tool_node.tools_by_name.keys()
            for tool in msg.tool_calls
        ):
            return "rag_tool_node"
    else:
        return "__end__"


def build_rag_graph():
    """Set up the graph based on the state definition."""

    graph_builder = StateGraph(AgenticState)

    # Add nodes to the graph
    graph_builder.add_node("rag_chatbot_node", rag_chatbot)
    graph_builder.add_node("rag_tool_node", rag_tool_node)

    # Set the entry point
    graph_builder.add_edge(START, "rag_chatbot_node")

    # Set up edges
    graph_builder.add_conditional_edges(
        "rag_chatbot_node", should_use_tool
    )  # to user or tool

    graph_builder.add_edge(
        "rag_tool_node", "rag_chatbot_node"
    )  # to chatbot after the tool has been called

    # Compile the graph
    chat_graph = graph_builder.compile()
    return chat_graph


# --- Gradio UI ---

with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🤖 Chat With Docs Agent")

    with gr.Tab("Settings & Config"):
        with gr.Row():
            with gr.Column():
                llm_m = gr.Textbox(
                    label="LLM Model", value=os.getenv("OPENAI_MODEL_NAME", "")
                )
                llm_u = gr.Textbox(
                    label="LLM URL", value=os.getenv("OPENAI_BASE_URL", "")
                )
                llm_k = gr.Textbox(
                    label="LLM Key",
                    type="password",
                    value=os.getenv("OPENAI_API_KEY", ""),
                )
            with gr.Column():
                emb_m = gr.Textbox(
                    label="Embed Model", value=os.getenv("EMBED_MODEL_NAME", "")
                )
                emb_u = gr.Textbox(
                    label="Embed URL", value=os.getenv("EMBED_BASE_URL", "")
                )
                emb_k = gr.Textbox(
                    label="Embed Key",
                    type="password",
                    value=os.getenv("EMBED_API_KEY", ""),
                )

        coll_n = gr.Textbox(label="Collection Name", value=RAG_COLLECTION_NAME)
        sys_p = gr.TextArea(label="System Prompt", value=SYSTEM_INSTRUCTION)
        setup_btn = gr.Button("Apply Settings", variant="primary")
        setup_status = gr.Markdown()

    with gr.Tab("PDF Ingestion"):
        pdf_input = gr.File(label="Upload PDFs", file_count="multiple")
        ingest_btn = gr.Button("Ingest Documents")
        ingest_status = gr.Markdown()

    with gr.Tab("Chat"):
        chatbot = gr.Chatbot(label="Conversation", height=500)
        msg_input = gr.Textbox(placeholder="Ask a question...")
        state_history = gr.State([])  # Holds LangChain Message objects
        clear = gr.Button("Clear chat hystory")

        # --- Streaming chat function ---
        def chat_fn(user_input, history):

            messages = [{"role": "system", "content": SYSTEM_INSTRUCTION}]
            import pdb

            for human, ai in history:

                messages.append({"role": "user", "content": human})
                messages.append({"role": "assistant", "content": ai})
            messages.append({"role": "user", "content": user_input})

            # Convert user query into LangGraph input format
            inputs = {"messages": messages}

            # Run graph step-by-step using stream
            response = rag_graph.invoke(inputs)

            # Append to chat history for Gradio
            last_response = response["messages"][-1].content
            history = history + [(user_input, last_response)]
            messages.append({"role": "assistant", "content": last_response})
            yield messages, history, ""

        msg_input.submit(
            chat_fn, [msg_input, state_history], [chatbot, state_history, msg_input]
        )
    setup_btn.click(
        reinitialize_system,
        [llm_m, llm_u, llm_k, emb_m, emb_u, emb_k, coll_n, sys_p],
        setup_status,
    )
    ingest_btn.click(process_pdf, [pdf_input], ingest_status)
    clear.click(lambda: ([], []), None, [chatbot, state_history])
if __name__ == "__main__":
    # Launch on specific port
    demo.launch(server_name="0.0.0.0", server_port=7860, share=False)
