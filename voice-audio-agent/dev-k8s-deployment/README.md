<!-- File: dev-k8s-deployment/README.md -->

# Kubernetes Dev Deployment

This folder provides a simple Service, Deployment, and Istio VirtualService in `pod-svc-vs.yaml` for local/dev testing.

## Prerequisites

- `kubectl` configured against your cluster  
- Istio installed (if you want to use the `VirtualService`)  
- A namespace created (default in this file is `andrew-mendez-184335fb`)

## What to Change

Open `pod-svc-vs.yaml` and update:

- **Namespace** under every `metadata:` block  
- `OPENAI_API_KEY` environment variable  
- URLs for `LLM_API_BASE`, `ASR_SERVER_ADDRESS`, and `TTS_SERVER_ADDRESS` if your services live elsewhere  

## Deploy

```bash
kubectl apply -f pod-svc-vs.yaml
```

## Verify

```bash
kubectl get ns,svc,deploy -n <your-namespace>
kubectl get virtualservice -n <your-namespace>
```
