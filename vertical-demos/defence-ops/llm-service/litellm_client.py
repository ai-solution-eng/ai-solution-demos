"""Client for the model endpoint: the app key's models, their capabilities, and chat.

The endpoint is a LiteLLM proxy or any OpenAI-compatible server, such as an HPE MLIS deployment.
Only LiteLLM declares what each model accepts (/v1/model/info); for the others the chart's
llm.capabilities values say it.
"""

import time
from dataclasses import dataclass, replace
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class LiteLLMNotConfiguredError(Exception):
    """The app has no key for the model endpoint."""


class LiteLLMError(Exception):
    """The model endpoint answered with an error, or could not be reached.

    Attributes:
        status_code: The endpoint's HTTP status; 502 when it could not be reached, 504 on timeout.
    """

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AssumedCapabilities:
    """What to assume where the endpoint declares nothing (chart values llm.capabilities).

    None leaves the capability unknown.
    """

    vision: bool | None = None
    video: bool | None = None
    reasoning: bool | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    """What a model accepts: LiteLLM's model_info where it declares it, else AssumedCapabilities.

    None means neither says. The chat flow then assumes images work, video does not, and
    thinking is unavailable.

    Attributes:
        id: Model name as the app's key sees it.
        vision: supports_vision.
        video: supports_video_input, or False once the model has refused a clip.
        reasoning: supports_reasoning.
    """

    id: str
    vision: bool | None
    video: bool | None
    reasoning: bool | None


def _flag(info: dict[str, Any], key: str, assumed: bool | None) -> bool | None:
    value = info.get(key)
    return value if isinstance(value, bool) else assumed


def _error_text(response: httpx.Response) -> str:
    try:
        body: Any = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:500]}"
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    return f"HTTP {response.status_code}: {str(message or body)[:500]}"


class LiteLLMClient:
    """Talks to the model endpoint with the app's own key, which never leaves this service."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        timeout: float,
        cache_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        assumed: AssumedCapabilities | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._cache_seconds = cache_seconds
        self._transport = transport  # tests pass httpx.MockTransport
        self._assumed = assumed or AssumedCapabilities()
        self._cached: tuple[float, list[ModelCapabilities]] | None = None
        self._video_refused: set[str] = set()

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise LiteLLMNotConfiguredError(
                "No key for the model endpoint: set llm.apiKey, or llm.existingSecret, in the "
                "app's Helm values."
            )
        return {"Authorization": f"Bearer {self._api_key}"}

    async def _get(self, client: httpx.AsyncClient, path: str) -> dict[str, Any]:
        response = await client.get(f"{self._base_url}{path}", headers=self._headers())
        if response.status_code >= 400:
            raise LiteLLMError(_error_text(response), response.status_code)
        body: Any = response.json()
        return body if isinstance(body, dict) else {}

    async def list_models(self, refresh: bool = False) -> list[ModelCapabilities]:
        """Lists the models the app's key may use, with their declared capabilities.

        /v1/models is the authority on which models the key may use; LiteLLM's /v1/model/info
        adds the capability flags it has, and the assumed capabilities fill the rest. Cached for
        models_cache_seconds.

        Args:
            refresh: Ignore the cache.

        Returns:
            Models sorted by name.

        Raises:
            LiteLLMNotConfiguredError: The app has no key.
            LiteLLMError: The endpoint refused the key or could not be reached.
        """
        now = time.monotonic()
        if not refresh and self._cached and now - self._cached[0] < self._cache_seconds:
            return self._cached[1]

        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                listed = await self._get(client, "/v1/models")
                try:
                    info = await self._get(client, "/v1/model/info")
                except LiteLLMError as e:
                    # Expected from anything but LiteLLM: llm.capabilities stands in.
                    logger.info("model_info_unavailable", status_code=e.status_code)
                    info = {}
        except httpx.HTTPError as e:
            raise LiteLLMError(
                f"Could not reach the model endpoint at {self._base_url}: {e}", 502
            ) from e

        model_info: dict[str, dict[str, Any]] = {}
        for entry in info.get("data", []):
            if isinstance(entry, dict) and isinstance(entry.get("model_name"), str):
                details = entry.get("model_info")
                details = details if isinstance(details, dict) else {}
                model_info.setdefault(entry["model_name"], details)

        ids = sorted(
            {
                m["id"]
                for m in listed.get("data", [])
                if isinstance(m, dict) and isinstance(m.get("id"), str)
            }
        )
        assumed = self._assumed
        models = [
            ModelCapabilities(
                id=model_id,
                vision=_flag(model_info.get(model_id, {}), "supports_vision", assumed.vision),
                video=False
                if model_id in self._video_refused
                else _flag(model_info.get(model_id, {}), "supports_video_input", assumed.video),
                reasoning=_flag(
                    model_info.get(model_id, {}), "supports_reasoning", assumed.reasoning
                ),
            )
            for model_id in ids
        ]
        self._cached = (now, models)
        return models

    async def capabilities(self, model: str) -> ModelCapabilities:
        """Returns one model's capabilities.

        Raises:
            LiteLLMError: 404 when the app's key may not use the model.
        """
        for candidate in await self.list_models():
            if candidate.id == model:
                return candidate
        raise LiteLLMError(
            f"Model {model} is not available to this app's key on the model endpoint. "
            "An administrator should pick another model on System Config.",
            404,
        )

    def mark_video_refused(self, model: str) -> None:
        """Remembers that a model refused a video clip, so frames are sent from now on."""
        self._video_refused.add(model)
        if self._cached:
            self._cached = (
                self._cached[0],
                [replace(m, video=False) if m.id == model else m for m in self._cached[1]],
            )

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Sends one chat completion to the model endpoint.

        Raises:
            LiteLLMNotConfiguredError: The app has no key.
            LiteLLMError: The model or the endpoint failed; status_code says how.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/v1/chat/completions", headers=self._headers(), json=payload
                )
        except httpx.TimeoutException as e:
            raise LiteLLMError(
                f"The model did not answer within {self._timeout:.0f} s. It may still be loading; "
                "try again in a minute.",
                504,
            ) from e
        except httpx.HTTPError as e:
            raise LiteLLMError(
                f"Could not reach the model endpoint at {self._base_url}: {e}", 502
            ) from e
        if response.status_code >= 400:
            raise LiteLLMError(_error_text(response), response.status_code)
        body: Any = response.json()
        return body if isinstance(body, dict) else {}
