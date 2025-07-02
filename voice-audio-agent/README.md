<!-- File: README.md (master) -->

# End-to-End Audio Conversational Agent in HPE's Private Cloud AI (PCAI) Platform

This repo guides you through:

1.  **Docker** → build & push two images (WebSocket server & Gradio UI)
2.  **Kubernetes Dev** → a quick `kubectl apply` to test locally
3.  **Helm** → package & import into HPE Private Cloud AI (PCAI)

---

## Pre-requisites

Deploy the following models in your HPE Machine Learning Inference Software Tool:

- **magpie-tts-multilingual**  
  https://build.nvidia.com/nvidia/magpie-tts-multilingual/api
- **parakeet-ctc-1.1b-asr**  
  https://build.nvidia.com/nvidia/parakeet-ctc-1_1b-asr/modelcard
- **meta-llama/Llama-3.2-1B**  
  https://huggingface.co/meta-llama/Llama-3.2-1B

---

## Customization

### Changing the Language

This demo is configured for English (`en-US`) by default but can be easily adapted for other languages supported by the models, such as Spanish.

All deployment configurations, including language codes and voice selection, are managed in the Helm chart's `values.yaml` file. For detailed instructions on how to switch to Spanish or see other available voices, please refer to the **[Helm README](helm/README.md)**.

---

## Quickstart

1.  **Docker**  
    ```bash
    cd docker
    bash build.sh
    ```
    See [docker/README.md](docker/README.md).

2.  **Kubernetes Dev**
    ```bash
    cd dev-k8s-deployment
    kubectl apply -f pod-svc-vs.yaml
    ```
    See [dev-k8s-deployment/README.md](dev-k8s-deployment/README.md).

3.  **Helm & PCAI**
    ```bash
    cd helm
    helm package .
    ```
    Import `audio-pipeline-chart-0.1.0.tgz` via PCAI’s Tools & Frameworks → Import Framework.
    See the **[Helm README](helm/README.md)** for more details on customization and deployment.

Now you’re all set to run your end-to-end audio AI pipeline on HPE Private Cloud AI!