# Multilingual Voice Agent Demo


| Owner                 | Name              | Email                              |
| ----------------------|-------------------|------------------------------------|
| Use Case Owner        | Tanguy Pomas      | tanguy.pomas@hpe.com               |
| PCAI Deployment Owner | Tanguy Pomas      | tanguy.pomas@hpe.com               |


## Abstract

This demo mainly aims to prove that PCAI can host end-to-end voice agent solutions that not only can speak English, but any other language, as long these languages are supported by both the chat model, a Speech-To-Text (STT) model and a Text-To-Speech (TTS) model.
Hence, the focus of this demo is not centered on the end application, but rather on the capabilities of the deployed models, defaulting to Open WebUI as application interface.

To enrich this demo, and go beyond simply adding STT and TTS to a standard chatbot, we provide optional steps for connecting the chat model to a SQL Database. Alternatively, enriching the chat knowledge with standard text documents is also available out-of-the-box using Open WebUI built-in RAG features.

This demo features:
* HPE Machine Learning Inference Software (MLIS) to deploy the following models:
  - [openai/whisper-large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo) as STT model
  - [Qwen/Qwen3-30B-A3B-Instruct-2507-FP8](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8) as chat model (text-to-text)
  - [chatterbox](https://github.com/resemble-ai/chatterbox) as TTS model
* [Open WebUI](https://docs.openwebui.com/) as chat / voice application interface
* Multilingual support from both Whisper STT and Chatterbox TTS models
* Additional voice creation for Chatterbox TTS model
* Language selection for Whisper transcriptions, as well as voice selection for Chatterbox generated speech within Open WebUI interface
* Options to enrich the chat answers with basic RAG and/or SQL data 
* (Optional) SQL example using an edited version of a [Saudi Real Estate Kaggle Dataset](https://www.kaggle.com/datasets/lama122/saudi-arabia-real-estate-aqar), for chatting in Arabic (or not)
* (Optional) MCP server providing tools for exploring SQL databases to the chat model 

Theoretically supported languages:
* To be listed

Recordings:
- None for the moment


## Description

*The two sections below are mandatory. Other sections can be added at will to accomodate the content.*

### Overview

*Describe the demo here, trying to be specific and detailed. At least one general diagram is mandatory and should depict all actors/components/entities that participate to the demo. Looking at this diagram the audience should be able to understand the demo.*

### Workflow

*Describe the workflow of the demo from an execution standpoint. Highlight any source of data, any processing in between and any place where the data will land in its final form. Also describe each component and the role/transformation it has/does. At least one diagram is mandatory and keep in mind that these could be shown to customers so keep it clear.*


## Deployment


### Prerequisites

* One GPU is needed for each model to deploy, **three GPUs in total**. For reference, all models chosen for this demo can fit in L40S
* Open WebUI must be imported

### Installation and configuration

* Import Open WebUI:
  * Using the latest helm chart at your dispoal in our [Frameworks](https://github.com/ai-solution-eng/frameworks/tree/main/open-webui) repo. No change in the values is needed.
* Deploy the three models using MLIS:
  * MLIS configuration for **Qwen/Qwen3-30B-A3B-Instruct-2507-FP8**:
    * Registry: None
	* Image: vllm/vllm-openai:latest
	* Resources:
	  * CPU: 4 to 6 (flexible, lower values may work)
	  * Memory: 40Gi to 60Gi (flexible, lower values may work)
	  * GPU: 1 to 1 (mandatory)
	* Arguments: --model Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 --enable-auto-tool-choice --tool-call-parser hermes --port 8080 --max-model-len 32768	
	* Environment variables (optional):
	  * AIOLI_DISABLE_LOGGER to 1
	  * AIOLI_PROGRESS_DEADLINE to 10000s
  * MLIS configuration for **openai/whisper-large-v3-turbo**:
    * Registry: None
	* Image: tpomas/vllm-audio:0.11.0 *See Note 1
	* Resources:
	  * CPU: 1 to 1
	  * Memory: 10Gi to 10Gi
	  * GPU: 1 to 1 (mandatory)
	* Arguments: --model openai/whisper-large-v3-turbo --port 8080
	* Environment variables (optional):
	  * AIOLI_PROGRESS_DEADLINE to 10000s
  * MLIS configuration for **Chatterbox TTS model**:
    * Registry: None
	* Image: tpomas/chatterbox-uv-gpu:0.0.1 *See Note 2
	* Resources:
	  * CPU: 8 to 8 (flexible, lower values may work)
	  * Memory: 40Gi to 80Gi (flexible, lower values may work)
	  * GPU: 1 to 1 (mandatory)
	* No additional argument needed
	* Environment variables:
	  * AIOLI_SERVICE_PORT to 4123 (mandatory)
	  * AIOLI_PROGRESS_DEADLINE to 10000s (optional)
	  
***Notes:
  1 [STT transcription API not being available in default vLLM image](https://docs.vllm.ai/en/latest/serving/openai_compatible_server/#transcriptions-api), a custom image is needed. tpomas/vllm-audio:0.11.0 has been created from vllm/vllm-openai:v0.11.0 with the addition of running pip install vllm[audio]**
  2 Chatterbox is neither vLLM-compatible, nor provide an Open AI API compatibility by default. tpomas/chatterbox-uv-gpu:0.0.1 has been built using "Dockerfile.uv.gpu" under the "docker" folder from (this GitHub repo)[https://github.com/travisvn/chatterbox-tts-api], that aims to make Chatterbox more easily deployable using Open AI API compatible endpoints.

* Add voices to Chatterbox (mandatory to generate non-English speech)
  A few minutes after Chatterbox has been deployed, you should be able to add additional voices for it, using short audio samples. Details for this procedure can be found in the [Chatterbox TTS API repo](https://github.com/travisvn/chatterbox-tts-api/blob/main/docs/MULTILINGUAL.md#2-upload-voice-with-language).
  For convenience, we are providing the following audio samples, under the deploy/data folder:
  * ARA_NORM_0002.wav, an Arabic audio sample coming from [this Kaggle dataset](https://www.kaggle.com/datasets/haithemhermessi/arabic-speech-corpus)
  * lupincontresholme_0009.wav, a French audio sample coming from [this Kaggle dataset](https://www.kaggle.com/datasets/bryanpark/french-single-speaker-speech-dataset)
  * meian_0000.wav, a Japanese audio sample coming from [this Kaggle dataset](https://www.kaggle.com/datasets/bryanpark/japanese-single-speaker-speech-dataset)
  
  Notes:
    * The latter two samples come from datasets that are part of the same dataset collection, called CSS10.
	* [CSS10 GitHub repository](https://github.com/Kyubyong/css10) can be a good source to quickly find other quality audio samples for other languages (German, Spanish, Finnish, Hungarian, ...)
	* Quality of provided is crucial to generate qualitative speech
  We also provide a simple notebook called "load_voices.ipynb" (under deploy/notebook) that contains the three commands used to add voices cloned from the three provided samples.
  In order to use it: 
  * Start a notebook server on PCAI
  * Upload load_voices.ipynb and the three audio samples into the same folder
  * Copy and paste your Chatterbox MLIS endpoints and API Token into each of the three notebook cells
  * Run each cell, adding a voice to Chatterbox that way is almost instantaneous
  * Voices called "arabic_speaker", "french_speaker" and "japanese_speaker" will be added to Chatterbox, and you will be able to select them when running the demo

* Connect models to Open WebUI:
  * Adding connection to Qwen3-30B-A3B-Instruct-2507-FP8:
    * As an Admin user on Open WebUI, go to Admin Panel -> Settings -> Connections
	* Add an OpenAI API connection to your Qwen MLIS deployment using its MLIS endpoints and API Token
  * Adding connection to Whisper and Chatterbox:
    * Still as an Admin user, go to Admin Panel -> Settings -> Audio
	* For both Speech-to-Text and Text-to-Speech sections, select "OpenAI" as Engine, then fill in:
	  * For the STT section: Your Whisper MLIS deployment endpoint, its API token, and set the STT Model to openai/whisper-large-v3-turbo
	  * For the TTS section: Your Chatterbox MLIS deployment endpoints and its API token. You can leave tts-1 or tts-1-hd as TTS model, and leave alloy as TTS Voice
	* Take note of the "Response Splitting" value

* (Recommended) Create a custom chat model using prompt engineering:
  * You may want to customize the prompt used by the chat model to avoid outputting numbers in digits (see related Chatterbox limitation)
  * Go to Workspace -> Models -> + New Model
  * Give a custom name to your new model (e.g. Voice Qwen) and select Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 as your Base Model
  * In "System Prompt", give detailed instructions that the chat model should follow to avoid outputting digits in its answer.
    * You can copy-paste the relevant passage of the prompt provided for the Saudi Real Estate example from deploy/data/prompt.txt
	* It is worth customizing that prompt, for example providing additional examples of good output vs bad output in the language you plan to run the demo with.
  * Click on the "Save&Create" button

* (Optional) Loading Saudi Real Estate dataset as a SQL Database, and provide the chat model the ability to query it
  * Do this step only if you are interested in a Real Estate use case and/or the ability to have the chat model leverage data from a SQL DB
  * To be added soon

* Run the demo  
  
## Running the demo

* Go to Open WebUI, and select Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 (or your model with custom prompt) as your chat model
* To customize the language in which whisper will transcribe your speech, and the chatterbox voices:
  * Go to your user settings -> Audio
  * STT Settings -> Language dictates the whisper language. Expected value is the language two-letter code, defaults to "en" for "English". Provided example is "ar" for "Arabic"
  * TTS Settings -> Set Voice refers to the Chatterbox voice to use. The list of available voices will NOT be displayed, but you can write down the name of the voice you want to use. If no voice is found with that name, Chatterbox will default to the Alloy English voice.
* Relevant options to enable/leave disabled:
  * Enabling "Instant Auto-Send After Voice Transcription" will automatically send your query to the chat model once it's transcribed by Whisper when using the "Dictate" button. It has no effect when using the "Voice mode" button.
  * Enabling "Auto-playback response" will automatically send the chat response to Chatterbox and play the audio once it is ready. It has no effect when using the "Voice mode" button.
* Ask your question to the chat model:
  * Click on "New Chat", and then the second button on the right, "Dictate"
  * Allow Open WebUI to access your mic if prompted
  * Ask your question verbally
  * Click on the right tick button when you are done. Your speech will be send to whisper for transcription.
* Check the transcription:
  * If "Instant Auto-Send After Voice Transcription" has been left disabled, transcription will appear where you usually type your queries. You can review the transcription and decide to erase it, edit it, or send it. It is no different than if you had typed this transcription yourself.
  * If "Instant Auto-Send After Voice Transcription" has been enabled, transcription will immediately be send to the chat model, as if you typed your question and pressed enter.
* Listen to the answer:
  * Wait for the chat answer to appear.
  * If "Auto-playback response" has been left disabled, you will have to press the little "Read aloud" button below the chat response to generate the audio using the specified Chatterbox voice
  * If "Auto-playback response" has been enabled, you won't have to do anything else, but audio will only start being generated once the entire chat response is generated.

* **DO NOT USE VOICE MODE** (rightmost button)

## Limitations

* Chatterbox voices **cannot pronounce numbers written with digits** properly (English voices might be able to, but not in other languages). If you intend to have chatterbox voices pronounce numbers, you will have to get the chat model to output numbers in words instead. This can be done with prompt engineering.
* **!!Issue to investigate!!**This Chatterbox deployment **cannot handle concurrent requests**. As a consequence, it is more than likely to fail if multiple persons are running this demo at the same time
