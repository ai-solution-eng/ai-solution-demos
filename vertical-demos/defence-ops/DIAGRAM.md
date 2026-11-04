# Defence-Ops Architecture Diagram

This document gives a high-level overview of the **Defence-Ops** architecture: the services in the pod, the platform services they use, and how a question travels.

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
        subgraph gateway ["&nbsp; Ingress gateway &nbsp;"]
            istio["&nbsp; &nbsp; &nbsp; <b>SSO + routing</b> &nbsp; &nbsp; &nbsp;<br/><i>oauth2-proxy, VirtualService</i>"]
        end
        class istio container;
        subgraph ns ["One pod"]

            app_ui_svc["<b>app-ui</b><br/><i>Dashboard · System Config</i>"]
            llm_svc["<b>llm-service</b><br/><i>Prompts · VLM orchestration</i>"]
            kafka_svc["<b>kafka-service</b><br/><i>Live alerts · sample alerts</i>"]
            video_svc["<b>video-service</b><br/><i>Feeds · clips · frames</i>"]
            class app_ui_svc,llm_svc,kafka_svc,video_svc container;
            
        end
        subgraph inf_cluster ["Model serving"]
            litellm["<b>LiteLLM</b><br/><i>Model gateway, per-app key</i>"]
            mlis["<b>MLIS / vLLM</b><br/><i>vision-language models</i>"]
            ollama["<b>Other servers</b><br/><i>e.g. Ollama</i>"]
        end
        class litellm,mlis,ollama container;
    end

    %% 3. External Dependencies (Bottom)
    subgraph external ["Kafka broker (e.g. HPE Data Fabric)"]
        kafka_cluster["<b>Kafka API</b><br/>[e.g. Data Access Gateway]<br/><i>alerts topic</i>"]
    end
    class kafka_cluster outsider;

    %% --- Layout & Relationships ---
    spa -- "Secure Access<br/>[HTTPS]" --> istio
    istio -- "UI · System Config" --> app_ui_svc
    istio -- "/llm" --> llm_svc
    istio -- "/kafka" --> kafka_svc
    istio -- "/video" --> video_svc
    
    %% Aligning lower components side-by-side
    llm_svc -- "Chat + imagery" --> litellm
    llm_svc -. or directly .-> mlis
    litellm --> mlis
    litellm --> ollama
    llm_svc -- "Clip or frames<br/>[localhost]" --> video_svc
    kafka_svc -- "Produce · consume<br/>[SASL, stream user]" --> kafka_cluster
    
    %% --- Custom Standout Styling ---
    style pcai fill:none,stroke:#01a982,stroke-width:4px,color:#01a982
    style gateway fill:#068667,stroke:#01a982,stroke-width:2px,color:#fff
    style inf_cluster fill:#068667,stroke:#01a982,stroke-width:2px,color:#fff
    class user,ns,pods,storage,external node;
```

## Service Responsibilities

### 🖥️ app-ui
- **Frontend**: A Next.js dashboard for a "Global Defence Ops Centre": four feeds, the live alert ticker and the tactical assistant.
- **Admin Configuration**: Manages the demo settings (model, Kafka broker and topic) via `/api/v1/admin/demo-config`. Only PCAI administrators can change them; the app verifies their Keycloak sign-in token.
- **Persistence**: Saves the settings to a JSON file on a Persistent Volume. It holds no secrets.

### 🤖 llm-service
- **Tactical Chat**: Combines the question, the feeds in context and the latest alerts into one request to the selected model.
- **Prompts**: Puts fixed operating rules ahead of the operator's assistant instructions: use only the imagery and alerts supplied, separate observed from assessed with a confidence level, concise military register, British English and UTC times, no targeting details. It also tells the model which images, or which part of the clip, show which feed.
- **Models**: Lists the models the app's key may use, with what each accepts (vision, video, thinking), as set in LiteLLM or, for a model served directly, in the chart's `llm.capabilities` values.
- **Visual Context**: Sends video-capable models one short Motion-JPEG clip of the feeds, other vision models still frames, and falls back to frames if a model refuses video.
- **Thinking**: Switches on the model's own reasoning when asked and supported, and returns it separately from the answer.

### 📡 kafka-service
- **Live alerts**: Streams the topic to the dashboard as Server-Sent Events at `/api/v1/kafka/stream`.
- **Sample alerts**: `/api/v1/kafka/generate` writes 3–5 alerts about the feeds in the grid (each sample video has its own set; uploads are matched by file name), or general alerts when no feed is recognised.
- **Credentials**: Connects as a user limited to the alerts topic; its credentials are a Kubernetes Secret, from the Helm values or created beforehand.

### 🎥 video-service
- **Feeds**: Lists the sample videos and uploads, and serves them to the dashboard for playback.
- **Model input**: Builds the 2 fps clips and the still frames that `llm-service` sends to the model; rejects path tricks and oversized uploads.

## Request Flow Example: Tactical Analysis

1.  **User** asks a question about the "Convoy" feed, or presses Shield or Eye on it.
2.  **app-ui** sends the question, the assistant instructions, the feed names and the latest alerts (from **kafka-service**) to **llm-service**.
3.  **llm-service** reads the selected model from the shared configuration and checks what it accepts.
4.  **llm-service** asks **video-service** for a short clip (video models) or still frames per feed (other vision models).
5.  **llm-service** calls the model through **LiteLLM**, or the model's own endpoint, with the app's own key: the operating rules and instructions, the tasking, a note of which imagery shows which feed, and the alerts.
6.  The answer, with the model's reasoning if thinking was on, appears in the dashboard.
