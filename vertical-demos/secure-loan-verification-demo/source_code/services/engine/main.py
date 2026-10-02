"""SLVD engine entrypoint."""
from __future__ import annotations

import logging

import uvicorn

from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def main() -> None:
    uvicorn.run("engine.api:app", host="0.0.0.0", port=8080,
                log_level="info", lifespan="auto")


if __name__ == "__main__":
    main()
