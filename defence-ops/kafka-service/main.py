"""Kafka Service for streaming messages and generating sample tactical alerts."""

import asyncio
import json
import os
import random
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from common.logging_config import setup_logging

# Configure structured logging
setup_logging("kafka-service")
logger = structlog.get_logger(__name__)

app = FastAPI(title="Kafka Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ADMIN_URL = os.environ.get("ADMIN_SERVICE_URL", "http://app-ui:3000")


async def get_kafka_config() -> Dict[str, Any]:
    """Fetches Kafka configuration from the app-ui.
    
    Returns:
        A dictionary containing the Kafka configuration.
    """
    urls = [ADMIN_URL, "http://app-ui:3000"]
    last_error = None

    for url in urls:
        try:
            logger.debug("fetching_config", url=url)
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.get(f"{url}/api/v1/admin/demo-config", timeout=2.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success":
                        logger.info("config_loaded_successfully", url=url)
                        return data.get("data", {})
                    else:
                        logger.warning("config_response_error", url=url, status=data.get("status"))
                else:
                    logger.warning("config_http_error", url=url, status_code=resp.status_code)
        except Exception as e:
            last_error = str(e)
            logger.debug("config_fetch_failed", url=url, error=str(e))

    logger.error("all_config_fetches_failed", last_error=last_error)
    return {}


@app.get("/health")
async def health_check() -> Dict[str, str]:
    """Health check endpoint.
    
    Returns:
        A dictionary indicating the health status.
    """
    return {"status": "healthy"}


@app.get("/api/v1/kafka/stream")
async def kafka_stream(request: Request) -> EventSourceResponse:
    """Subscribes to the configured Kafka topic and pushes Server-Sent Events.
    
    Args:
        request: The FastAPI request object to monitor for disconnection.
        
    Returns:
        An EventSourceResponse for SSE streaming.
    """
    logger.debug("kafka_stream_request_received")
    config = await get_kafka_config()
    broker = config.get("kafka_broker")
    topic = config.get("kafka_topic")

    username = config.get("kafka_username")
    password = config.get("kafka_password")

    if not broker or not topic:
        logger.debug("kafka_not_configured", config_keys=list(config.keys()))

        async def error_generator():
            yield {
                "event": "error",
                "data": (
                    "Kafka Broker or Topic not configured. "
                    "Please set them in Advanced Demo Settings."
                ),
            }

        return EventSourceResponse(error_generator())

    async def event_generator() -> AsyncGenerator[Dict[str, str], None]:
        consumer: Optional[AIOKafkaConsumer] = None
        try:
            # Setup consumer with manual assignment to avoid JoinGroup/rebalance issues
            consumer_kwargs: Dict[str, Any] = {
                "bootstrap_servers": broker,
                "auto_offset_reset": "latest",
                "enable_auto_commit": False,
            }
            if username and password:
                consumer_kwargs["security_protocol"] = "SASL_PLAINTEXT"
                consumer_kwargs["sasl_mechanism"] = "PLAIN"
                consumer_kwargs["sasl_plain_username"] = username
                consumer_kwargs["sasl_plain_password"] = password

            logger.info("starting_kafka_consumer_manual", broker=broker, topic=topic)
            consumer = AIOKafkaConsumer(**consumer_kwargs)
            await consumer.start()

            # Fetch partitions metadata for target topic
            partitions = consumer.partitions_for_topic(topic)
            if not partitions:
                for _ in range(10):
                    await asyncio.sleep(0.2)
                    partitions = consumer.partitions_for_topic(topic)
                    if partitions:
                        break

            if not partitions:
                logger.error("no_topic_metadata", topic=topic)
                yield {"event": "error", "data": f"Error: Metadata for topic {topic} not found."}
                return

            # Manually assign all partitions of the topic
            tp_list = [TopicPartition(topic, p) for p in partitions]
            consumer.assign(tp_list)

            # Ensure we start from the very latest message
            await consumer.seek_to_end(*tp_list)

            # Send initial connection success event
            yield {"event": "connected", "data": f"Connected to {topic}"}

            # Consume messages
            async for msg in consumer:
                if await request.is_disconnected():
                    break
                payload = msg.value.decode("utf-8")
                yield {"event": "message", "data": payload}

        except asyncio.CancelledError:
            logger.info("sse_client_disconnected")
        except Exception as e:
            logger.error("kafka_consumer_error", error=str(e), exc_info=True)
            yield {"event": "error", "data": f"Consumer Error: {str(e)}"}
        finally:
            if consumer:
                await consumer.stop()

    return EventSourceResponse(event_generator())


@app.post("/api/v1/kafka/generate")
async def generate_sample_messages() -> Any:
    """Generates sample tactical alerts and produces them to Kafka.
    
    Returns:
        A dictionary with status and message.
    """
    config = await get_kafka_config()
    broker = config.get("kafka_broker")
    topic = config.get("kafka_topic")
    username = config.get("kafka_username")
    password = config.get("kafka_password")

    if not broker or not topic:
        return JSONResponse(
            status_code=400, content={"status": "error", "message": "Kafka not configured"}
        )

    producer_kwargs: Dict[str, Any] = {
        "bootstrap_servers": broker,
    }
    if username and password:
        producer_kwargs["security_protocol"] = "SASL_PLAINTEXT"
        producer_kwargs["sasl_mechanism"] = "PLAIN"
        producer_kwargs["sasl_plain_username"] = username
        producer_kwargs["sasl_plain_password"] = password

    producer = AIOKafkaProducer(**producer_kwargs)
    try:
        await producer.start()

        severities = ["CRITICAL", "WARNING", "INFO"]
        locations = [
            "Sector 7G",
            "North Perimeter",
            "Command Centre",
            "Fuel Depot",
            "South Outpost",
            "East Ridge",
            "Observation Deck",
            "Hangar A-1",
            "Comms Relay",
            "Tactical Bay",
        ]
        objects = [
            "Unidentified drone",
            "Inbound aircraft",
            "Suspicious vehicle",
            "Thermal signature",
            "Unknown signal",
        ]
        areas = ["restricted airspace", "perimeter fence", "approach vector 4", "landing zone echo"]
        systems = ["Pressure sensors", "Optical sensors", "Power grid", "Uplink signal", "Cooling"]
        reports = ["Visual contact confirmed", "Lost contact", "Signal interference", "All clear"]

        templates = [
            lambda: f"{random.choice(objects)} detected in {random.choice(areas)}.",
            lambda: f"{random.choice(systems)} anomaly reported.",
            lambda: f"Intrusion alarm triggered near {random.choice(locations)}.",
            lambda: f"Priority relay: {random.choice(reports)}.",
            lambda: f"Automated scan of {random.choice(locations)} completed.",
        ]

        # Pick 3-5 random alerts
        count = random.randint(3, 5)
        to_send = []
        for _ in range(count):
            to_send.append(
                {
                    "severity": random.choice(severities),
                    "location": random.choice(locations),
                    "message": random.choice(templates)(),
                }
            )

        for alert in to_send:
            payload = {
                "timestamp": time.time(),
                "severity": alert["severity"],
                "location": alert["location"],
                "message": alert["message"],
            }
            await producer.send_and_wait(topic, json.dumps(payload).encode("utf-8"))
            logger.info("sent_sample_alert", topic=topic, alert=alert["message"])

        return {"status": "success", "message": f"Generated {len(to_send)} sample alerts"}
    except Exception as e:
        logger.error("producer_error", error=str(e), exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": f"Producer Error: {str(e)}"}
        )
    finally:
        await producer.stop()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=True if os.environ.get("ENV") == "development" else False,
    )

