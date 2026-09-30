<div align=center>
<img src="https://raw.githubusercontent.com/hpe-design/logos/master/Requirements/color-logo.png" alt="HPE Logo" height="100"/>
</div>

# HPE Private Cloud AI

##  AI Solution Misc Demos

This folder contains demos that are neither implementing a solution to a common use case, nor bound to a specific vertical. They may or may not partially overlap with primary demos, implement an uncommon use case, or just be custom apps built as pure technical demos. 

Here is the list of demos you will find in this misc folder:

| Demo                                 | Short Description          | Demo Video      | 
| -------------------------------------|----------------------------|-----------------|
| [AI Vulnerability Scanner](ai-vulnerability-scanner)            | An AI-powered scanner that crawls a bundled **OWASP Juice Shop** app and uses an LLM (deployed via **MLIS**) to analyze each page like a pentester, surfacing ranked, remediated findings in a live dashboard. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/AI%20Vulnerability%20Scanner%20Demo.mp4) |
| [Agentic Meetings Simulations](agentic-meetings-simulations)              | A custom web application that simulates company meetings using agentic AI workflows. Relies on **MLIS** for model deployment.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Meeting-agentic-simulation.mp4) |
| [Offline Meeting Transcription](offline-meeting-transcription)                | A transcription pipeline converting raw audio recordings into speaker-attributed transcripts and structured meeting minutes, using Whisper (for ASR, deployed on **MLIS**) and **Pyannote** (for speaker diarization) connected to **Open WebUI**.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Offline_meeting_transcription.mp4) |
| [Onboarding Buddy](onboarding-buddy)                        | A mock application to help organizations streamline onboarding for new hires, with task management by admin users and an AI Q&A assistant for the new hires. Uses **MLIS** for model deployment.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Onboarding%20Buddy%20Recording.mp4) |
| [Realtime Live Voice Translation](realtime-live-voice-translation)                | A custom web application that captures the user's voice and provides transcription and translation in real time. Relies on Whisper ASR model and a generic LLM deployed on **MLIS**.| - |
| [Voice Agent XTTS](voice-agent-xtts)                        | A custom Gradio application that connects to a chat model, Whisper for STT and XTTS-v2 for TTS, all deployed on **MLIS**, to provide a conversational assitant, able to discuss with the user in many different languages. Also includes a "chat with SQL data" scenario.           | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Voice_Agent.mp4) |

