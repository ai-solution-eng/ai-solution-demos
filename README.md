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
| [Flight CS Agent](flight-customer-service-agent)              | An **Langflow** setup defining a basic agentic flow to answer questions requiring informations from both local files, using RAG, and data from a SQL database. Relies on **MLIS** for model deployment.           |
| [Coding Assistant](coding-assistant)                          | A setup using **MLIS** for model deployment, **Open WebUI** to define a custom pipeline using that model, and the **VScode extension Continue.dev** using that pipeline to act as code assistant.            |
| [Batch Agreement Robot](batch-agreement-robot)                | A simple custom application in which users can upload text and PDF files, ask or upload a list of questions and get answers from each document in Excel format.           |
| [Image Segmentation](image-segmentation)                      | Python scripts to fine-tune CNNs for segmentation tasks on provided datasets, expected to be executed in a **Jupyter notebook**, with experiment tracking on **MLflow**. Also includes a streamlit application to display segmentation results from any checkpoint saved, on any dataset image.           |
| [Voice Audio Agent](voice-audio-agent)                        | A custom Gradio application that connects to a chat model, Whisper for STT and XTTS-v2 for TTS, all deployed on **MLIS**, to provide a conversational assitant, able to discuss with the user in many different languages. Also includes a "chat with SQL data" scenario.           |
| [NL to SQL MCP Manufacturing](nl-to-sql-mcp-manufacturing)                        | An **Open WebUI** setup to allow chatting with SQL data, leveraging tools from an MCP server to interact with data from a Postgres database. Relies on **MLIS** for model deployment.           |
| [Vision Analytics](vision-analytics)                        | A Gradio application using a VLM to analyze images, videos and/or streams. Files can be uploaded from the UI, or read from the filesystem. Relies on **MLIS** for model deployment.           |

The remaining demos are split between two folders:
- **vertical_demos**: Demos bound to a specific vertical, or which require provided data to be run (not runnable with your own data).
- **archived_demos**: Outdated demos that we no longer support and/or miscelleanous demos that do not fit into the other categories.

## Upcoming changes

The following demos will be updated:
- **Coding Assistant**
- **Flight CS Agent**
- **Voice Agent**

New demos are being considered:
- **Model Monitoring**
- **Object Detection**
- **RAG**
- **Model training / LLM finetuning**

## Contributions

We welcome demo contributions, see [CONTRIBUTING](CONTRIBUTING.md) for more details.

