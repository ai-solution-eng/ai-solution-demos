"""Video feed names as the services accept them, and the labels operators see."""

from typing import Annotated

from pydantic import StringConstraints

# Names as /api/v1/video/list returns them: no paths.
VideoName = Annotated[str, StringConstraints(pattern=r"^\w[\w .-]*\.(mp4|mov)$", max_length=200)]


def feed_label(filename: str) -> str:
    """Turns a video file name into the feed name the dashboard shows.

    Args:
        filename: The video file, e.g. ``naval_exercises.mp4``.

    Returns:
        The label, e.g. ``Naval Exercises``; the same rule as the dashboard's drop-downs.
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    words = stem.replace("-", " ").replace("_", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words)
