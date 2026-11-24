"""Tests for the Kafka connection options built from the environment."""

import asyncio

import pytest
from pydantic import SecretStr

import main
from settings import KafkaSettings


def use(monkeypatch: pytest.MonkeyPatch, **values: object) -> None:
    monkeypatch.setattr(main, "settings", KafkaSettings.model_validate(values))


def test_sasl_without_credentials_explains_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch)
    with pytest.raises(main.KafkaNotConfiguredError, match="kafka.username"):
        main.connection_kwargs("df.example:9092")


def test_sasl_plaintext_uses_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    use(
        monkeypatch,
        kafka_sasl_username="defence-ops",
        kafka_sasl_password=SecretStr("not-a-real-pw"),
    )
    kwargs = main.connection_kwargs("df.example:9092")
    assert kwargs["security_protocol"] == "SASL_PLAINTEXT"
    assert kwargs["sasl_mechanism"] == "PLAIN"
    assert kwargs["sasl_plain_username"] == "defence-ops"
    assert kwargs["sasl_plain_password"] == "not-a-real-pw"
    assert "ssl_context" not in kwargs


def test_sasl_ssl_adds_a_tls_context(monkeypatch: pytest.MonkeyPatch) -> None:
    use(
        monkeypatch,
        kafka_sasl_username="defence-ops",
        kafka_sasl_password=SecretStr("not-a-real-pw"),
        kafka_security_protocol="SASL_SSL",
    )
    assert "ssl_context" in main.connection_kwargs("df.example:9093")


def test_missing_topic_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def settings() -> dict[str, str]:
        return {"kafka_broker": "df.example:9092"}

    monkeypatch.setattr(main, "load_demo_config", settings)
    with pytest.raises(main.KafkaNotConfiguredError, match="System Config"):
        asyncio.run(main.get_kafka_target())


class FakeMessage:
    value = b'{"alert": "Perimeter breach"}'


class FakeConsumer:
    """Stands in for AIOKafkaConsumer: knows the topic's partitions only if subscribed."""

    created: list["FakeConsumer"] = []

    def __init__(self, *topics: str, **kwargs: object) -> None:
        self.topics = topics
        self.kwargs = kwargs
        FakeConsumer.created.append(self)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def partitions_for_topic(self, topic: str) -> set[int] | None:
        return {0} if topic in self.topics else None

    def __aiter__(self) -> "FakeConsumer":
        self.sent = False
        return self

    async def __anext__(self) -> FakeMessage:
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return FakeMessage()


class FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


def test_stream_subscribes_so_the_gateway_sends_topic_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def target() -> tuple[str, str]:
        return "df.example:9092", "tactical-alerts"

    use(
        monkeypatch,
        kafka_sasl_username="defence-ops",
        kafka_sasl_password=SecretStr("not-a-real-pw"),
    )
    monkeypatch.setattr(main, "get_kafka_target", target)
    monkeypatch.setattr(main, "AIOKafkaConsumer", FakeConsumer)

    async def events() -> list[object]:
        response = await main.kafka_stream(FakeRequest())  # type: ignore[arg-type]
        return [event async for event in response.body_iterator]

    received = asyncio.run(events())
    consumer = FakeConsumer.created[-1]
    assert consumer.topics == ("tactical-alerts",)
    assert "group_id" not in consumer.kwargs
    assert [e["event"] for e in received if isinstance(e, dict)] == ["connected", "message"]
