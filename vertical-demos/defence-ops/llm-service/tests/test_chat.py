"""Tests for model capabilities, video-or-frames and thinking mode, against a fake LiteLLM."""

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import main
import prompts
from litellm_client import AssumedCapabilities, LiteLLMClient, ModelCapabilities

MODELS = {"data": [{"id": "nemotron-vl"}, {"id": "qwen3-vl"}, {"id": "text-only"}]}
MODEL_INFO = {
    "data": [
        {
            "model_name": "nemotron-vl",
            "model_info": {
                "supports_vision": True,
                "supports_video_input": True,
                "supports_reasoning": True,
            },
        },
        {"model_name": "qwen3-vl", "model_info": {"supports_vision": True}},
        {"model_name": "text-only", "model_info": {"supports_vision": False}},
    ]
}


class FakeLiteLLM:
    """Answers like LiteLLM and records each chat payload."""

    def __init__(self, refuse_video: int | None = None, model_info_status: int = 200) -> None:
        self.refuse_video = refuse_video
        self.model_info_status = model_info_status
        self.chats: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test-defence-ops"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=MODELS)
        if request.url.path == "/v1/model/info":
            return httpx.Response(self.model_info_status, json=MODEL_INFO)
        payload = json.loads(request.content)
        self.chats.append(payload)
        content = payload["messages"][-1]["content"]
        kinds = {p["type"] for p in content} if isinstance(content, list) else set()
        if self.refuse_video and "video_url" in kinds:
            return httpx.Response(
                self.refuse_video, json={"error": {"message": "video_url is not supported"}}
            )
        message = {"content": "<think>Two tanks, one burning.</think>Two tanks; one is on fire."}
        return httpx.Response(200, json={"choices": [{"message": message}]})


def make_client(
    fake: FakeLiteLLM,
    key: str | None = "sk-test-defence-ops",
    assumed: AssumedCapabilities | None = None,
) -> LiteLLMClient:
    return LiteLLMClient(
        "http://litellm.test:4000", key, 30, 60, httpx.MockTransport(fake), assumed=assumed
    )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, FakeLiteLLM]]:
    fake = FakeLiteLLM()
    monkeypatch.setattr(main, "client", make_client(fake))

    async def settings() -> dict[str, Any]:
        return {"llm_model": "nemotron-vl"}

    async def clip(_base: str, videos: list[str]) -> str:
        return f"data:video/mp4;base64,{'+'.join(videos)}"

    async def frames(_base: str, videos: list[str], max_images: int) -> list[tuple[str, list[str]]]:
        return [(v, [f"data:image/jpeg;base64,{v}"]) for v in videos][:max_images]

    monkeypatch.setattr(main, "load_demo_config", settings)
    monkeypatch.setattr(main, "fetch_clip", clip)
    monkeypatch.setattr(main, "fetch_frames", frames)
    yield TestClient(main.app), fake


def ask(client: TestClient, **body: Any) -> httpx.Response:
    response: httpx.Response = client.post(
        "/api/v1/llm/chat", json={"message": "What do you see?", **body}
    )
    return response


def test_models_merge_litellm_capabilities() -> None:
    models = asyncio.run(make_client(FakeLiteLLM()).list_models())
    assert models == [
        ModelCapabilities("nemotron-vl", vision=True, video=True, reasoning=True),
        ModelCapabilities("qwen3-vl", vision=True, video=None, reasoning=None),
        ModelCapabilities("text-only", vision=False, video=None, reasoning=None),
    ]


def test_models_without_model_info_have_unknown_capabilities() -> None:
    models = asyncio.run(make_client(FakeLiteLLM(model_info_status=403)).list_models())
    assert [m.id for m in models] == ["nemotron-vl", "qwen3-vl", "text-only"]
    assert all(m.vision is None and m.reasoning is None for m in models)


def test_assumed_capabilities_fill_what_the_endpoint_does_not_declare() -> None:
    assumed = AssumedCapabilities(vision=True, video=True, reasoning=True)
    direct = asyncio.run(
        make_client(FakeLiteLLM(model_info_status=404), assumed=assumed).list_models()
    )
    assert all(m.vision and m.video and m.reasoning for m in direct)  # no /v1/model/info

    proxied = asyncio.run(make_client(FakeLiteLLM(), assumed=assumed).list_models())
    assert proxied == [  # what LiteLLM declares wins
        ModelCapabilities("nemotron-vl", vision=True, video=True, reasoning=True),
        ModelCapabilities("qwen3-vl", vision=True, video=True, reasoning=True),
        ModelCapabilities("text-only", vision=False, video=True, reasoning=True),
    ]


def test_video_model_gets_one_clip(api: tuple[TestClient, FakeLiteLLM]) -> None:
    client, fake = api
    data = ask(client, videos=["convoy.mp4", "tank_footage.mp4"]).json()["data"]
    assert data["context"] == "video"
    parts = fake.chats[0]["messages"][-1]["content"]
    assert [p["type"] for p in parts] == ["text", "video_url"]


@pytest.mark.parametrize("status", [400, 500])  # vLLM refuses with 400, LiteLLM's counter 500
def test_refused_video_falls_back_to_frames_and_is_remembered(
    api: tuple[TestClient, FakeLiteLLM], status: int
) -> None:
    client, fake = api
    fake.refuse_video = status
    data = ask(client, videos=["convoy.mp4"]).json()["data"]
    assert data["context"] == "frames"
    assert any("refused the video clip" in n for n in data["notices"])
    assert [p["type"] for p in fake.chats[-1]["messages"][-1]["content"]] == ["text", "image_url"]

    ask(client, videos=["convoy.mp4"])  # the next question goes straight to frames
    assert len(fake.chats) == 3


def test_unreachable_litellm_is_not_taken_for_a_video_refusal(
    api: tuple[TestClient, FakeLiteLLM],
) -> None:
    client, fake = api
    fake.refuse_video = 502
    assert ask(client, videos=["convoy.mp4"]).status_code == 502
    assert len(fake.chats) == 1


def test_image_model_gets_frames(
    api: tuple[TestClient, FakeLiteLLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = api

    async def settings() -> dict[str, Any]:
        return {"llm_model": "qwen3-vl"}

    monkeypatch.setattr(main, "load_demo_config", settings)
    data = ask(client, videos=["convoy.mp4", "operation.mp4"]).json()["data"]
    assert data["context"] == "frames"
    assert [p["type"] for p in fake.chats[0]["messages"][-1]["content"]] == [
        "text",
        "image_url",
        "image_url",
    ]


def test_thinking_switch_reaches_a_reasoning_model(api: tuple[TestClient, FakeLiteLLM]) -> None:
    client, fake = api
    data = ask(client, enable_thinking=True).json()["data"]
    assert data["thinking"] is True
    assert fake.chats[0]["reasoning_effort"] == "medium"
    assert fake.chats[0]["chat_template_kwargs"] == {"enable_thinking": True}
    assert data["reasoning"] == "Two tanks, one burning."
    assert data["reply"] == "Two tanks; one is on fire."

    ask(client, enable_thinking=False)
    assert fake.chats[1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert fake.chats[1]["reasoning_effort"] == "none"  # what Ollama listens to


def test_thinking_is_not_sent_to_other_models(
    api: tuple[TestClient, FakeLiteLLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = api

    async def settings() -> dict[str, Any]:
        return {"llm_model": "qwen3-vl"}

    monkeypatch.setattr(main, "load_demo_config", settings)
    data = ask(client, enable_thinking=True).json()["data"]
    assert data["thinking"] is False
    assert "reasoning_effort" not in fake.chats[0]
    assert "chat_template_kwargs" not in fake.chats[0]
    assert any("does not support thinking" in n for n in data["notices"])


@pytest.mark.parametrize(
    ("message", "reply", "reasoning"),
    [
        (
            {"content": "Clear.", "reasoning_content": "Checked all feeds."},
            "Clear.",
            "Checked all feeds.",
        ),
        (  # vLLM called directly
            {"content": "Clear.", "reasoning": "Checked all feeds."},
            "Clear.",
            "Checked all feeds.",
        ),
        ({"content": "Scanning...</think>Clear."}, "Clear.", "Scanning..."),
        ({"content": "Clear."}, "Clear.", None),
    ],
)
def test_reasoning_is_split_from_the_answer(
    message: dict[str, Any], reply: str, reasoning: str | None
) -> None:
    assert main._split_reasoning({"choices": [{"message": message}]}) == (reply, reasoning)


@pytest.mark.parametrize("name", ["../../etc/passwd", "convoy.mp4/../x.mp4", "notes.txt"])
def test_video_names_with_paths_are_refused(api: tuple[TestClient, FakeLiteLLM], name: str) -> None:
    client, fake = api
    assert ask(client, videos=[name]).status_code == 422
    assert fake.chats == []


def test_missing_key_explains_the_fix(
    api: tuple[TestClient, FakeLiteLLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = api
    monkeypatch.setattr(main, "client", make_client(fake, key=None))
    response = client.get("/api/v1/llm/models")
    assert response.status_code == 503
    assert "llm.apiKey" in response.json()["detail"]


def test_the_model_is_told_which_image_shows_which_feed(
    api: tuple[TestClient, FakeLiteLLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = api

    async def settings() -> dict[str, Any]:
        return {"llm_model": "qwen3-vl"}

    monkeypatch.setattr(main, "load_demo_config", settings)
    ask(
        client,
        message="SITREP",
        system_prompt="Report as a SITREP.",
        videos=["convoy.mp4", "naval_exercises.mp4"],
        kafka_context=[
            {
                "timestamp": 1793649055,
                "severity": "CRITICAL",
                "location": "Naval Exercises · Harbour",
                "message": "Rocket salvo fired.",
            }
        ],
    )
    system, user = fake.chats[0]["messages"]
    assert system["content"].startswith(prompts.OPERATING_RULES)
    assert system["content"].endswith("Report as a SITREP.")
    text = user["content"][0]["text"]
    assert text.startswith("TASKING: SITREP")
    assert "image 1 Convoy; image 2 Naval Exercises" in text
    assert "- 1950Z CRITICAL | Naval Exercises · Harbour: Rocket salvo fired." in text


def test_the_clip_is_described_in_feed_order(api: tuple[TestClient, FakeLiteLLM]) -> None:
    client, fake = api
    ask(client, videos=["tank_footage.mp4", "convoy.mp4"])
    text = fake.chats[0]["messages"][-1]["content"][0]["text"]
    assert "showing these feeds in this order: 1. Tank Footage; 2. Convoy." in text
    assert "ALERTS: none received." in text


def test_the_rules_stand_without_operator_instructions(
    api: tuple[TestClient, FakeLiteLLM],
) -> None:
    client, fake = api
    ask(client, system_prompt="   ")
    system, user = fake.chats[0]["messages"]
    assert system["content"] == prompts.OPERATING_RULES
    assert "IMAGERY: none attached. Answer from the alerts and the tasking." in user["content"]


def test_frames_note_counts_images_per_feed() -> None:
    note = prompts.frames_note([("convoy.mp4", 2), ("tactical_unit.mp4", 1)])
    assert note.startswith("IMAGERY: 3 still images in this order: images 1-2 Convoy; image 3 ")
    assert "image 3 Tactical Unit." in note
