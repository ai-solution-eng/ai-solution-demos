"""Tests for the shared settings loader: saved System Config over the chart's defaults."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from common import demo_config


def load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, saved: dict[str, str] | None
) -> dict[str, Any]:
    path = tmp_path / "config.json"
    if saved is not None:
        path.write_text(json.dumps({"demo_services": saved}))
    monkeypatch.setattr(demo_config, "CONFIG_PATH", path)
    monkeypatch.setenv("LLM_MODEL", "default-vlm")
    monkeypatch.setenv("KAFKA_BROKER", "kafka.example.com:9092")
    monkeypatch.setenv("KAFKA_TOPIC", "default-alerts")
    return asyncio.run(demo_config.load_demo_config())


def test_defaults_apply_before_anything_is_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert load(monkeypatch, tmp_path, None) == {
        "llm_model": "default-vlm",
        "kafka_broker": "kafka.example.com:9092",
        "kafka_topic": "default-alerts",
    }


def test_saved_values_win_and_empty_ones_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load(monkeypatch, tmp_path, {"llm_model": "chosen-vlm", "kafka_topic": ""})
    assert config["llm_model"] == "chosen-vlm"
    assert config["kafka_topic"] == "default-alerts"
