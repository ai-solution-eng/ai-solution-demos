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
* **HPE Machine Learning Inference Software (MLIS)** to deploy the following models:
  - [**openai/whisper-large-v3-turbo**](https://huggingface.co/openai/whisper-large-v3-turbo) as STT model
  - [**Qwen/Qwen3-30B-A3B-Instruct-2507-FP8**](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8) as chat model (text-to-text)
  - [**Chatterbox**](https://github.com/resemble-ai/chatterbox) as TTS model
* [**Open WebUI**](https://docs.openwebui.com/) as chat / voice application interface
* **Multilingual support** from both Whisper STT and Chatterbox TTS models
* Additional **voice creation** for Chatterbox TTS model
* Language selection for Whisper transcriptions, as well as voice selection for Chatterbox generated speech within Open WebUI interface
* Options to enrich the chat answers with basic **RAG and/or SQL data**
Optional features:
* **Chat with SQL data**, using an edited version of a [Saudi Real Estate Kaggle Dataset](https://www.kaggle.com/datasets/lama122/saudi-arabia-real-estate-aqar)
* **MCP server** to provide tools for the chat model to access the SQL data 

Supported languages:
* Arabic (ar)
* Danish (da)
* German (de)
* Greek (el)
* English (en)
* Spanish (es)
* Finnish (fi)
* French (fr)
* Hebrew (he)
* Hindi (hi)
* Italian (it)
* Japanese (ja)
* Korean (ko)
* Malay (ms)
* Dutch (nl)
* Norwegian (no)
* Polish (pl)
* Portuguese (pt)
* Russian (ru)
* Swedish (sv)
* Swahili (sw)
* Turkish (tr)
* Chinese (zh)

That list corresponds to languages supported by Chatterbox, Whisper can transcribe additional languages and the Qwen3 chat model can be swapped for another one if its performance for the chosen language are underwhelming.

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

* **Three GPUs** to deploy the three models. For reference, all models chosen for this demo can fit on **L40S GPUs**.
* (Optional) For chatting with SQL data: **EzPrestoMCP** installed (not publicly available as of now, may be available by default in future AIE releases, contact us for advice otherwise)

### Installation and configuration

* **Import Open WebUI**:
  * Using the latest helm chart at your dispoal in our [Frameworks](https://github.com/ai-solution-eng/frameworks/tree/main/open-webui) repo. No change in the values is needed.
* **Deploy the three models using MLIS**:
  
  * **Qwen/Qwen3-30B-A3B-Instruct-2507-FP8** MLIS configuration:
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
     
  * **openai/whisper-large-v3-turbo** MLIS configuration:
    * Registry: None
	* Image: tpomas/vllm-audio:0.11.0 **See Note 1 for details**
	* Resources:
	  * CPU: 1 to 1
	  * Memory: 10Gi to 10Gi
	  * GPU: 1 to 1 (mandatory)
	* Arguments: --model openai/whisper-large-v3-turbo --port 8080
	* Environment variables (optional):
	  * AIOLI_PROGRESS_DEADLINE to 10000s
     
  * **Chatterbox TTS model** MLIS configuration:
    * Registry: None
	* Image: tpomas/chatterbox-uv-gpu:0.0.1 **See Note 2 for details**
	* Resources:
	  * CPU: 8 to 8 (flexible, lower values may work)
	  * Memory: 40Gi to 80Gi (flexible, lower values may work)
	  * GPU: 1 to 1 (mandatory)
	* No additional argument needed
	* Environment variables:
	  * AIOLI_SERVICE_PORT to 4123 (mandatory)
	  * AIOLI_PROGRESS_DEADLINE to 10000s (optional)


     
* **Add voices to Chatterbox (mandatory to generate non-English speech)**
  * A few minutes after Chatterbox has been deployed, you should be able to add additional voices for it, using short audio samples. Details for this procedure can be found in the [Chatterbox TTS API repo](https://github.com/travisvn/chatterbox-tts-api/blob/main/docs/MULTILINGUAL.md#2-upload-voice-with-language).
  * For convenience, we are providing the following audio samples, under the deploy/data folder **(See Note 3 for details)**:
    * ARA_NORM_0002.wav, an Arabic audio sample coming from [this Kaggle dataset](https://www.kaggle.com/datasets/haithemhermessi/arabic-speech-corpus)
    * lupincontresholme_0009.wav, a French audio sample coming from [this Kaggle dataset](https://www.kaggle.com/datasets/bryanpark/french-single-speaker-speech-dataset)
    * meian_0000.wav, a Japanese audio sample coming from [this Kaggle dataset](https://www.kaggle.com/datasets/bryanpark/japanese-single-speaker-speech-dataset)
  * We also provide a simple notebook called **load_voices.ipynb, under deploy/notebook,** that contains the three commands used to add voices cloned from the three provided samples. In order to use it: 
    * Start a notebook server on PCAI
    * Upload load_voices.ipynb and the three audio samples into the same folder
    * Copy and paste your Chatterbox MLIS endpoints and API Token into each of the three notebook cells
    * Run each cell, adding a voice to Chatterbox that way is almost instantaneous
    * Voices called "arabic_speaker", "french_speaker" and "japanese_speaker" will be added to Chatterbox, and you will be able to select them when running the demo

* **Connect models to Open WebUI**:
  * Adding connection to Qwen3-30B-A3B-Instruct-2507-FP8:
    * As an Admin user on Open WebUI, go to Admin Panel -> Settings -> Connections
	* Add an OpenAI API connection to your Qwen MLIS deployment using its MLIS endpoints and API Token
    ![qwen_connection](images/qwen_connection.PNG)
  * Adding connection to Whisper and Chatterbox:
    * Still as an Admin user, go to Admin Panel -> Settings -> Audio
	* For both Speech-to-Text and Text-to-Speech sections, select "OpenAI" as Engine, then fill in:
	  * For the STT section: Your Whisper MLIS deployment endpoint, its API token, and set the STT Model to openai/whisper-large-v3-turbo
	  * For the TTS section: Your Chatterbox MLIS deployment endpoints and its API token. You can leave tts-1 or tts-1-hd as TTS model, and leave alloy as TTS Voice
    ![STT_TTS_connection](images/stt-tts_connection.PNG)
	* Take note of the "Response Splitting" value: either "Punctuation" and "Paragraph" values are OK 

* **(Recommended) Create a custom chat model using prompt engineering**:
  * You may want to customize the prompt used by the chat model to avoid outputting numbers in digits (see related Chatterbox limitation)
  * Go to Workspace -> Models -> + New Model
  * Give a custom name to your new model (e.g. Voice Qwen) and select Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 as your Base Model
  * In "System Prompt", give detailed instructions that the chat model should follow to avoid outputting digits in its answer.
    * You can copy-paste the relevant passage of the prompt provided for the Saudi Real Estate example from **deploy/data/sa-houses-prompt.txt**
	* It is worth customizing that prompt, for example providing additional examples of good output vs bad output in the language you plan to run the demo with.
  ![custom_prompt](images/custom_model_prompt.PNG)
  * Click on the "Save&Create" button

* **(Optional) Loading a CSV file as a SQL Database, and provide the chat model the ability to query it**
  * Follow this step only if you are interested in chatting with a SQL DB
  * Using the Saudi Real Estate dataset CSV file, made available in **deploy/data/Saudi_Arabia_houses.csv**
  * Make the CSV available in the right place:
    * In a notebook, go the shared folder, and, under that shared folder, create a new folder, a subfolder inside it, and upload your CSV file inside that subfolder.
    * Run chmod -R 777 on your folder. See example:
    ![folder_structure](images/folder_structure.PNG)
  * Add the CSV as a Data Source:
    * On AIE, go to Data Engineering -> Data Sources -> Structured Data -> Add New Data Source
    * Select Hive, and fill the following information:
      * Name: Any name you want (e.g. realestate)
      * Hive Metastore: Discovery
      * Data Dir: file:/data/shared/<YOUR FOLDER NAME>
      * File Type: CSV
    ![hive_connection](images/hive_connection.PNG)


* **Run the demo**  

	  
**Notes**:
  1. [STT transcription API not being available in the official vLLM images](https://docs.vllm.ai/en/latest/serving/openai_compatible_server/#transcriptions-api), a custom image is needed. tpomas/vllm-audio:0.11.0 has been created from vllm/vllm-openai:v0.11.0 with the addition of running pip install vllm[audio]. It is provided for convenience, but 
  2. Chatterbox is neither vLLM-compatible, nor provide an Open AI API compatibility by default. tpomas/chatterbox-uv-gpu:0.0.1 has been built using "Dockerfile.uv.gpu" under the "docker" folder from the (Chatterbox TTS API GitHub repo)[https://github.com/travisvn/chatterbox-tts-api], that aims to make Chatterbox more easily deployable using Open AI API compatible endpoints.
  3. The latter two samples come from datasets that are part of the same dataset collection, called CSS10. [CSS10 GitHub repository](https://github.com/Kyubyong/css10) can be a good source to quickly find other quality audio samples for other languages (German, Spanish, Finnish, Hungarian, ...). Quality of provided voice sample is crucial to generate qualitative speech.

## Running the demo

* Go to Open WebUI, and select Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 (or your model with custom prompt) as your chat model
* To customize the language in which whisper will transcribe your speech, and the chatterbox voices:
  * Go to your user settings -> Audio
  * STT Settings -> Language dictates the whisper language. Expected value is the language two-letter code, defaults to "en" for "English". Provided example is "ar" for "Arabic"
  * TTS Settings -> Set Voice refers to the Chatterbox voice to use. The list of available voices will NOT be displayed, but you can write down the name of the voice you want to use. If no voice is found with that name, Chatterbox will default to the Alloy English voice.
* Relevant options to enable/leave disabled:
  * Enabling "Instant Auto-Send After Voice Transcription" will automatically send your query to the chat model once it's transcribed by Whisper when using the "Dictate" button. It has no effect when using the "Voice mode" button.
  * Enabling "Auto-playback response" will automatically send the chat response to Chatterbox and play the audio once it is ready. It has no effect when using the "Voice mode" button.
![user_settings](images/user_settings.PNG)
* Ask your question to the chat model:
  * Click on "New Chat", and then the second button on the right, "Dictate"
![dictate_button](images/dictate_button.PNG)
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

* **DO NOT USE VOICE MODE**: this demo will be updated to support voice mode, and make it the default way to run it over using "Dictate". Supporting it will make getting the audio response faster, especially for long answers (see limitations for more details).

## Limitations

* Chatterbox voices **cannot pronounce numbers written with digits** properly (English voices might be able to, but not in other languages). If you intend to have chatterbox voices pronounce numbers, you will have to get the chat model to output numbers in words instead. This can be done with prompt engineering.
* **This Chatterbox deployment cannot handle concurrent requests**. As a consequence:
  * It is likely to fail if multiple persons are running this demo at the same time: some persons may not get any audio output, and the Chatterbox deployment may even crash and restart (voices will have to be re-added in that case)
  * It cannot handle Open WebUI Voice Mode: Voice Mode will spilt the chat response (by punctuation, or paragraph, based on the "Response Splitting" value in admin audio setting) **while it is being generated**, and immediately send each split as an independent request to Chatterbox. Chatterbox won't have time to process the first split before receiving the second one, and will fail to process the subsequent requests as well.
  * "Dictate" mode on the other hand will wait for the entire chat response to be generated before splitting it and sending each split one by one to Chatterbox: waiting to receive the audio for the first split before sending the second one, and so on, and so forth... Note that if there is a single split (one sentence, paragraph, or "Response Splitting" set to None), Voice Mode will work, but won't allow for faster audio response compare to "Dictate"

