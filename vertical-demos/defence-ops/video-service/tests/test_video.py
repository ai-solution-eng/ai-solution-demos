"""Tests for video name handling and the upload limit."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    assets, uploads = tmp_path / "assets", tmp_path / "uploads"
    assets.mkdir()
    uploads.mkdir()
    (assets / "convoy.mp4").write_bytes(b"baked")
    (tmp_path / "secret.mp4").write_bytes(b"outside")
    monkeypatch.setattr(main, "ASSETS_DIR", assets)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    yield assets, uploads


def test_bare_names_resolve(dirs: tuple[Path, Path]) -> None:
    assets, _ = dirs
    assert main._resolve_video("convoy.mp4") == assets / "convoy.mp4"


@pytest.mark.parametrize(
    "name", ["../secret.mp4", "assets/convoy.mp4", "/etc/passwd", "convoy.txt", "missing.mp4"]
)
def test_anything_but_a_known_video_is_not_found(dirs: tuple[Path, Path], name: str) -> None:
    assert main._resolve_video(name) is None


def test_combined_frames_refuses_too_many_feeds(dirs: tuple[Path, Path]) -> None:
    vids = ",".join(f"v{i}.mp4" for i in range(main.MAX_FEEDS + 1))
    response = TestClient(main.app).get("/api/v1/video/combined-frames", params={"vids": vids})
    assert response.status_code == 400


def test_frame_counts_are_bounded(dirs: tuple[Path, Path]) -> None:
    response = TestClient(main.app).get(
        "/api/v1/video/combined-context", params={"vids": "convoy.mp4", "count": 10_000}
    )
    assert response.status_code == 422


def test_upload_replaces_the_previous_one(dirs: tuple[Path, Path]) -> None:
    _, uploads = dirs
    client = TestClient(main.app)
    first = client.post("/api/v1/video/upload", files={"file": ("a.mp4", b"one", "video/mp4")})
    second = client.post("/api/v1/video/upload", files={"file": ("b.MOV", b"two", "video/mp4")})
    assert first.status_code == second.status_code == 200
    assert [p.name for p in uploads.iterdir()] == [second.json()["data"]["filename"]]
    assert second.json()["data"]["filename"].endswith(".mov")


def test_oversized_upload_is_refused_and_leaves_nothing(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, uploads = dirs
    monkeypatch.setattr(main, "MAX_UPLOAD_MB", 1)
    big = b"\0" * (1024 * 1024 + 1)
    response = TestClient(main.app).post(
        "/api/v1/video/upload", files={"file": ("big.mp4", big, "video/mp4")}
    )
    assert response.status_code == 413
    assert list(uploads.iterdir()) == []


def test_non_video_upload_is_refused(dirs: tuple[Path, Path]) -> None:
    response = TestClient(main.app).post(
        "/api/v1/video/upload", files={"file": ("run.sh", b"#!/bin/sh", "text/plain")}
    )
    assert response.status_code == 400
