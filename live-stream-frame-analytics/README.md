# Real-Time VLM-Powered Video Stream Analysis on HPE Private Cloud AI

author: andrew.mendez@hpe.com


This project demonstrates a powerful, interactive web application for analyzing multiple, real-time video streams using a Vision Language Model (VLM). The application is built with Gradio, containerized with Docker, and designed for seamless deployment on **HPE Private Cloud AI (PCAI)** using a reusable Helm chart.

Users can view up to four MJPEG video streams, capture a single frame from any stream, and send it to a deployed VLM with a custom prompt to extract structured information.


*(Image: A conceptual view of the Gradio UI, showing four video stream panels, each with controls for analysis and data export.)*

---

### Context on Customer and Use Case

This customer runs a chain of vehicle inspection centers in Latin America. Their goal in engaging with us was to modernize their facilities and automate current processes around auditing, surveillance, and control of the inspection process. They were looking to use AI to extract metadata about the vehicles in their facilities and make that metadata available to analysts and downstream processes.

### Solution Overview and Demo Video

![Solution Overview](assets/SolutionOverview.jpg)

▶️ [Quick Demo Video](https://hpe-my.sharepoint.com/:v:/p/akash_khanna/EQj1-tXzrTdEi3nBGVIe9dwBLJKRiBhHLUAVn2agLjddrg)

---

### Key Features

*   **Quad-Stream Viewer:** Monitor up to four independent MJPEG video streams simultaneously in a tabbed interface.
*   **On-Demand VLM Analysis:** Capture a single frame from any live stream and send it to a Vision Language Model for analysis.
*   **Structured Data Extraction:** The VLM prompt is engineered to return structured data (e.g., vehicle type, color, status), which the application parses into a clean JSON object.
*   **Dynamic VLM Configuration:** Configure the VLM's API endpoint and key directly within the Gradio UI, allowing for easy switching between different models or environments.
*   **Data Export:** Download the parsed JSON data for any analysis with a single click.
*   **HPE PCAI Optimized:** Packaged as a Helm chart that integrates with PCAI's Istio gateway and "Tools & Frameworks" deployment model.

---

## Prerequisites

### 1. Model Deployment in HPE PCAI

This application requires a Vision Language Model to be deployed and running within the HPE Machine Learning Inference Software Tool. The prompt is specifically tailored for:

*   **Model:** `meta-llama/Llama-3.2-11B-Vision-Instruct`
*   **Details:** [Hugging Face Model Card](https://huggingface.co/meta-llama/Llama-3.2-11B-Vision-Instruct)

Before deploying this application, you must:
1.  Deploy the `Llama-3.2-11B-Vision-Instruct` model in your HPE PCAI environment.
2.  Obtain the **API Base URL** (the model's inference endpoint).
3.  Generate and copy an **API Key** for accessing the model.

You will need the API Base URL and API Key to configure the application after it is deployed.

### 2. Local Development Tools

*   **Docker:** To build and push the application container image.
*   **Helm 3.x:** To package the Kubernetes deployment chart.
*   **`kubectl`:** (Optional) For inspecting the deployment in your Kubernetes cluster.

---

## Quickstart: Build and Deploy

The process involves three main steps: building the Docker image, packaging the Helm chart, and deploying it through the HPE PCAI user interface.

### Step 1: Build and Push the Docker Image

The application code is packaged into a Docker image which will be pulled by your Kubernetes cluster.

1.  Navigate to the `docker` directory:
    ```bash
    cd docker/
    ```

2.  Ensure you are logged into a Docker registry that your PCAI cluster can access (e.g., Docker Hub, a private registry).
    ```bash
    docker login
    ```

3.  Execute the build script. This will build the image for the `linux/amd64` platform and push it to the registry. The default image name is `mendeza/gradio_stream_demo:0.0.1`.
    ```bash
    bash build.sh
    ```

### Step 2: Package the Helm Chart

The Helm chart contains all the Kubernetes manifests needed to run the application on PCAI.

1.  Navigate to the `helm` directory:
    ```bash
    cd helm/
    ```

2.  Package the chart into a `.tgz` archive.
    ```bash
    helm package .
    ```

3.  This command will create a file named `gradio-vlm-chart-0.1.0.tgz`. This is the file you will upload to PCAI.

### Step 3: Deploy via HPE Private Cloud AI

1.  Log in to your **HPE Private Cloud AI** console.
2.  Navigate to **Tools & Frameworks** from the side menu.
3.  Click **Import Framework**.

    *   **Step 1: Framework Details**
        *   **Framework Name:** `Gradio VLM Stream Analyzer`
        *   **Description:** `An interactive UI to analyze frames from MJPEG streams with a Vision Language Model.`
        *   Optionally, upload a logo.
        *   Click **Next**.

    *   **Step 2: Upload Package**
        *   Upload the `gradio-vlm-chart-0.1.0.tgz` file you created in the previous step.
        *   Choose a **Namespace** to deploy into (e.g., your project's namespace).
        *   Click **Next**.

    *   **Step 3: Review & Deploy**
        *   The UI will display the contents of the `values.yaml` file.
        *   **IMPORTANT:** You must configure the ingress endpoint. Locate the `ezua.virtualService.endpoint` key and update its value to match your PCAI environment's domain. For example:
          `endpoint: "gradio-vlm.${DOMAIN_NAME}"` To something like this:  `endpoint: "gradio-vlm.my-namespace.ingress.pcai-cluster.mycompany.com"`
        *   Review other values if needed (e.g., `replicaCount`, `resources`).
        *   Click **Deploy**.

---

## Using the Application

1.  Once the deployment is complete, navigate to **My Workloads** in PCAI to find and launch the application.
2.  The application will open in a new tab.

### Initial Configuration

Before you can analyze frames, you must configure the VLM endpoint:

1.  Expand the **Vision Model Configuration** accordion at the top.
2.  Enter the **API Key** and **API Base URL** that you obtained from your model deployment in the prerequisites.
3.  Click **Apply Configuration**. A status message will confirm if the connection was successful.

### Analyzing a Stream

1.  Select one of the **Stream** tabs.
2.  Enter an MJPEG stream URL and click **Start Stream**.
3.  Once the video is playing, you can click **Analyze Current Frame**.
4.  The application will capture the latest frame, send it to the VLM, and display the raw text output and the parsed JSON object below.
5.  Click **Save Parsed JSON** to download the results.