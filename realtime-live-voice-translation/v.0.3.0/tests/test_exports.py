from app.routes import exports as exports_routes
from app.schemas.api import ExportSegment, MeetingPackageResult
from app.state.export_jobs import EXPORT_JOBS
from app.state.rooms import ROOMS, build_room_state


def test_export_documents_uses_mocked_service(client, monkeypatch):
    async def fake_generate_export_documents(*, items, llm_client, llm_model):
        assert len(items) == 1
        assert llm_model
        return {
            "languages": ["en", "es"],
            "documents": [{"filename": "meeting_summary_en.txt", "content": "summary"}],
        }

    monkeypatch.setattr(
        exports_routes,
        "generate_export_documents",
        fake_generate_export_documents,
    )

    response = client.post(
        "/api/export-documents",
        json={
            "transcript": [
                {
                    "original": "Hello world",
                    "translation": "Hola mundo",
                    "src": "en",
                    "tgt": "es",
                    "ts_ms": 1000,
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["languages"] == ["en", "es"]


def test_export_package_returns_zip_response(client, monkeypatch):
    async def fake_build_export_package_bytes(**kwargs):
        assert kwargs["audio_bytes"] == b"audio-bytes"
        return b"zip-bytes", "meeting_package_test.zip"

    monkeypatch.setattr(
        exports_routes,
        "build_export_package_bytes",
        fake_build_export_package_bytes,
    )

    response = client.post(
        "/api/export-package",
        files={"audio": ("meeting.webm", b"audio-bytes", "audio/webm")},
        data={
            "transcript_json": "[]",
            "documents_transcript_json": "[]",
            "llm_json": "{}",
            "whisper_json": "{}",
        },
    )

    assert response.status_code == 200
    assert response.content == b"zip-bytes"
    assert response.headers["content-type"] == "application/zip"
    assert "meeting_package_test.zip" in response.headers["content-disposition"]


def test_export_package_download_returns_completed_job(client):
    EXPORT_JOBS["job-123"] = {
        "status": "completed",
        "zip_bytes": b"archive",
        "archive_name": "done.zip",
    }

    response = client.get("/api/export-package/download/job-123")

    assert response.status_code == 200
    assert response.content == b"archive"
    assert "done.zip" in response.headers["content-disposition"]


def test_room_export_documents_returns_empty_when_room_has_no_segments(client):
    room = build_room_state("export-room-1234")
    ROOMS[room["room_id"]] = room

    response = client.post(
        f"/api/rooms/{room['room_id']}/export-documents",
        json={"target_language": "es"},
    )

    assert response.status_code == 200
    assert response.json() == {"languages": ["en"], "documents": [], "segments": []}


def test_room_export_package_start_rejects_when_no_recording(client):
    room = build_room_state("export-room-no-recording")
    ROOMS[room["room_id"]] = room

    response = client.post(
        f"/api/rooms/{room['room_id']}/export-package/start",
        json={"target_language": "es"},
    )

    assert response.status_code == 409
    assert "only available after a recording has been captured" in response.json()["detail"]


def test_room_export_package_start_queues_job_for_recorded_room(client, monkeypatch):
    room = build_room_state("export-room-recorded")
    room["recording_state"] = "stopped"
    room["recording_audio_bytes"] = b"wav-bytes"
    room["recording_filename"] = "meeting_audio.wav"
    room["recording_mime_type"] = "audio/wav"
    room["recording_transcript_items"] = [
        {
            "original": "Hello world",
            "translation": "Hola mundo",
            "src": "en",
            "tgt": "es",
            "ts_ms": 1000,
        }
    ]
    room["documents_source_items"] = list(room["recording_transcript_items"])
    ROOMS[room["room_id"]] = room

    calls = {}

    async def fake_cleanup_old_export_jobs():
        calls["cleanup"] = True

    async def fake_create_export_job():
        return "job-room-123"

    async def fake_run_export_job(**kwargs):
        calls["run_kwargs"] = kwargs

    def fake_create_task(coro):
        calls["task_created"] = True
        coro.close()
        return object()

    monkeypatch.setattr(exports_routes, "cleanup_old_export_jobs", fake_cleanup_old_export_jobs)
    monkeypatch.setattr(exports_routes, "create_export_job", fake_create_export_job)
    monkeypatch.setattr(exports_routes, "run_export_job", fake_run_export_job)
    monkeypatch.setattr(exports_routes.asyncio, "create_task", fake_create_task)

    response = client.post(
        f"/api/rooms/{room['room_id']}/export-package/start",
        json={"target_language": "es"},
    )

    assert response.status_code == 200
    assert response.json() == {"job_id": "job-room-123", "status": "queued"}
    assert calls["cleanup"] is True
    assert calls["task_created"] is True


def test_room_export_documents_uses_mocked_builder_for_segmented_room(client, monkeypatch):
    room = build_room_state("export-room-segments")
    room["segments"] = [
        {
            "segment_id": "seg-1",
            "revision": 1,
            "status": "final",
            "is_final": True,
            "original": "Bonjour",
            "src": "fr",
            "ts_ms": 1000,
            "translations": {"en": "Hello"},
        }
    ]
    room["segment_index"] = {"seg-1": 0}
    ROOMS[room["room_id"]] = room

    async def fake_build_documents_from_export_segments(**kwargs):
        assert kwargs["export_languages"][0] == "en"
        return MeetingPackageResult(
            languages=["en", "fr"],
            documents=[{"filename": "meeting_summary_en.txt", "language": "en", "kind": "summary", "content": "summary"}],
            segments=[
                ExportSegment(
                    id=1,
                    start_s=0.0,
                    end_s=1.0,
                    start_label="00:00.000",
                    end_label="00:01.000",
                    original="Bonjour",
                    english="Hello",
                )
            ],
        )

    monkeypatch.setattr(
        exports_routes,
        "build_documents_from_export_segments",
        fake_build_documents_from_export_segments,
    )

    response = client.post(
        f"/api/rooms/{room['room_id']}/export-documents",
        json={"target_language": "fr"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["languages"] == ["en", "fr"]
    assert payload["documents"][0]["filename"] == "meeting_summary_en.txt"
