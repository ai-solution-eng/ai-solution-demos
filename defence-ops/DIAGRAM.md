# Defence-Ops Architecture Diagram

This document provides a high-level overview of the **Defence-Ops** microservices architecture, illustrating the connections between services, external providers, and the user request flow.

## System Architecture
 
```mermaid
%%{ init: { 'themeVariables': { 'edgeLabelBackground': 'transparent', 'primaryTextColor': '#F7F7F7', 'secondaryTextColor': '#F7F7F7', 'tertiaryTextColor': '#F7F7F7' } } }%%
flowchart TD
    %% Styling Classes
    classDef container fill:#7764fc,color:#fff,stroke:#0b4884,stroke-width:2px;
    classDef outsider fill:#0070f8,color:#fff,stroke:#0b4884,stroke-width:2px;
    classDef node fill:none,color:#F7F7F7,stroke:#535C66,stroke-width:2px,stroke-dasharray: 5 5;
    classDef external fill:#3E4550,color:#fff,stroke:#666,stroke-width:2px;
    classDef database fill:#6c2b7c,color:#fff,stroke:#0b4884,stroke-width:2px;

    %% 1. User Context (Top)
    subgraph user ["User"]
        spa["<b>Web Browser</b><br/><i>Tactical Control Dashboard</i>"]
    end
    class spa outsider;

    %% 2. HPE Private Cloud AI
    subgraph pcai ["<b>HPE Private Cloud AI</b> / <i>Turnkey AI Platform</i>"]
        subgraph gateway ["&nbsp; VirtualService &nbsp;"]
            istio["&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; <b>Gateway</b> &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;<br/><i>Routes traffic to services</i>"]
        end
        class istio container;
        subgraph ns ["Namespace"]

            app_ui_svc["<b>app-ui</b><br/><i>Config Provider</i>"]
            llm_svc["<b>llm-service</b><br/><i>VLM Orchestration</i>"]
            kafka_svc["<b>kafka-service</b><br/><i>Event Streaming</i>"]
            video_svc["<b>video-service</b><br/><i>MJPEG Streaming</i>"]
            class app_ui_svc,llm_svc,kafka_svc,video_svc container;
            
        end
        subgraph inf_cluster ["Inference Services [MLIS]"]
            mlis["<b>MLIS</b><br/><i>Local VLM / NVIDIA Triton</i>"]
        end
        class mlis container;
    end

    %% 3. External Dependencies (Bottom)
    subgraph external ["Data Fabric (Tactical/Cloud)"]
        kafka_cluster["<b>Tactical Kafka</b><br/>[Message Broker]<br/><i>Mission telemetry source</i>"]
    end
    class kafka_cluster outsider;

    %% --- Layout & Relationships ---
    spa -- "Secure Access<br/>[HTTPS]" --> istio
    istio -- "Routes UI/Config" --> app_ui_svc
    istio -- "Routes /llm" --> llm_svc
    istio -- "Routes /kafka" --> kafka_svc
    istio -- "Routes /video" --> video_svc
    
    %% Aligning lower components side-by-side
    llm_svc -- "VLM Inference" --> mlis
    video_svc -- "Sync Assets" --> app_ui_svc
    kafka_svc -- "Pub/Sub<br/>[SASL_SSL]" --> kafka_cluster
    
    %% --- Custom Standout Styling ---
    style pcai fill:none,stroke:#01a982,stroke-width:4px,color:#01a982
    style gateway fill:#068667,stroke:#01a982,stroke-width:2px,color:#fff
    style inf_cluster fill:#068667,stroke:#01a982,stroke-width:2px,color:#fff
    class user,ns,pods,storage,external node;
```

## Service Responsibilities

### 🖥️ app-ui
- **Frontend**: A Next.js-powered dashboard providing a "Global Tactical Operations Room" experience.
- **Admin Configuration**: Manages demo settings (LLM endpoints, Kafka credentials) via `/api/v1/admin/demo-config`.
- **Persistence**: Saves and loads system configuration from a shared JSON file on a Persistent Volume.

### 🤖 llm-service
- **Tactical Chat**: Handles user queries and merges them with Kafka tactical context or image/video frames.
- **Model Discovery**: Automatically discovers available `InferenceServices` in the cluster or validates external OpenAI-compatible providers.
- **Inference Integration**: Orchestrates calls to modern LLMs (e.g., Qwen, DeepSeek, Llama) for tactical analysis.

### 📡 kafka-service
- **SSE Streaming**: Provides a Server-Sent Events (SSE) endpoint at `/api/v1/kafka/stream` for real-time dashboard updates.
- **Alert Generation**: Simulations or actual production of tactical alerts to the Kafka cluster.
- **Telemetry**: Consumes raw mission data and exposes it to the UI.

### 🎥 video-service
- **Video Processing**: Serves four independent MJPEG streams representing tactical feeds.
- **VLM Readiness**: Extract sequence frames from video feeds for Vision Language Model analysis in the `llm-service`.

## Request Flow Example: Tactical Analysis

1.  **User** asks a question in the chat about "Tactical 1" feed.
2.  **app-ui** requests the latest frames for "Tactical 1" from **video-service**.
3.  **app-ui** sends the prompt + frames + recent alerts (from **kafka-service**) to **llm-service**.
4.  **llm-service** consults its configuration (fetched from **app-ui** via the internal API).
5.  **llm-service** calls the configured **Inference Endpoint** (e.g., a KServe deployment).
6.  The result is returned to the user via the dashboard.
