"""What the model is told: fixed operating rules, then the tasking, the imagery and the alerts."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from common.feeds import feed_label

# Sent ahead of the dashboard's assistant instructions, which any operator can rewrite or clear,
# so that answers stay grounded in what was supplied and in a military register.
OPERATING_RULES = """\
You are the intelligence assistant in a military operations centre, supporting the watch officer.
1. Use only the attached imagery, the alerts and the tasking. Where they do not show something, \
say "not observed" or "unknown". Never invent units, call signs, grid references, times or \
casualty figures.
2. Mark what is OBSERVED and what is ASSESSED, and give each assessment a confidence: high, \
medium or low.
3. Bottom line up front. Short declarative sentences, standard military terms and abbreviations, \
no pleasantries or filler.
4. Refer to imagery by the feed names listed under IMAGERY, and to alerts by time and location.
5. Use British English and 24-hour UTC times written as 1950Z.
6. This is a demonstration on sample footage, not an operational system: recommend protective, \
reporting and collection actions, never targeting or weapon-employment details."""

# video-service builds clips from 8 frames per feed at 2 fps, and takes stills about 30 frames
# (roughly a second) apart.
CLIP_SECONDS_PER_FEED = 4
MAX_ALERTS = 20


class Alert(BaseModel):
    """One tactical alert from the Kafka ticker."""

    timestamp: float | None = Field(default=None, ge=0)
    severity: str = Field(default="INFO", max_length=20)
    location: str | None = Field(default=None, max_length=200)
    message: str = Field(default="", max_length=2000)


def system_message(instructions: str | None) -> str:
    """The operating rules, followed by the operations centre's own instructions if any."""
    if instructions and instructions.strip():
        return (
            f"{OPERATING_RULES}\n\nINSTRUCTIONS FROM THE OPERATIONS CENTRE:\n{instructions.strip()}"
        )
    return OPERATING_RULES


def clip_note(videos: list[str]) -> str:
    """Describes one clip of several feeds, so the model knows which part shows which feed."""
    order = "; ".join(f"{i}. {feed_label(v)}" for i, v in enumerate(videos, 1))
    return (
        f"IMAGERY: one video clip at 2 frames per second, about {CLIP_SECONDS_PER_FEED} seconds "
        f"per feed, showing these feeds in this order: {order}."
    )


def frames_note(feeds: list[tuple[str, int]]) -> str:
    """Describes the attached stills as (video, number of frames) in the order attached."""
    spans: list[str] = []
    first = 1
    for video, count in feeds:
        last = first + count - 1
        images = f"image {first}" if count == 1 else f"images {first}-{last}"
        spans.append(f"{images} {feed_label(video)}")
        first = last + 1
    total = first - 1
    plural = "image" if total == 1 else "images"
    return (
        f"IMAGERY: {total} still {plural} in this order: {'; '.join(spans)}. Images of the same "
        "feed are about a second apart, oldest first."
    )


def no_imagery_note(videos: list[str]) -> str:
    """Says that no imagery is attached, and whether the operator had feeds selected."""
    if not videos:
        return "IMAGERY: none attached. Answer from the alerts and the tasking."
    names = ", ".join(feed_label(v) for v in videos)
    return (
        f"IMAGERY: none attached. The operator selected {names}, but their imagery could not be "
        "given to you: do not describe them."
    )


def _alert_time(timestamp: float | None) -> str:
    if timestamp is None:
        return "time unknown"
    return datetime.fromtimestamp(timestamp, UTC).strftime("%H%MZ")


def alerts_note(alerts: list[Alert]) -> str:
    """Lists the latest alerts, oldest first, with their time, severity and location."""
    latest = alerts[-MAX_ALERTS:]
    if not latest:
        return "ALERTS: none received."
    lines = [
        f"- {_alert_time(a.timestamp)} {a.severity}"
        f"{' | ' + a.location if a.location else ''}: {a.message}"
        for a in latest
    ]
    return f"ALERTS (latest {len(latest)} from the Kafka datalink, oldest first):\n" + "\n".join(
        lines
    )


def user_text(tasking: str, imagery: str, alerts: list[Alert]) -> str:
    """The text part of the user message: tasking, what imagery is attached, and the alerts."""
    return f"TASKING: {tasking}\n\n{imagery}\n\n{alerts_note(alerts)}"
