<div align=center>
<img src="https://raw.githubusercontent.com/hpe-design/logos/master/Requirements/color-logo.png" alt="HPE Logo" height="100"/>
</div>

# HPE Private Cloud AI

##  AI Solution Use Case Demos

This repository contains use case demos developed for Private Cloud AI (PCAI). 

### Primary demos

The most generic, vertical-agnostic demos, implementing some of the most recurrent use cases are found are the root level of this repo.

These are the following:

| Demo                                 | Short Description          | Demo Video      | 
| -------------------------------------|----------------------------|-----------------|
| [Basic Agent Langflow](basic-agent-langflow)              | A **Langflow** setup defining a basic agentic flow to answer questions requiring informations from both local files, using RAG, and data from a SQL database. Relies on **MLIS** for model deployment. **MCP server** usage optional.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/MCP%20Customer%20Flight%20Support%20Agent%20Short.mp4) |
| [Base Code Assistant - Opencode](basic-code-assistant-opencode)                          | An explanation on how to setup and use **Opencode**, an open source AI coding agent, in a **VS Code server**, leveraging models deployed using **MLIS**. Includes an optional step to leverage **GitHub MCP server**.            | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/basic-opencode-demo-video.mp4) |
| [Conversation Toolbox](conversation-toolbox-demo)                          | A custom web application connecting to a chat model, an ASR model and Fish Audio S2 pro TTS model, deployed using **MLIS**, to provide a multilingual AI voice assistant, as well as file transcriptions capabilities. Voice Assistant accepts connections to **MCP servers** to enrich its capabilities.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/ConversationToolboxDemo.mkv) |
| [Finetune Tool Calling LLM](finetune-tool-calling-llm)                | **Notebooks**, using **Nemo microservices** to fine-tune an LLM to improve its tool-calling capabilities.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Finetune_llm_tool_call_short.mp4) |
| [Image Generation - ComfyUI](image-generation-comfyui)                | An explanation of how to simply use **ComfyUI**, an AI creation engine enabling powerful media creation AI workflows, such as, but not limited to **image generation**, **image editing** and **video generation**.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/comfyui-demo.mp4) |
| [Image Segmentation](image-segmentation)                      | Python scripts to fine-tune CNNs for segmentation tasks on provided datasets, expected to be executed in a **Jupyter notebook**, with experiment tracking on **MLflow**. Also includes a streamlit application to display segmentation results from any checkpoint saved, on any dataset image.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/image_segmentation_demo_video.mp4) |
| [Multimodal RAG](multimodal-rag)                        | An advanced retrieval-augmented generation (RAG) flow, supporting multiple files modalities, including text, images, audio and video, coming with its own **MCP server** to easily reuse this RAG flow elsewhere. Includes steps on how to use it with **Open WebUI** and **Opencode**.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/MultimodalRag.mkv) |
| [NL to SQL](nl-to-sql)                        | An **Open WebUI** setup to allow chatting with SQL data, leveraging tools from an **MCP server** to interact with data from a Postgres database. Relies on **MLIS** for model deployment.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/NL2SQL%20MCP.mp4) |
| [Object Detection - YOLO](object-detection-yolo)                        | A simple **streamlit application** running object detection inference using a **YOLO model** on images and videos it takes as input.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/object-detection-demo.mp4) |
| [Text Document Analysis](text-document-analysis)                | A simple web application in which users can upload text and PDF files, ask or upload a list of questions and get answers for each document in an Excel sheet, after document analysis leveraging an LLM deployed using **MLIS**.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/BAR_demo_video.mp4) |
| [Vision Analytics](vision-analytics)                        | A Gradio application using a VLM to analyze images, videos and/or streams. Files can be uploaded from the UI, or read from the filesystem. Relies on **MLIS** for model deployment.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/vision_analytics_pcai.mp4) |

### [Vertical demos](vertical-demos)

The [vertical demos folder](vertical-demos) contains demos bound to a specific vertical, usually providing their own data.

It contains the following demos:

| Demo                                 | Short Description          | Demo Video      | 
| -------------------------------------|----------------------------|-----------------|
| [Molecular Aligned Multi-Modal Architecture and Language (Biomed-MAMMAL)](vertical-demos/biomed-mammal) | A **BentoML** inference service for a **biomedical foundation model** which achieves state-of-the-art results over a variety of tasks across the entire **drug discovery** pipeline and diverse **biomedical domains**. | - |
| [Blood Vessel Geometry Analysis and Reconstruction](vertical-demos/blood-vessel-geometry-analysis-and-reconstruction)              | A streamlit application relying on **NVIDIA Vista 3D model** (deployed using **MLIS**) to analyze, reconstruct and render vessels in 3D. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Enhancing%20Healthcare%20with%20AI_%20Blood%20Vessel%20Analysis%20and%203D%20Reconstruction(1).mp4) |
| [Defence Ops](vertical-demos/defence-ops)                          | A web application leveraging a VLM (deployed using **MLIS**)to analyze videos, with preloaded defence-related ones provided for example.             | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/DefenceOps.mp4) |
| [Genome Sequencing](vertical-demos/genome-sequencing)                | **Notebooks** leveraging **NVIDIA Parabricks** for genome sequencing.           | - |
| [Hospital Visit Summary](vertical-demos/hospital-visit-summary)                      | A **streamlit application** that can display patient information regarding their previous visits from a database, and summarize it. Requires deploying an LLM using **MLIS**.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/PatientVisitSummariesApp.mp4) |
| [Lawfirm Co](vertical-demos/lawfirm-co)                        | An application using RAG and video analytics in the context of legal documents analysis. Requires deploying a VLM and embedding model using **MLIS**.          | - |
| [License Plate Number Detection](vertical-demos/license-plate-number-detection)                        | An application using an object detection model (YOLO) and an OCR one to extract license plate numbers from videos. Uses **MLIS** for model deployment.| - |
| [Maintenance Ticket Assistant](vertical-demos/maintenance-ticket-assistant)                        | An application that can classifies tickets and provide expected resolution steps using a chat model. Also uses OCR to analyze text from network equipment photos for diagnostic purposes. Relies on **MLIS** for model deployment.| - |
| [Predictive Maintenance](vertical-demos/predictive-maintenance)                        | A predictive maintenance model trained leveraging **Jupyter Notebook**, tracked in **MLFlow**, packaged with BentoML and deployed via **MLIS**.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/predictive-maintenance-demo.mp4) |
| [Traffic Report](vertical-demos/traffic-report)                        | A **streamlit** application that uses a VLM and YOLO to detect vehicles in images/videos and provide an analysis of the scenes. Relies on **MLIS** for model deployment.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/traffic-report-demo.mp4) |
| [Water Utility Planner](vertical-demos/water-utility-planner)                        | A chat assistant in charge of predicting which sewer pipes require inspection and why, requiring XGBoost model training with **Jupyter Notebooks**, tracking with **MLflow**, packaging with BentoML, deployment with **MLIS**, using **Open WebUI** for interaction. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Water%20Utility%20Agentic%20Planner%20-%20Short%20version.mp4) |

### [Misc demos](misc-demos)

The [misc demos folder](misc-demos) contains demos that are neither implementing a solution to a common use case, nor bound to a specific vertical. They may or may not partially overlap with primary demos, implement an uncommon use case, or just be custom apps built as pure technical demos. 

It contains the following demos:

| Demo                                 | Short Description          | Demo Video      | 
| -------------------------------------|----------------------------|-----------------|
| [AI Vulnerability Scanner](misc-demos/ai-vulnerability-scanner)            | An AI-powered scanner that crawls a bundled **OWASP Juice Shop** app and uses an LLM (deployed via **MLIS**) to analyze each page like a pentester, surfacing ranked, remediated findings in a live dashboard. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/AI%20Vulnerability%20Scanner%20Demo.mp4) |
| [Agentic Meetings Simulations](misc-demos/agentic-meetings-simulations)              | A custom web application that simulates company meetings using agentic AI workflows. Relies on **MLIS** for model deployment.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Meeting-agentic-simulation.mp4) |
| [Offline Meeting Transcription](misc-demos/offline-meeting-transcription)                | A transcription pipeline converting raw audio recordings into speaker-attributed transcripts and structured meeting minutes, using Whisper (for ASR, deployed on **MLIS**) and **Pyannote** (for speaker diarization) connected to **Open WebUI**.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Offline_meeting_transcription.mp4) |
| [Onboarding Buddy](misc-demos/onboarding-buddy)                        | A mock application to help organizations streamline onboarding for new hires, with task management by admin users and an AI Q&A assistant for the new hires. Uses **MLIS** for model deployment.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Onboarding%20Buddy%20Recording.mp4) |
| [Realtime Live Voice Translation](misc-demos/realtime-live-voice-translation)                | A custom web application that captures the user's voice and provides transcription and translation in real time. Relies on Whisper ASR model and a generic LLM deployed on **MLIS**.| - |
| [Voice Agent XTTS](misc-demos/voice-agent-xtts)                        | A custom Gradio application that connects to a chat model, Whisper for STT and XTTS-v2 for TTS, all deployed on **MLIS**, to provide a conversational assitant, able to discuss with the user in many different languages. Also includes a "chat with SQL data" scenario.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Voice_Agent.mp4) |


### [Archived demos](archived-demos)

The [archived demos](archived-demos) folder contains outdated demos, usually rendered obsolete by newer demos. While these demos may still run fine on newer PCAI instances, we do no longer support them. They are provided for reference only.

It contains the following demos:

| Demo                                 | Short Description          | Demo Video      | 
| -------------------------------------|----------------------------|-----------------|
| [AI Support Assistant](archived-demos/ai-support-assistant)                          | A mock support application relying on **Open WebUI** built-in RAG capabilities and **Airflow**. Relies on **Ollama** for model deployment.             | - |
| [Coding Assistant](archived-demos/coding-assistant)                          | A setup using **MLIS** for model deployment, **Open WebUI** to define a custom pipeline using that model, and the **VScode extension Continue.dev** using that pipeline to act as code assistant.            | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/AI-coding-assistant.mp4) |
| [Live Stream Frame Analytics](archived-demos/live-stream-frame-analytics)                      | A **Gradio application** for analyzing multiple, real-time video streams using a Vision Language Model (VLM) deployed on **MLIS**.| - |
| [Media Database SQL RAG](archived-demos/media-database-sql-rag)                        | Helm chart to deploy **Vanna AI**, a tool that relies on an LLM to convert natural language questions into SQL queries, enabling chatting with SQL data. **MLIS** can be used to deploy the LLM.          | - |
| [Voice Agent MagpieTTS](archived-demos/voice-agent-magpietts)                        | An older demo based on a custom **Gradio application** that connects to a chat model, parakeet-ctc-1.1b-asr for STT and magpie-tts-multilingual for TTS, all deployed on **MLIS**, to provide a conversational assitant.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Voice-Audio-Agent-demo.mp4) |
| [Voice Agent Open WebUI](archived-demos/voice-agent-openwebui)                        | An **Open WebUI** setup using a chat model, Whisper for STT and Chatterbox for TTS, deployed on **MLIS**, to allow voice-to-voice chatting with the chat model, in many different languages. Includes instructions for chatting with SQL data as well.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/multilingual_voice_demo_combined.mp4) |

## Upcoming changes

The following demos will be updated:
- **Finetune Tool Calling LLM**

New demos are being considered:
- **Model Monitoring**
- **New agentic demo**


## Contributions

We welcome demo contributions, see [CONTRIBUTING](CONTRIBUTING.md) for more details.

