<div align=center>
<img src="https://raw.githubusercontent.com/hpe-design/logos/master/Requirements/color-logo.png" alt="HPE Logo" height="100"/>
</div>

# HPE Private Cloud AI

##  AI Solution Use Case Demos

This repository contains use case demos developed for Private Cloud AI (PCAI). 

The most generic, vertical-agnostic demos, implementing some of the most recurrent use cases are found are the root level of this repo. 
These are the following:

| Demo                                                          | Short Description          |
| --------------------------------------------------------------|----------------------------|
| [Basic Agent Langflow](basic-agent-langflow)              | A **Langflow** setup defining a basic agentic flow to answer questions requiring informations from both local files, using RAG, and data from a SQL database. Relies on **MLIS** for model deployment. **MCP server** usage optional.           |
| [Base Code Assistant - Opencode](basic-code-assistant-opencode)                          | An explanation on how to setup and use **Opencode**, an open source AI coding agent, in a **VS Code server**, leveraging models deployed using **MLIS**. Includes an optional step to leverage **GitHub MCP server**.            |
| [Conversation Toolbox](conversation-toolbox-demo)                          | A custom web application connecting to a chat model, an ASR model and Fish Audio S2 pro TTS model, deployed using **MLIS**, to provide a multilingual AI voice assistant, as well as file transcriptions capabilities. Voice Assistant accepts connections to **MCP servers** to enrich its capabilities.           |
| [Finetune Tool Calling LLM](finetune-tool-calling-llm)                | **Notebooks**, using **Nemo microservices** to fine-tune an LLM to improve its tool-calling capabilities.           |
| [Image Generation - ComfyUI](image-generation-comfyui)                | An explanation of how to simply use **ComfyUI**, an AI creation engine enabling powerful media creation AI workflows, such as, but not limited to **image generation**, **image editing** and **video generation**.|
| [Image Segmentation](image-segmentation)                      | Python scripts to fine-tune CNNs for segmentation tasks on provided datasets, expected to be executed in a **Jupyter notebook**, with experiment tracking on **MLflow**. Also includes a streamlit application to display segmentation results from any checkpoint saved, on any dataset image.           |
| [Multimodal RAG](multimodal-rag)                        | An advanced retrieval-augmented generation (RAG) flow, supporting multiple files modalities, including text, images, audio and video, coming with its own **MCP server** to easily reuse this RAG flow elsewhere. Includes steps on how to use it with **Open WebUI** and **Opencode**.           |
| [NL to SQL](nl-to-sql)                        | An **Open WebUI** setup to allow chatting with SQL data, leveraging tools from an **MCP server** to interact with data from a Postgres database. Relies on **MLIS** for model deployment.           |
| [Object Detection - YOLO](object-detection-yolo)                        | A simple **streamlit application** running object detection inference using a **YOLO model** on images and videos it takes as input.           |
| [Offline Meeting Transcription](offline-meeting-transcription)                | A transcription pipeline converting raw audio recordings into speaker-attributed transcripts and structured meeting minutes, using Whisper (for ASR, deployed on **MLIS**) and **Pyannote** (for speaker diarization) connected to **Open WebUI**.|
| [Realtime Live Voice Translation](realtime-live-voice-translation)                | A custom web application that captures the user's voice and provides transcription and translation in real time. Relies on Whisper ASR model and a generic LLM deployed on **MLIS**.|
| [Text Document Analysis](text-document-analysis)                | A simple web application in which users can upload text and PDF files, ask or upload a list of questions and get answers for each document in an Excel sheet, after document analysis leveraging an LLM deployed using **MLIS**.           |
| [Vision Analytics](vision-analytics)                        | A Gradio application using a VLM to analyze images, videos and/or streams. Files can be uploaded from the UI, or read from the filesystem. Relies on **MLIS** for model deployment.           |

Demos bound to a specific vertical, or which require provided data to be run (not runnable with your own data), live in their own folder:

### [Vertical demos](vertical-demos)

The [vertical demos folder](vertical-demos) contains demos bound to a specific vertical, usually providing their own data.

It contains the following demos:

| Demo | Short Description | Demo Video |
|---|---|---|
| [Molecular Aligned Multi-Modal Architecture and Language (Biomed-MAMMAL)](vertical-demos/biomed-mammal) | A **BentoML** inference service for a **biomedical foundation model** which achieves state-of-the-art results over a variety of tasks across the entire **drug discovery** pipeline and diverse **biomedical domains**. | - |
| [Blood Vessel Geometry Analysis and Reconstruction](vertical-demos/blood-vessel-geometry-analysis-and-reconstruction) | A streamlit application relying on **NVIDIA Vista 3D model** (deployed using **MLIS**) to analyze, reconstruct and render vessels in 3D. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Enhancing%20Healthcare%20with%20AI_%20Blood%20Vessel%20Analysis%20and%203D%20Reconstruction(1).mp4) |
| [Defence Ops](vertical-demos/defence-ops) | A web application leveraging a VLM (deployed using **MLIS**)to analyze videos, with preloaded defence-related ones provided for example. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/DefenceOps.mp4) |
| [Genome Sequencing](vertical-demos/genome-sequencing) | **Notebooks** leveraging **NVIDIA Parabricks** for genome sequencing. | - |
| [Hospital Visit Summary](vertical-demos/hospital-visit-summary) | A **streamlit application** that can display patient information regarding their previous visits from a database, and summarize it. Requires deploying an LLM using **MLIS**. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/PatientVisitSummariesApp.mp4) |
| [Lawfirm Co](vertical-demos/lawfirm-co) | An application using RAG and video analytics in the context of legal documents analysis. Requires deploying a VLM and embedding model using **MLIS**. | - |
| [License Plate Number Detection](vertical-demos/license-plate-number-detection) | An application using an object detection model (YOLO) and an OCR one to extract license plate numbers from videos. Uses **MLIS** for model deployment. | - |
| [Maintenance Ticket Assistant](vertical-demos/maintenance-ticket-assistant) | An application that can classifies tickets and provide expected resolution steps using a chat model. Also uses OCR to analyze text from network equipment photos for diagnostic purposes. Relies on **MLIS** for model deployment. | - |
| [Predictive Maintenance](vertical-demos/predictive-maintenance) | A predictive maintenance model trained leveraging **Jupyter Notebook**, tracked in **MLFlow**, packaged with BentoML and deployed via **MLIS**. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/predictive-maintenance-demo.mp4) |
| [Secure Loan Verification Demo](vertical-demos/secure-loan-verification-demo) | A **governed, human-in-the-loop AI workflow** for loan renewal: an agent gathers credit data from five bank systems via a governed **MCP** server and drafts a decision memo; a policy gate escalates large or risky cases to a human approver, and every step is audited. LLM served via **MLIS** or LiteLLM; includes a React portal and approval-email flow. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Slvd%20Demo.mp4) |
| [Traffic Report](vertical-demos/traffic-report) | A **streamlit** application that uses a VLM and YOLO to detect vehicles in images/videos and provide an analysis of the scenes. Relies on **MLIS** for model deployment. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/traffic-report-demo.mp4) |
| [Water Utility Planner](vertical-demos/water-utility-planner) | A chat assistant in charge of predicting which sewer pipes require inspection and why, requiring XGBoost model training with **Jupyter Notebooks**, tracking with **MLflow**, packaging with BentoML, deployment with **MLIS**, using **Open WebUI** for interaction. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Water%20Utility%20Agentic%20Planner%20-%20Short%20version.mp4) |

Outdated demos that we no longer support, and/or miscelleanous demos that do not fit into the other categories, are gathered under:
- **Archived_demos**: [archived-demos](archived-demos).

## Upcoming changes

The following demos will be updated:
- **Finetune Tool Calling LLM**

New demos are being considered:
- **Model Monitoring**
- **RAG**

## Contributions

We welcome demo contributions, see [CONTRIBUTING](CONTRIBUTING.md) for more details.

