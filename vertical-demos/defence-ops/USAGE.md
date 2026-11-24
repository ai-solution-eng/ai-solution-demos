# Defence-Ops: User Guide & Demo Walkthrough

This guide walks through a demonstration of the **Defence-Ops** tactical intelligence dashboard. It assumes the application is deployed and open in your browser.

## ⚙️ Phase 1: Initial Configuration

Configure the model and the Kafka datalink once, or when you need to switch models or clusters.

1.  **Open Settings**: Click **System Config** (gear icon) in the navigation bar. Only PCAI administrators can change these settings; everyone else sees them read-only, with the reason. Press **Esc** or the **✕** to close it.
2.  **Model**: Pick a model from the list of models this app's key may use on the model endpoint (LiteLLM, or a model served directly). Badges show whether it takes **Vision**, **Video** and **Thinking**; choose one with Vision. **Unknown** means the endpoint does not declare it: set `llm.capabilities` in the app's Helm values.
    *   Models with Video receive a short clip of the selected feeds; other vision models receive still frames.
    *   The chat's **Thinking** toggle is available only for models with Thinking.
3.  **Data Fabric (Optional)**:
    *   To stream live alerts, enter the **Broker** (`broker-host:port`) and the **Topic**.
    *   The credentials come from the app's Helm values, set when the app is imported; they are not entered here.
4.  **Save**: Click "Save Configuration" to apply the settings.

---

## 🚀 Phase 2: Tactical Demonstration Flow

Follow this sequence to show the core capabilities.

### 1. Tactical feeds
- Choose a source in each grid cell and press **Play**. `video-service` serves the footage.
- Up to four feeds can play at once; the same feed can sit in more than one cell.

### 2. One-click reports (Shield & Eye)
- **Threat assessment (Shield)**: a bottom line up front (BLUF), what is observed, each threat with its likelihood and impact, and recommended actions in priority order.
- **SALUTE report (Eye)**: Size, Activity, Location, Unit, Time and Equipment for the forces in the feed, followed by a table of objects of interest with counts.

### 3. Live tactical alerts (Kafka)
- **Generate Alerts** sends 3–5 sample alerts to the Kafka topic about the feeds in the grid. A Naval Exercises feed, for example, brings a rocket salvo from pennant 158; a Convoy feed brings reports on the halted column. Each alert's location starts with the feed's name.
- With no feed selected, or only uploaded footage the app does not recognise, the alerts are general ones.
- The latest 20 alerts go with every question, so the assistant can tie them to what it sees.

### 4. Tactical chat
The chat is the most flexible way to work with the assistant. Type any question, or click one of the sample questions above the box (**SITREP**, **Confirm alerts**, **Count vehicles**, **Force strength**, **Likely intent**, **Priorities**, **Next ISR**) to put it in the box, edit it if you like, and press **Enter**.

#### 🧠 Thinking Mode
- **Checked**: the model reasons before it answers; open **Reasoning** above the answer to see how it got there. Use it for assessments where the logic matters.
- **Unchecked**: direct, faster answers. Some models reason anyway; their reasoning is folded away in the same way.

#### 🎯 Context Selection
- **Smart (Active Streams)**: every playing feed plus the recent alerts. Use it for questions such as *"Is there any coordinated movement across the sectors?"*
- **A single feed**: the assistant sees only that feed (and the alerts).
- **None**: alerts and question only, no imagery.

The assistant is told which images or which part of the clip show which feed, so it can name feeds in its answer.

### 5. Assistant Instructions
- The default instructions ask for a watchkeeper's report: **BLUF**, **OBSERVED** by feed, **THREATS** with likelihood and impact, and **RECOMMENDED ACTIONS**, using SITREP and SALUTE formats where they fit.
- Edit them to change the format or persona, for example *"Report in brief radio style"* or *"Give a detailed technical analysis for an engineer"*. **Reset** restores the default. Your wording is kept in this browser only.
- Whatever the instructions say, the assistant follows fixed operating rules: it uses only the imagery and alerts it was given, separates what is observed from what is assessed (with a confidence level), writes in a concise military register in British English with UTC times, and gives no targeting or weapon-employment details.

---

## 💡 Demo Tips

- **Latency**: the first play of a feed can take a few seconds; after that it is instant. A question with several feeds takes longer than one with a single feed.
- **Show the link between alerts and imagery**: select Naval Exercises and Convoy, press **Generate Alerts**, then ask *"Which alerts can you confirm from the imagery?"*
- **Architecture**: if technical questions come up, open **Docs** and select the **DIAGRAM** tab.

---
© 2026 HPE. For demonstration purposes only.
