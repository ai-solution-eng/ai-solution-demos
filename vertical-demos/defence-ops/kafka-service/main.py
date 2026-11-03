"""Kafka Service for streaming messages and generating sample tactical alerts."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Annotated, Any

import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.helpers import create_ssl_context
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from alerts import profile_for, sample_alerts
from common.demo_config import load_demo_config
from common.feeds import VideoName, feed_label
from common.logging_config import setup_logging
from settings import get_settings

# Configure structured logging
setup_logging("kafka-service")
logger = structlog.get_logger(__name__)
settings = get_settings()

app = FastAPI(title="Kafka Service")
stream_slots = asyncio.Semaphore(settings.max_stream_clients)


class KafkaNotConfiguredError(Exception):
    """The broker, topic or credentials are missing."""


async def get_kafka_target() -> tuple[str, str]:
    """Reads the broker and topic: saved on System Config, else the chart's defaults.

    Raises:
        KafkaNotConfiguredError: Either is missing.
    """
    config = await load_demo_config()
    broker, topic = config.get("kafka_broker"), config.get("kafka_topic")
    if not broker or not topic:
        raise KafkaNotConfiguredError(
            "Kafka broker or topic not configured. An administrator sets them on System Config, "
            "or kafka.broker and kafka.topic in the app's Helm values."
        )
    return str(broker), str(topic)


def connection_kwargs(broker: str) -> dict[str, Any]:
    """Client options shared by the consumer and the producer.

    Raises:
        KafkaNotConfiguredError: A SASL protocol is set but the credentials are missing.
    """
    kwargs: dict[str, Any] = {
        "bootstrap_servers": broker,
        "security_protocol": settings.kafka_security_protocol,
    }
    if settings.kafka_security_protocol.startswith("SASL"):
        if not settings.kafka_sasl_username or not settings.kafka_sasl_password:
            raise KafkaNotConfiguredError(
                "No Kafka credentials: set kafka.username and kafka.password, or "
                "kafka.existingSecret, in the app's Helm values."
            )
        kwargs |= {
            "sasl_mechanism": settings.kafka_sasl_mechanism,
            "sasl_plain_username": settings.kafka_sasl_username,
            "sasl_plain_password": settings.kafka_sasl_password.get_secret_value(),
        }
    if settings.kafka_security_protocol.endswith("SSL"):
        kwargs["ssl_context"] = create_ssl_context(cafile=settings.kafka_ssl_cafile)
    return kwargs


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/api/v1/kafka/stream")
async def kafka_stream(request: Request) -> EventSourceResponse:
    """Subscribes to the configured Kafka topic and pushes Server-Sent Events.

    Args:
        request: The FastAPI request object to monitor for disconnection.

    Returns:
        An EventSourceResponse for SSE streaming.
    """
    try:
        broker, topic = await get_kafka_target()
        kwargs = connection_kwargs(broker)
    except KafkaNotConfiguredError as e:
        message = str(e)

        async def not_configured() -> AsyncGenerator[dict[str, str], None]:
            yield {"event": "error", "data": message}

        return EventSourceResponse(not_configured())

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        if stream_slots.locked():
            yield {
                "event": "error",
                "data": f"Too many live alert streams open ({settings.max_stream_clients}). "
                "Close other Ops Centre tabs and reload.",
            }
            return
        async with stream_slots:
            consumer: AIOKafkaConsumer | None = None
            try:
                # Subscribed without a group id: no JoinGroup or rebalancing, and with no
                # committed offsets it starts at the end. An unsubscribed consumer never asks the
                # Data Fabric gateway for the topic's metadata, so it would find no partitions.
                consumer = AIOKafkaConsumer(
                    topic, **kwargs, auto_offset_reset="latest", enable_auto_commit=False
                )
                logger.info("starting_kafka_consumer", broker=broker, topic=topic)
                await consumer.start()

                partitions = consumer.partitions_for_topic(topic)
                for _ in range(10):
                    if partitions:
                        break
                    await asyncio.sleep(0.2)
                    partitions = consumer.partitions_for_topic(topic)
                if not partitions:
                    logger.error("no_topic_metadata", topic=topic)
                    yield {"event": "error", "data": f"Error: no metadata for topic {topic}."}
                    return

                yield {"event": "connected", "data": f"Connected to {topic}"}

                async for msg in consumer:
                    if await request.is_disconnected():
                        break
                    if msg.value is not None:
                        yield {"event": "message", "data": msg.value.decode("utf-8", "replace")}
            except asyncio.CancelledError:
                logger.info("sse_client_disconnected")
            except Exception as e:
                logger.error("kafka_consumer_error", error=str(e), exc_info=True)
                yield {"event": "error", "data": f"Consumer Error: {e}"}
            finally:
                if consumer:
                    await consumer.stop()

    return EventSourceResponse(event_generator())


class GenerateRequest(BaseModel):
    """Which feeds the operator has on screen, so the sample alerts can match them."""

    feeds: list[VideoName] = Field(default_factory=list, max_length=4)


@app.post("/api/v1/kafka/generate")
async def generate_sample_messages(req: Annotated[GenerateRequest | None, Body()] = None) -> Any:
    """Generates 3-5 sample tactical alerts about the given feeds and produces them to Kafka.

    Args:
        req: The feeds on screen; without any, or with none recognised, the alerts are general.

    Returns:
        A dictionary with status and message.
    """
    feeds = req.feeds if req else []
    try:
        broker, topic = await get_kafka_target()
        kwargs = connection_kwargs(broker)
    except KafkaNotConfiguredError as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

    producer = AIOKafkaProducer(**kwargs)
    try:
        await producer.start()
        alerts = sample_alerts(feeds)
        for alert in alerts:
            await producer.send_and_wait(topic, json.dumps(alert).encode("utf-8"))
            logger.info("sent_sample_alert", topic=topic, location=alert["location"])
        matched = sorted({feed_label(f) for f in feeds if profile_for(f)})
        about = f"for {', '.join(matched)}" if matched else "(general, no matching feed selected)"
        return {"status": "success", "message": f"Generated {len(alerts)} alerts {about}"}
    except Exception as e:
        logger.error("producer_error", error=str(e), exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": f"Producer Error: {e}"}
        )
    finally:
        await producer.stop()
