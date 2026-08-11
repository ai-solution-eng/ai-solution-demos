import yaml

from speech_to_speech_tools.utils.pcai_models import (
    cohere_transcribe_3_2b,
    gemma4_31B,
    qwen3_tts_1_7B,
)

helm_path = "helm/values.yaml"
asr = cohere_transcribe_3_2b
llm = gemma4_31B
tts = qwen3_tts_1_7B


class BlankLineDumper(yaml.Dumper):
    def write_line_break(self, data=None):
        # Write the normal line break
        super().write_line_break(data)
        # If the indentation stack has only 1 element, we are at the root level
        if len(self.indents) == 1:
            super().write_line_break()


with open(helm_path) as fid:
    data = yaml.safe_load(fid)


config_updates = {
    "asrBaseUrl": asr.url_remote,
    "llmBaseUrl": llm.url_remote,
    "ttsBaseUrl": tts.url_remote,
}

secrets_updates = {"asrApiKey": asr.api_key, "llmApiKey": llm.api_key, "ttsApiKey": tts.api_key}

data["config"].update(config_updates)
data["secrets"].update(secrets_updates)

with open(helm_path, "w") as fid:
    yaml.dump(data, fid, Dumper=BlankLineDumper, default_flow_style=False, sort_keys=False)
