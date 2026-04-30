<div align=center>
<img src="https://raw.githubusercontent.com/hpe-design/logos/master/Requirements/color-logo.png" alt="HPE Logo" height="100"/>
</div>

# HPE Private Cloud AI

##  AI Solution Archived Demos

This folder contains older demos that may have been superseded by more recent ones, or are planned to be so. It also contains miscelleanous vertical agnostic demos that implement specific use cases with narrow applications. 

**We do not guarantee support for these demos.**

Here is the list of demos you will find in this archive folder:

| Demo                                                          | Short Description          |
| --------------------------------------------------------------|----------------------------|
| [Agentic Meetings Simulations](agentic-meetings-simulations)              | A custom web application that simulates company meetings using agentic AI workflows. Relies on **MLIS** for model deployment.           |
| [AI Support Assistant](ai-support-assistant)                          | A mock support application relying on **Open WebUI** built-in RAG capabilities and **Airflow**. Relies on **Ollama** for model deployment.             |
| [Finetune Tool Calling LLM](finetune-tool-calling-llm)                | **Notebooks**, using **Nemo microservices** to fine-tune an LLM to improve its tool-calling capabilities.           |
| [Live Stream Frame Analytics](live-stream-frame-analytics)                      | A **Gradio application** for analyzing multiple, real-time video streams using a Vision Language Model (VLM) deployed on **MLIS**.|
| [Media Database SQL RAG](media-database-sql-rag)                        | Helm chart to deploy **Vanna AI**, a tool that relies on an LLM to convert natural language questions into SQL queries, enabling chatting with SQL data. **MLIS** can be used to deploy the LLM.          |
| [Multilingual Voice Agent](multilingual-voice-agent)                        | An **Open WebUI** setup using a chat model, Whisper for STT and Chatterbox for TTS, deployed on **MLIS**, to allow voice-to-voice chatting with the chat model, in many different languages. Includes instructions for chatting with SQL data as well.|
| [Onboarding Buddy](onboarding-buddy)                        | A mock application to help organizations streamline onboarding for new hires, with task management by admin users and an AI Q&A assistant for the new hires. Uses **MLIS** for model deployment.|
| [Voice Audio Agent](voice-audio-agent)                        | A custom **Gradio application** that connects to a chat model, parakeet-ctc-1.1b-asr for STT and magpie-tts-multilingual for TTS, all deployed on **MLIS**, to provide a conversational assitant.|

