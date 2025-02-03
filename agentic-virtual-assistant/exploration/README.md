# Overview of Agentic Systems Demo 
Authored by: Andrew Mendez, andrew.mendez@hpe.com, 2/3/2025

## Overview

This notebook is an exploration of agentic systems, and an overview of key components when developing agentic systems. We implement several agentic systems using Nvidia NIM, Langchain
To run the demo, you will need to complete the installation steps and the configuration steps.

## Requirements
* requires python3.11 if you want to install
* This agentic notebook requires LLMs that are capable of tool calling. Any NIM based model will work, and openAI endpoint will work. Not all MLIS endpoints will work if the model was downloaded from huggingface. Reach out to andrew.mendez@hpe.com if you would like to customize demo to run on a non NIM endpoint.

Note: tested on an M1 Macbook pro, should work on other OS, but will need to manually install pip packages

## Installation Steps 
* run the commands to create a virtual environment
    * `python3 -m venv demo-env`
    * `source demo-env/bin/activate`
    * `pip install -r requirements.txt`


## Configuration Steps

* Change <NV_API_KEY> to your own API key. Follow the guide here to create an NIM API key (link)[https://github.com/NVIDIA-AI-Blueprints/ai-virtual-assistant/tree/main?tab=readme-ov-file#obtain-api-keys]
* NOTE: the NV_API_KEY can be removed if you point the `NVIDIAEmbeddings` model to a NIM Embedding model running on MLIS. 

## Notebooks to run:
* `demo.ipynb`: the notebook that overviews agentic systems and several agentic architectures/workflows
* `final_graph_demo.ipynb`: this notebook is a standalone notebook that shows the final agentic system for ease of extending.

## (Optional): Additional Configuration options

* The `ChatNVIDIA` Langchain class makes it easy to point to any MLIS or VLLM endpoint, here is how to point to a new endpoint (in case the current endpoint is down):
    * change the base url to another MLIS endpoint, example: 
    * set the api key to an empty string
    * **NOTE: This agentic system requires
* Currently the notebook uses `ChatNVIDIA` as the llm agent. This can be swapped out with OpenAI if needed.
* `NVIDIAEmbeddings` can point to an MLIS NIM embedding model if needed. Currently the embedding is down