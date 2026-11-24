"""Visual context for a question: one short clip or a few frames, built by video-service."""

import asyncio
from typing import Any

import httpx


class MediaError(Exception):
    """video-service could not provide the clip or frames."""


async def _get(base_url: str, path: str, params: dict[str, str | int]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.get(f"{base_url.rstrip('/')}{path}", params=params)
    except httpx.HTTPError as e:
        raise MediaError(f"video-service unreachable: {e}") from e
    if response.status_code >= 400:
        raise MediaError(
            f"video-service answered HTTP {response.status_code}: {response.text[:200]}"
        )
    body: Any = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise MediaError("video-service returned no data")
    return data


async def fetch_clip(base_url: str, videos: list[str]) -> str:
    """Builds one Motion-JPEG AVI from all the feeds (8 frames each, 512x512, 2 fps).

    About 1 s per feed (measured 2026-11-02: 0.5-1.7 s for one feed, 8.6 s for all ten).

    Returns:
        The clip as a data URL.
    """
    data = await _get(base_url, "/api/v1/video/combined-context", {"vids": ",".join(videos)})
    clip = data.get("video_b64")
    if not isinstance(clip, str):
        raise MediaError("video-service returned no clip")
    return clip


async def _feed_frames(base_url: str, video: str, count: int) -> list[str]:
    data = await _get(base_url, "/api/v1/video/combined-frames", {"vids": video, "count": count})
    frames = data.get("images_b64")
    return [f for f in frames if isinstance(f, str)] if isinstance(frames, list) else []


async def fetch_frames(
    base_url: str, videos: list[str], max_images: int
) -> list[tuple[str, list[str]]]:
    """Captures still frames per feed, spread so every feed is represented within max_images.

    Each feed is fetched on its own, so the model can be told which images show which feed.

    Returns:
        (video, JPEG data URLs) for each feed that gave frames, in the order asked, with at most
        max_images frames in all.

    Raises:
        MediaError: No feed gave any frames.
    """
    per_feed = max(1, max_images // len(videos))
    results = await asyncio.gather(
        *(_feed_frames(base_url, v, per_feed) for v in videos), return_exceptions=True
    )
    feeds: list[tuple[str, list[str]]] = []
    budget = max_images
    for video, result in zip(videos, results, strict=True):
        if isinstance(result, BaseException) or not result or budget <= 0:
            continue
        feeds.append((video, result[:budget]))
        budget -= len(feeds[-1][1])
    if not feeds:
        first_error = next((r for r in results if isinstance(r, BaseException)), None)
        raise MediaError(str(first_error) if first_error else "video-service returned no frames")
    return feeds
