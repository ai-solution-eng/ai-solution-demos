"""Environment settings for llm-service."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class LlmSettings(BaseSettings):
    """Settings read from the environment, which the Helm chart sets.

    Attributes:
        litellm_base_url: The model endpoint (LiteLLM, or an OpenAI-compatible server such as
            an HPE MLIS deployment), without the /v1 suffix.
        litellm_api_key: The app's key for that endpoint, from the chart's Secret.
        llm_supports_vision: Assumed when the endpoint does not declare it (llm.capabilities).
        llm_supports_video: Likewise for video input.
        llm_supports_reasoning: Likewise for thinking mode.
        video_service_url: video-service in the same pod.
        request_timeout_seconds: Upper bound for one model call.
        models_cache_seconds: How long the model list is reused.
        max_images: Frames sent to a model that takes images but not video (the 1.0 limit).
    """

    litellm_base_url: str = "http://litellm.litellm.svc.cluster.local:4000"
    litellm_api_key: SecretStr | None = None
    llm_supports_vision: bool | None = None
    llm_supports_video: bool | None = None
    llm_supports_reasoning: bool | None = None
    video_service_url: str = "http://127.0.0.1:8003"
    request_timeout_seconds: float = Field(default=600, gt=0)
    models_cache_seconds: float = Field(default=60, ge=0)
    max_images: int = Field(default=5, ge=1, le=16)


@lru_cache
def get_settings() -> LlmSettings:
    """Returns the settings, read once per process."""
    return LlmSettings()
