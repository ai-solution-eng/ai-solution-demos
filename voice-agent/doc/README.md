# THIS DEMO IS NOT READY YET - Updated application version with new detailed documentation coming soon

# Voice Agent - v5.1.4 🎙️

An advanced, real-time voice AI agent system featuring **Whisper ASR**, **XTTS v2 TTS**, and **LLM Orchestration**. Designed for natural human-like conversations with multi-language support and advanced customer retention capabilities.

## 🚀 What's New in v5.1.4

- **Customer Retention Logic:** Integrated a smart retention engine that detects churn intent and automatically attempts to retain users with empathetic responses and promises of human follow-ups.
- **Official XTTS v2 Voices:** Switched to 33 verified, high-quality official speakers for superior clarity and accent accuracy.
- **Experimental Hebrew Support:** Added Hebrew (`he`) as an experimental language feature.
- **Enhanced TTS Stability:** - Reduced temperature (0.65) for more consistent vocal delivery.
    - Aggressive text cleaning to handle abbreviations and artifacts.
    - Multi-layered Fallback: XTTS v2 → Edge-TTS → Text-only.
- **Infrastructure Upgrades:** - Background health probes every 30 seconds to monitor sub-services.
    - Optimized memory management for long-running sessions.
    - Doubled resource allocation in Helm charts for ultra-low latency.

## 🏗️ Architecture

The system is containerized and orchestrated via Kubernetes (Helm), consisting of three primary services:

1.  **WebSocket Server (Port 8765):** The core pipeline managing audio streaming, Voice Activity Detection (VAD), Speech-to-Text (STT), and LLM routing.
2.  **Gradio UI (Port 8080):** A modern web interface for real-time interaction, configuration management, and conversation history.
3.  **XTTS v2 Server:** A dedicated GPU-accelerated service for high-fidelity speech synthesis.

## ⚙️ Resource Configuration (values.yaml)

To ensure real-time performance on high-end hardware (e.g., NVIDIA L40S), the default resources in this version are:

| Component | CPU Request/Limit | Memory Request/Limit |
| :--- | :--- | :--- |
| **WebSocket Server** | 2000m / 8000m | 2Gi / 4Gi |
| **Gradio UI** | 1000m / 4000m | 1Gi / 2Gi |
| **XTTS v2 Server** | 2000m / 4000m | 4Gi / 8Gi (Requires GPU) |

## 🛠️ Installation & Deployment

### Prerequisites
- Docker with Buildx
- Kubernetes Cluster (1.19+)
- Helm 3.0+
- NVIDIA GPU with CUDA 12.x drivers

### 1. Build Docker Images
Use the interactive build script to build and push images to your registry:
```bash
chmod +x build.sh
./build.sh
2. Deploy via HelmBash# Install the chart
helm install voice-agent ./chart

# Upgrade to latest version
helm upgrade voice-agent ./chart



📊 Latency Metrics
Version 5.1.4 includes detailed latency reporting:

ASR Latency: Time from speech end to text.

LLM Latency: Time for the brain to generate a response.

TTS Latency: Time to generate the first audio chunk.


🧠 Developer Notes
Retention Engine: Located in app.py. It uses multi-language keyword detection to trigger specialized prompts when a user expresses a desire to disconnect or cancel.

Database Integration: Supports PostgreSQL (via psycopg2) for persistence of conversation logs and latency metrics.

Health Checks: The system includes internal periodic_health_probe tasks to ensure all sub-services (LLM, TTS, DB) are responsive.


