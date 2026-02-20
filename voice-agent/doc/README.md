# Multilingual Voice Agent Demo -> change name to what?


| Owner                 | Name              | Email                              |
| ----------------------|-------------------|------------------------------------|
| Use Case Owner        | Tanguy Pomas      | tanguy.pomas@hpe.com               |
| PCAI Deployment Owner | Tanguy Pomas      | tanguy.pomas@hpe.com               |


## Abstract


This demo aims to show a reactive conversational agent that can transcribe voice input from any language handled by Whisper, process it with a chat LLM (defaulting to Qwen3-30B-A3B-Instruct-2507-FP8) and provide an audio output in any language supported by XTTS-v2 (17 languages). 
One of the strong points of this demo is its **speed and fluidity**: it should take at most a couple of seconds before getting an answer back from the agent, and its answers can be interrupted with the user's voice, allowing for natural conversations.


To showcase the agent interacting with data on the platform, this demo includes the option to connect to a postgres database, create tables that include fake customer information related to mobile/internet subscription and have a discussion leveraging that data, the demo user impersonating one of the fake customers.


This demo features:
* **HPE Machine Learning Inference Software (MLIS)** to deploy the following models:
  - **whisper-large-v3** as STT model, either as a [NIM](https://build.nvidia.com/openai/whisper-large-v3) or using [vLLM](https://huggingface.co/openai/whisper-large-v3-turbo)
  - [**Qwen/Qwen3-30B-A3B-Instruct-2507-FP8**](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8) as chat model (text-to-text)
  - [**XTTS-v2**](https://huggingface.co/coqui/XTTS-v2) as TTS model
* A custom Gradio application to interact with those models
* **Natural, reactive conversations** with the chat model, with possibility to interrupt its answers
* **Multilingual support** from both Whisper STT and XTTS-v2 TTS models
* Additional **voice creation/voice cloning** for XTTS-v2 TTS model
* Language selection for Whisper transcriptions, as well as voice selection for XTTS-v2 generated speech within the Gradio interface

Optional features, if connection to DB enabled from the Gradio UI:
* **Agent to leverage data from a Postgres DB**, containing fake customer information: impersonate a customer making requests to the agent that has information regarding you, your subscription plan, tickets, analyzing your sentiment...
* **Data visualization**: either from the Gradio app, from PCAI data catalog, or even **build your own superset dashboard**

Supported languages* (see note):
* English (en)
* Spanish (es)
* French (fr)
* German (de)
* Italian (it)
* Portuguese (pt)
* Polish (pl)
* Turkish (tr)
* Russian (ru)
* Dutch (nl)
* Czech (cs)
* Arabic (ar)
* Chinese (zh-cn)
* Japanese (ja)
* Hungarian (hu)
* Korean (ko)
* Hindi (hi)

**Note**

That list corresponds to languages supported by XTTS-v2, Whisper can transcribe additional languages and the Qwen3 chat model can be swapped for another one if its performance for the chosen language is underwhelming.

In other words, you can expect to talk with the agent using languages that are not on this list, but the agent's responses will necessarily be in one of those languages.

Also note that **only English has been tested for dicussion with database connection enabled**, some hiccups are to be expected with any of the other 17 languages supported by XTTS.

**Recordings**:
* Coming soon


## Description

### Overview

The demo relies on Whisper to transcribe the demo user queries into text, a standard chat model (Qwen3-30B-A3B-Instruct-2507-FP8) for interpreting the queries and respond to them, and XTTS-v2 to generate an oral response from the text output. Those three models need to be deployed to MLIS, and the demo user is expected to connect them to Open WebUI to interact with them.

Input language (expected by Whisper for its transcription), as well as output language (chat model response's language, to generate audio from by XTTS) can be selected from the application UI.

Voice to be used for generating the audio response can also be selected from the UI: XTTS provide dozens of different voices by default, some are being more adapted to certain languages. XTTS-v2 supports voice cloning, and this demo allows for that: you can upload a clean voice sample to the application, and the cloned voice will be available for selection for future conversations with the agent.

Additional demo part involves connecting to a postgres database, creating a few tables, mimicking data that could correspond to customers for a mobile/internet provider. If connection to the database is enabled, standard chatbot discussion is disabled: all the conversation will be centered on that customer data, and the demo user is expected to impersonate a customer from the database.


#### Architecture Diagram

![architecture](images/architecture.PNG)

### Workflow

#### Basic Demo Workflow

The actual demo workflow is quite simple, open the gradio app, go the voice input and voice output tabs to:
* Have Whisper transcribe your questions in the language you plan to speak (in the Voice Input tab)
* Get answers from the language you want to hear as output (most likely identical to input language, but not necessarily) and select the voice you want the answer to be generated with (in the Voice Output tab)

Once done, go back to the Voice Chat tab, click on Conversation Mode, then on Start once you are ready to talk and on Stop when you no longer want the agent to listen and respond to you.
Saying anything while a response is getting played will interrupt the response and will make the agent generate another response to the latest thing you said. This will happen only when saying things loud enough and for long enough, so that parasite noises won't interrupt the agent otherwise.
The sound threshold above which the agent will actively listen and then respond to can be tuned with the Trigger Sensitivity bar. Lowering it will make your voice more easily detected in calm environments where you don't need to speak loudly, increasing it is better for noisy environments where you need to talk loudly and ignore significant background noise.


![workflow1](images/workflow1.PNG)

#### Demo Workflow with optional SQL Data

**Note: this part has not been tested with non-English languages**

If you are opting for the optional "agent with access to customer Database"/"DB connected mode" component of this demo, a few things change:
* Tick the box for enabling database connection
* Initialize tables if not done during the demo setup, or if tables where deleted
* Optionally, monitor the data from the database from:
	* AIE Data Catalog
	* Superset, after adding the created tables as new data sources

It is otherwise identical to chatting without access to SQL data.

![workflow2](images/workflow2.PNG)

## Deployment


### Prerequisites

* **Three GPUs** to deploy the three models. For reference, all models chosen for this demo can fit on **L40S GPUs**.
* (Optional) Access to a postgres database (to enable the database connected part of the demo). We recommend that you deploy it on the same PCAI instance using a [helm chart from our frameworks repo](https://github.com/ai-solution-eng/frameworks/tree/main/postgresql) 

### Installation and configuration

**1. Import the Gradio application**:
* Use the helm chart made available in the [chart folder](../deploy/chart) of this repo. No change in the values is needed.

**2. Deploy the three models using MLIS**:
  
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
     
  * **openai/whisper-large-v3-turbo** MLIS configuration:
    * Registry: None
	* Image: tpomas/vllm-audio:0.15.1
	* Resources:
	  * CPU: 1 to 1 (flexible)
	  * Memory: 10Gi to 10Gi (flexible)
	  * GPU: 1 to 1 (mandatory)
	* Arguments: --model openai/whisper-large-v3-turbo --port 8080
     
  * **XTTS-v2 model** MLIS configuration:
    * Registry: None
	* Image: tpomas/xtts-server:5.2.0
	* Resources:
	  * CPU: 4 to 16 (flexible, lower values may work)
	  * Memory: 16Gi to 32Gi (flexible, lower values may work)
	  * GPU: 1 to 1 (mandatory)
	* No additional argument needed
	* Environment variables:
	  * AIOLI_SERVICE_PORT to 8000 (mandatory)


**3. Connect models to Gradio application**:
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

**4. (Optional) Enable connection with Postgres Database**
  * Follow this step only if you are interested in chatting with a SQL DB
  * **This steps requires EzPrestoMCP** to be imported to your PCAI instance
  * You can use the custom Saudi Real Estate dataset CSV file, **Saudi_Arabia_houses.csv**, made available in the [**deploy/data**](../deploy/data) folder
  * Make the CSV available in the right place:
    * In a notebook, go the shared folder, and, under that shared folder, create a new folder, a subfolder inside it, and upload your CSV file inside that subfolder.
    * Run chmod -R 777 on your folder. See example:
    ![folder_structure](images/folder_structure.PNG)
  * Add the CSV as a Data Source:
    * On AIE, go to Data Engineering -> Data Sources -> Structured Data -> Add New Data Source
    * Select Hive, and fill the following information:
      * Name: Any name you want (e.g. realestate)
      * Hive Metastore: Discovery
      * Data Dir: file:/data/shared/YOUR FOLDER NAME
      * File Type: CSV
    ![hive_connection](images/hive_connection.PNG)
    * Make the newly created Data Source public by clicking on the three dots, then "Change to public access:"
    ![public_access](images/public_access.PNG)
  * Check that the CSV data has been successfully imported:
    * Click on "Data Catalog" -> Select your Data Source, default, then your table name should appear under the "All Datasets" section
    ![data_catalog](images/data_catalog.PNG)
    * Click on you table name, and you should have access to your dataset preview
    ![data_preview](images/data_preview.PNG)
    * Alternatively, you could go to Data Engineering -> Query Editor and add your dataset there in a similar fashion to run SQL queries on your data
  * Connect Open WebUI to the EzPrestoMCP server:
    * As an Open WebUI admin user, go to Admin Panel -> Settings -> External Tools -> Add Connection
    * Select Type as "MCP Streamable HTTP"
    * Copy-paste your MCP server URL in the URL field
    * Get a JWT token to paste in the Auth API Key field. You can get a JWT Token from a notebook instance with the command **cat /etc/secrets/ezua/.auth_token**. Note that, by default, **this token is valid only for 30 minutes, so you will need to refresh it**, unless its lifetime has been increased.
    * Fill ID, Name and Description fields however you like
  ![mcp](images/mcp.PNG)
  * Allow your chat model to use tools from that MCP server:
    * If you created a new model to customize its prompt, you can go back to edit this model (Workspace -> Models -> click on the logo next to your model name) and tick your newly added tools, under the Tools section, towards the bottom of that page:
    ![selected_tools](images/selected_tools.PNG)
      * Adding a tool this way will ensure the custom model will always have access to that tool, and you won't have to confirm connection to that toolset when using this model
    * If you have not created a new model with a custom prompt to bypass Chatterbox limitations to pronounce numbers written in digits, **you may still want a create a custom prompt to help the chat model generate better SQL queries**, to make a better use of tools available to it. In particular, describing at high level the database it has access to, and providing one or two examples of SQL queries will make it much more efficient at getting information from the provided CSV.
      * Check the example provided in [**deploy/data/sa-houses-prompt.txt**](../deploy/data/sa-houses-prompt.txt) for guidance.
      * If you use that example, double-check that its first few lines exactly corresponds to your database configuration: "realestate" is the source database, "default" the schema and "real_estate" the table. If you did not name your data source exactly "realestate" and/or the subfolder you created on the shared storage is not named exactly "real_estate", you will have to edit that part of the prompt, as the model will generate wrongly formatted SQL queries.
    * If you are not using a custom model, you will have to click on the "Integrations" button, at the right of the "+" sign below the place where you usually type your query, then Tools -> tick your tool. A wrench icon appears once the chat model is given access to the tool: 
    ![integration](images/integration.PNG)
      * You will have to do this every time you start a new chat

**5. Run the demo**  

	  
**Notes**:
  1. [Qwen/Qwen3-30B-A3B-Instruct-2507-FP8](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8) is just a suggestion that proved to work well for most, if not all languages supported by XTTS, but any OpenAI API compatible chat model could be used instead, as long as they supports the languages you plan to use the demo with.
  2. [STT transcription API not being available in the official vLLM images](https://docs.vllm.ai/en/latest/serving/openai_compatible_server/#transcriptions-api), a custom image is needed. tpomas/vllm-audio:0.15.1 has been created from vllm/vllm-openai:v0.15.1 with the addition of running pip install vllm[audio]. It is provided for convenience, you can build and use your own image instead.
  3. XTTS-v2 is neither vLLM-compatible, nor provide an Open AI API compatibility by default. The custom "tpomas/xtts-server:5.2.0" image has been built using the code available in the [xtts-server folder](../source/docker/xtts-server) made available in this repo.

## Running the demo

1. **Go to Open WebUI and select your model with custom prompt** (or Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 if you didn't customize the prompt)
2. **Select the language for Whisper, and the voice for Chatterbox**:
    * Go to your user settings -> Audio
    * STT Settings -> Language dictates the Whisper language. Expected value is the language two-letter code, defaults to "en" for "English". Provided example is "ar" for "Arabic". See "Supported Languages" at the top of this page if you have a doubt.
    * TTS Settings -> Set Voice refers to the Chatterbox voice to use. The list of available voices will NOT be displayed, but you can write down the name of the voice you want to use. If no voice is found with that name, Chatterbox will default to the Alloy English voice.
3. **Note these relevant options**:
    * Enabling "Instant Auto-Send After Voice Transcription" will automatically send your query to the chat model once it's transcribed by Whisper when using the "Dictate" button. It has no effect when using the "Voice mode" button.
    * Enabling "Auto-playback response" will automatically send the chat response to Chatterbox and play the audio once it is ready. It has no effect when using the "Voice mode" button.
![user_settings](images/user_settings.PNG)
4. **Ask your question to the chat model**:
    * Click on "New Chat", and then the second button on the right, "Dictate"
![dictate_button](images/dictate_button.PNG)
    * Allow Open WebUI to access your mic if prompted
    * Ask your question verbally
    * Click on the right tick button when you are done. Your speech will be send to whisper for transcription.
5. **Check the transcription**:
    * If "Instant Auto-Send After Voice Transcription" has been left disabled, transcription will appear where you usually type your queries. You can review the transcription and decide to erase it, edit it, or send it. It is no different than if you had typed this transcription yourself.
    * If "Instant Auto-Send After Voice Transcription" has been enabled, transcription will immediately be send to the chat model, as if you typed your question and pressed enter.
6. **Listen to the answer**:
    * Wait for the chat answer to appear.
    * If "Auto-playback response" has been left disabled, you will have to press the little "Read aloud" button below the chat response to generate the audio using the specified Chatterbox voice
    ![read_aloud](images/read_aloud.PNG)
    * If "Auto-playback response" has been enabled, you won't have to do anything else, but audio will only start being generated once the entire chat response is generated.
7. **(Optional) Check the SQL query and result**:
    * Clicking on the "1 Source" button and then on the MCP server + Tool name will give you access to the list of parameters used by the model with that tool (the SQL query) and its output
  ![check_query](images/check_query.PNG) 

**Note:**
* **DO NOT USE VOICE MODE**: this demo will be updated to support voice mode, and make it the default way to run it over using "Dictate". Supporting it will make getting the audio response faster, especially for long answers (see limitations for more details).

## Limitations

* It is possible to get no audio output for a response that is otherwise correctly generated on the backend. If that happens, try asking the same question again. You can also click on the "New Session" button or even refresh the page if the issue persists.
* DB Connection mode only works in English. It may also work in German, Spanish and French, but hasn't been thoroughly tested and will . It is not expected to work 


## Advice


* ABC