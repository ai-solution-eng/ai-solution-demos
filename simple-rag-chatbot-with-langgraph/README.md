# 🤖 Chat With Docs Agent (RAG + Gradio + LangGraph)

A **Retrieval-Augmented Generation (RAG) application** that lets you:

* Upload PDFs 📄
* Index them into **Qdrant** using NVIDIA embeddings
* Chat with your documents via a **tool-using LangGraph agent**
* Deploy locally, via Docker, or on Kubernetes using **Helm + Istio**

This project is designed for **enterprise-grade deployments** while remaining simple to run locally.

---

## ✨ Features

* **Agentic RAG** powered by LangGraph
* **Tool-based retrieval** (vector search only when needed)
* **PDF ingestion pipeline** (chunking + metadata)
* **NVIDIA AI Endpoints** for LLMs and embeddings
* **Qdrant** vector database (remote or in-cluster)
* **Gradio UI** for configuration, ingestion, and chat
* **Docker + Helm** deployment ready

---

## 🧠 Architecture Overview

```
User ──▶ Gradio UI ──▶ LangGraph Agent
                         │
                         ├─▶ Tool: Vector Search (Qdrant)
                         │
                         └─▶ LLM (ChatNVIDIA)
```

### Core Components

| Component            | Purpose                            |
| -------------------- | ---------------------------------- |
| **LangGraph**        | Orchestrates agent + tool calls    |
| **ChatNVIDIA**       | LLM backend                        |
| **NVIDIAEmbeddings** | Text embeddings                    |
| **Qdrant**           | Vector storage & similarity search |
| **Gradio**           | Web UI                             |

---

## 📂 Project Structure

```
.
├── main.py                # Application entrypoint (agent + UI)
├── .env                   # Environment variables required by the app
├── requirements.txt       # Python dependencies
├── Dockerfile             # Container image definition
├── deploy/helm/
│   ├── values.yaml        # Helm configuration
│   ├── Chart.yaml
│   └── templates
└── README.md              
```

---

## Pre-requisite


Before deploying this application, you must satisfy the following:

1. Have access to an HPE Private Cloud AI (PCAI) environment.
2. Deploy the models which are utilized by the application using the following settings

- **Qwen/Qwen3-8B**
```
Registry: none
Model format: custom
vllm/vllm-openai:v0.9.0

Resources:
  CPU 1>8
  Memory 8Gi>32Gi
  GPU 1>1
Arguments: 
  --model Qwen/Qwen3-8B --enable-reasoning --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser hermes --gpu-memory-utilization 0.9 --port 8080
Additional Variables:
name: HUGGING_FACE_HUB_TOKEN value: $YOUR TOKEN$
name: AIOLI_PROGRESS_DEADLINE value: 1500s
```

- **nvidia/nv-embedqa-e5-v5:** Please follow instructions in [documents/deploy-NIM-to-MLIS.pdf](documents/deploy-NIM-to-MLIS.pdf)

3. Import the Qdrant framework, following the setup instructions [here](https://github.com/ai-solution-eng/frameworks/tree/main/qdrant) using AI Essentials' Tools and Framework import wizard.



## 🚀 Quick Start (Local)

### 1️⃣ Create a virtual environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

### 2️⃣ Install dependencies

```bash
pip install -r requirements.txt
```

### 3️⃣ Set environment variables

Create a `.env` file:

```env
# LLM
OPENAI_MODEL_NAME=Qwen/Qwen3-8B
OPENAI_BASE_URL=https://<llm-endpoint>/v1
OPENAI_API_KEY=your-key

# Embeddings
EMBED_MODEL_NAME=nvidia/nv-embedqa-e5-v5
EMBED_BASE_URL=https://<embed-endpoint>/v1
EMBED_API_KEY=your-key

# Vector DB
QDRANT_URL=localhost:6333
RAG_COLLECTION=rag-collection
```

### 4️⃣ Run the app locally

```bash
python main.py
```

Open 👉 [http://localhost:7860](http://localhost:7860)

---

## 🧪 How It Works

### 🔹 PDF Ingestion

1. PDFs are uploaded via Gradio
2. Text is extracted using `PyPDFLoader`
3. Chunked with `RecursiveCharacterTextSplitter`
4. Embedded and stored in Qdrant

### 🔹 Agent Execution

* The agent decides **whether retrieval is needed**
* If yes → calls `retrieve_context_from_datastore`
* Retrieved chunks are injected into the LLM context
* If not → the model answers using internal knowledge

---

## 🛠 Configuration via UI

The **Settings tab** allows runtime reconfiguration of:

* LLM model, URL, API key
* Embedding model, URL, API key
* Qdrant collection name
* System prompt

No restart required 🚀

---

## 🐳 Docker Usage

### Build image

```bash
docker build -t rag-gradio-app .
```

### Run container

```bash
docker run -p 7860:7860 \
  --env-file .env \
  rag-gradio-app
```

---

## ☸️ Kubernetes + Helm Deployment in HPE Private Cloud AI

### 1️⃣ Update values

Edit `helm/values.yaml`:

* LLM & embedding endpoints
* API keys
* Qdrant service address
* Image tag

### 2️⃣ Package

```bash
helm package ./helm
```

### 3️⃣ Import into PCAI Using AI Essentials' Tools & Framework Wizard

Launch the app and use it.

---

## 🔐 Security Notes

⚠️ **Do not commit API keys**

For production:

* Use Kubernetes Secrets
* Inject env vars securely
* Rotate tokens regularly

---

## 📦 Dependencies

Key libraries used:

* `langchain`
* `langgraph`
* `langchain-nvidia-ai-endpoints`
* `langchain-qdrant`
* `qdrant-client`
* `gradio`

---

## 🧩 Extending the System

Ideas:

* Add reranking (e.g. Cross-Encoders)
* Add metadata filters (source, page)
* Enable streaming responses
* Persist chat history per user
* Add authentication

---

## 🙌 Acknowledgements

Built with:

* LangChain & LangGraph
* NVIDIA AI Endpoints
* Qdrant Vector Database
* Gradio UI

---
