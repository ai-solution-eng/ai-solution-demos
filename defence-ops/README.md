# Defence-Ops: Modern AI-Native Tactical Intelligence

A microservices-based demo application showcasing modern AI-native tactical intelligence capabilities on **HPE Private Cloud AI (PCAI)**.

## 🚀 Deployment on HPE PCAI 

Use Import Framework wizard to load the helm chart `defence-op.<version>.tgz` and use `defence-ops.png` image as logo. Provide a namespace (you can skip the release name).

Instead, if you have kubectl and helm installed and configured to your PCAI cluster, you can use the following commands to deploy the application:

### 📋 Prerequisites

1.  **Kubectl & Helm**: Ensure you have `kubectl` and `helm` installed and configured to your PCAI cluster.
2.  **Namespace**: Create the target namespace for the application.
    ```bash
    kubectl create namespace defence-ops
    ```

### 📦 Installation via Helm

The deployment is managed by a unified Helm chart located in `./helm`.

1.  **Customize Domain**: Update the `ezua.virtualService.endpoint` in `helm/values.yaml` or set it via command line.
2.  **Install Chart**:
    ```bash
    helm install defence-ops ./helm --namespace defence-ops \
      --set ezua.virtualService.endpoint=defence-ops.<YOUR_DOMAIN_NAME> \
      --set global.env=production
    ```
3.  **Verify Status**:
    ```bash
    kubectl get pods -n defence-ops
    ```

### 🔓 Accessing the Application

Once successfully deployed, the application will be available at:
`https://defence-ops.<YOUR_DOMAIN_NAME>`

First step is to set up the `system config`. Make sure you select the correct API format, when using MLIS, select `OpenAI compatible` and make sure you are using Vision Language Model, e.g. `nemotron-nano-12b-v2-vl` for best results, or `qwen3-vl:8b`). LLM Endpoint and API Token can be found in the `Model Endpoints` screen on AI Essentials.

### ▶️ Using the Application

- Select a video from dropdown menu for each grid box. Depending on your network speed, videos may take a while to load (but they will be cached for future use) when you click the play icon.

- Shield and Eye icons trigger pre-defined AI requests, while the chat box allows for custom queries. Shield triggers "Threat Detection" and Eye triggers "Object Identification" requests.

- When using chatbox, 
    - you can change the `Assistant Instructions` to customize the model behaviour, 
    - select a specific video for `Context` or use `Smart Context` to ask questions on what is actually seen on the screen (multiple videos + kafka messages),
    - enable 'thinking' mode to let the model think before answering and show its reasoning process.

## 🛠 Microservices Architecture

- **`app-ui`**: Next.js frontend providing the global tactical dashboard and administration portal.
- **`video-service`**: FastAPI service handling MJPEG streaming, video uploads, and sequence frame extraction.
- **`llm-service`**: FastAPI service integrating with KServe endpoints for Vision Language Model (VLM) analysis and tactical chat.
- **`kafka-service`**: FastAPI service providing real-time telemetry streaming via SSE and tactical alert generation.

## 🏗 Operations & Maintenance

If you want to make changes, you can build your own images and replace them in the `helm/values.yaml` file, or using `Configure` option on the Tools & Frameworks page.

### 🧱 Building and Publishing Images

A helper script is provided to build and push all microservices to Docker Hub. By default, it uses the `erdincka` user and the `defence-ops-` prefix.

1.  **Prerequisites**: Log in to Docker Hub: `docker login`.
2.  **Execute Publish Script**:
    ```bash
    ./scripts/publish_images.sh 1.0.0
    ```
    *Replace `1.0.0` with your desired tag.*

### 🛠 Local Development with Tilt

For live-updating development in a Kubernetes environment:
1. Ensure your `.env` file is configured with `DOMAIN` and `KUBE_CONTEXT`.
2. Run `tilt up`.

---

© 2026 HPE. For demonstration purposes only.
