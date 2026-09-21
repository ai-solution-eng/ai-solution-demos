import asyncio
import os

import redis.asyncio as redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://conversation-toolbox-redis:6379/0")


class RedisClient:
    _instance: redis.Redis | None = None
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def get_client(cls) -> redis.Redis:
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = await redis.from_url(
                        REDIS_URL,
                        decode_responses=False,
                        health_check_interval=30,  # keep bytes for audio
                    )
        return cls._instance

    @classmethod
    async def close(cls):
        if cls._instance:
            await cls._instance.close()
            cls._instance = None
