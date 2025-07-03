<!-- File: helm/README.md -->

# Helm Chart for Audio Pipeline

This Helm chart (`audio-pipeline-chart`) deploys both the WebSocket server and the Gradio UI on any Kubernetes cluster (designed for HPE PCAI).

## Prerequisites

- Helm 3.x
- A Kubernetes cluster with Istio (for the VirtualService)
- The models deployed in PCAI (see root README)

## Customize

- Edit `values.yaml` to set image tags, resource requests/limits, and environment variables.
- Ensure `ezua.virtualService.endpoint` and `istioGateway` match your PCAI ingress.

## Customizing for Other Languages
After the app is deployed to PCAI, you can select Configure to update the yaml to connect new ASR and TTS endpoints, and also configure the TTS to speak in spanish.

### Example: Enabling Spanish TTS

You can change the language for both Automatic Speech Recognition (ASR) and Text-to-Speech (TTS) by updating a few environment variables in the `values.yaml` file.

To switch to **Spanish**, open `values.yaml` and modify the following keys under `websocketServer.env`:

-   `TTS_LANGUAGE_CODE`: Set to `"es-US"`
-   `TTS_VOICE`: Choose a Spanish voice.
    -   Male: `"Magpie-Multilingual.ES-US.Diego"`
    -   Female: `"Magpie-Multilingual.ES-US.Isabela"`
- `LLM_PROMPT_TEMPLATE`: `'Answer the question: "{transcript}"\n\nAnswer concisely and in Spanish.'`

The LLM_PROMPT_TEMPLATE needs to change, because by default the LLM will respond in spanish.

Here is an example snippet from `values.yaml` configured for the male Spanish voice:

```yaml
# In helm/values.yaml
websocketServer:
  env:
    # ... other variables
    TTS_LANGUAGE_CODE: "es-US"
    TTS_VOICE: "Magpie-Multilingual.ES-US.Diego"
    LLM_PROMPT_TEMPLATE: 'Answer the question: "{transcript}"\n\nAnswer concisely and in Spanish.'
    # ... other variables
```

# Available Voices and Languages
The magpie-tts-multilingual model supports multiple languages and voices. Below is a list of available options you can use for the ASR_LANGUAGE_CODE, TTS_LANGUAGE_CODE, and TTS_VOICE variables.
```json
{
    "en-US,es-US,fr-FR,de-DE": {
        "voices": [
            "Magpie-Multilingual.EN-US.Sofia",
            "Magpie-Multilingual.EN-US.Ray",
            "Magpie-Multilingual.EN-US.Sofia.Calm",
            "Magpie-Multilingual.EN-US.Sofia.Fearful",
            "Magpie-Multilingual.EN-US.Sofia.Happy",
            "Magpie-Multilingual.EN-US.Sofia.Angry",
            "Magpie-Multilingual.EN-US.Sofia.Neutral",
            "Magpie-Multilingual.EN-US.Sofia.Disgusted",
            "Magpie-Multilingual.EN-US.Sofia.Sad",
            "Magpie-Multilingual.EN-US.Ray.Angry",
            "Magpie-Multilingual.EN-US.Ray.Calm",
            "Magpie-Multilingual.EN-US.Ray.Disgusted",
            "Magpie-Multilingual.EN-US.Ray.Fearful",
            "Magpie-Multilingual.EN-US.Ray.Happy",
            "Magpie-Multilingual.EN-US.Ray.Neutral",
            "Magpie-Multilingual.EN-US.Ray.Sad",
            "Magpie-Multilingual.ES-US.Diego",
            "Magpie-Multilingual.ES-US.Isabela",
            "Magpie-Multilingual.ES-US.Isabela.Neutral",
            "Magpie-Multilingual.ES-US.Diego.Neutral",
            "Magpie-Multilingual.ES-US.Isabela.Fearful",
            "Magpie-Multilingual.ES-US.Diego.Fearful",
            "Magpie-Multilingual.ES-US.Isabela.Happy",
            "Magpie-Multilingual.ES-US.Diego.Happy",
            "Magpie-Multilingual.ES-US.Isabela.Sad",
            "Magpie-Multilingual.ES-US.Diego.Sad",
            "Magpie-Multilingual.ES-US.Isabela.Pleasant_Surprise",
            "Magpie-Multilingual.ES-US.Diego.Pleasant_Surprise",
            "Magpie-Multilingual.ES-US.Isabela.Angry",
            "Magpie-Multilingual.ES-US.Diego.Angry",
            "Magpie-Multilingual.ES-US.Isabela.Calm",
            "Magpie-Multilingual.ES-US.Diego.Calm",
            "Magpie-Multilingual.ES-US.Isabela.Disgust",
            "Magpie-Multilingual.ES-US.Diego.Disgust",
            "Magpie-Multilingual.FR-FR.Louise",
            "Magpie-Multilingual.FR-FR.Pascal",
            "Magpie-Multilingual.FR-FR.Louise.Neutral",
            "Magpie-Multilingual.FR-FR.Pascal.Neutral",
            "Magpie-Multilingual.FR-FR.Louise.Angry",
            "Magpie-Multilingual.FR-FR.Louise.Calm",
            "Magpie-Multilingual.FR-FR.Louise.Disgust",
            "Magpie-Multilingual.FR-FR.Louise.Fearful",
            "Magpie-Multilingual.FR-FR.Louise.Happy",
            "Magpie-Multilingual.FR-FR.Louise.Pleasant_Surprise",
            "Magpie-Multilingual.FR-FR.Louise.Sad",
            "Magpie-Multilingual.FR-FR.Pascal.Angry",
            "Magpie-Multilingual.FR-FR.Pascal.Calm",
            "Magpie-Multilingual.FR-FR.Pascal.Disgust",
            "Magpie-Multilingual.FR-FR.Pascal.Fearful",
            "Magpie-Multilingual.FR-FR.Pascal.Happy",
            "Magpie-Multilingual.FR-FR.Pascal.Pleasant_Surprise",
            "Magpie-Multilingual.FR-FR.Pascal.Sad",
            "Magpie-Multilingual.EN-US.Mia",
            "Magpie-Multilingual.DE-DE.Jason",
            "Magpie-Multilingual.DE-DE.Leo",
            "Magpie-Multilingual.DE-DE.Aria"
        ]
    }
}
```