<div align=center>
<img src="https://raw.githubusercontent.com/hpe-design/logos/master/Requirements/color-logo.png" alt="HPE Logo" height="100"/>
</div>

# HPE Private Cloud AI

##  AI Solution Use Case Demos

This repository contains use case demos developed for Private Cloud AI (PCAI)

| Use Case                                                      | Docs          | Video         | Code          | Tags                                      |
| --------------------------------------------------------------|---------------|---------------|---------------|-------------------------------------------|
| [Flight CS Agent](flight-customer-service-agent)              | Yes           | Yes           | Yes           |`GenAI`, `agentic`,`RAG`, `Langflow`       |
| [Media DB SQL RAG](media-database-sql-rag)                    | Yes           | No            | Yes           |`GenAI`, `SQL`,`RAG`, `vanna.ai`           |
| [VLM on RTSP](live-stream-frame-analytics)                    | Yes           | Yes           | Yes           |`GenAI`, `Vision-Language`                 |
| [Coding Assistant](coding-assistant)                          | Yes           | Yes           | Yes           |`GenAI`, `Coding`, `Continue.dev`          |
| [Onboarding Buddy](onboarding-buddy)                          | Yes           | No            | Yes           |`GenAI`, `Chatbot`                         |
| [License Plate Detection](license-plate-number-detection)     | Yes           | No            | Yes           |`Computer Vision`,`Fine-tune`,`YOLO`       |
| [Genome Sequencing](genome-sequencing)                        | Yes           | No            | Yes           |`Genomic`,`NVIDIA`, `Parabricks`           |
| [Batch Agreement Robot](batch-agreement-robot)                | Yes           | No            | Yes           |`GenAI`, `Document Processing`             |
| [Traffic Report](traffic-report)                              | Yes           | Yes           | Yes           |`GenAI`, `Computer Vision`, `VLM`          |
| [Predictive Maintenance](predictive-maintenance)              | Yes           | Yes           | Yes           |`Maintenance`, `GenAI`, `LLM`, `VLM`       |
| [Blood Vessels Geometry Analysis and 3D Reconstruction](blood-vessel-geometry-analysis-and-reconstruction)              | Yes           | Yes           | Yes           |`Computer Vision`, `nvidia NIM vista-3d`, `3d rendering`, `streamlit`     |
| [Image Segmentation](image-segmentation)                      | Yes           | No            | Yes           |`Computer Vision`, `Fine-tune`, `CNN`, `streamlit`, `MLflow`      |
| [Fine-tuning LLM for Tool Calling](finetune-tool-calling-llm) | Yes           | Coming Soon   | Yes           |`Computer Vision`, `Fine-tune`, `CNN`, `streamlit`       |
| [Voice Audio Agent](voice-audio-agent)                        | Yes           | Yes           | Yes           |`GenAI`, `ASR`,`NVIDIA`                    |
| [Hospital Visit Summary](hospital-visit-summary)              | Yes           | Yes           | Yes           |`GenAI`, `SQL`, `LLM`                    |
| [Multilingual Voice Audio Agent](multilingual-voice-agent/doc)              | Yes           | Yes           | Yes           |`GenAI`, `Chatbot`, `ASR`, `TTS`, `SQL`                |
| [Simple RAG Chatbot With LangGraph ](simple-rag-chatbot-with-langgraph/README.md)  | Work in progress           | Coming Soon           | Yes           |`GenAI`, `Chatbot`, `LangGraph`, `Qdrant`                |

### Upcoming

| Use Case                                                      | Docs          | Video         | Code          | Tags                                      |
| --------------------------------------------------------------|---------------|---------------|---------------|-------------------------------------------|
| [Core Custom RAG]()                                           | Coming soon   | Coming soon   | Coming soon   |`GenAI`,`RAG`                              |
| [Core Fine-tuning]()                                          | Coming soon   | Coming soon   | Coming soon   |`GenAI`,`Fine-tune`                        |
| [Core Agentic]()                                              | Coming soon   | Coming soon   | Coming soon   |`GenAI`,`Agentic`                          |

## How to Contribute

We welcome all contributions to the repository. If you have updates/fixes to existing demos or net new PCAI validated use case demos you would like to submit, please create a PR.

### Guidelines

Here are the guidelines that contributors should follow to add new demos.

- Each demo has its own folder into this repository. The name of the folder must be **max 32 characters long**

- Inside each demo's folder, there must be a README.md file containing the demo documentation. Please, copy the **README-template.md** file to have the proper structure. This file also contains guidelines on how to write the documentation

- Demo recordings must be sent to us (Andrea, Hoang) because they will be copied into our cloud storage .**SharePoint is not allowed** to store recordings as it expires and needs privileges that customers don't have

- The demo's folder **must not contain** any temporary file. Examples of files and folders that must be avoided are:
    - node_modules
    - .ipynb_checkpoints
    - .vite
    - .venv
    - .idea

- The demo's folder **must not contain** charts of frameworks that needs to be imported for the demo. These charts must be added to [this](https://github.com/ai-solution-eng/frameworks) repository and proper installation instructions must added to the README.md file

- Once finished, add one line to this README.md (the able above) with its details 

### Structure of the demo folder

The demo's folder must have the structure depicted below.

```
<demo>
├── deploy
│   ├── chart
│   ├── config
│   ├── data
│   └── notebook
├── doc
├── example
└── source
    ├── chart
    ├── code
    └── docker
```

- **deploy** : any deploy file goes here, in particular:
    - **chart** : put here the helm chart and a logo image that will be used during the import of the framework. If there are more charts (i.e. a frontend and a backend), please create a sub folder for each one. Each time the source code is updated, the chart need to be rebuilt and put here
    - **config** : put here all configuration files (yalm, json, etc...) that will be used during deployment
    - **data** : folder for data to be used during the demo (documents, images). Large files (i.e. bigger than 5MB) should be sent to us and will be copied into our cloud storage. A reference will be added to the README.md file
    - **notebook** : all notebooks that are required during the demo go here, together with any referenced file (like python source code, requirements.txt, sample images). Notebooks will need to download all big files remotely from our cloud storage or copied by hand
- **doc** : documentation goes here. As most of the doc will be inside the README.md file, here we will have just the referenced images and a few more files. A big README.md can be split into sections: in this case, each section file must be put here
- **example** : this folder is meant to store any result produced by the demo that can be of any interest to the audience, like generated images, logs etc... These files can be referenced from the README.md file
- **source** : 
    - **chart** : the unzipped helm chart of the demo goes here. If there are more charts, please create one subfolder for each one
    - **code** : all source code file (not referenced by any notebook) goes here together with any yaml or json file that is needed by the code itself and need to be put close to the code (like inside the same container)
    - **docker** : docker files needed to create containers for charts or models to deploy go here
