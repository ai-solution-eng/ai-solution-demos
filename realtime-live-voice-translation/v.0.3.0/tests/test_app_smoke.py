def test_health_ready_and_defaults(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}

    defaults = client.get("/defaults")
    assert defaults.status_code == 200
    payload = defaults.json()
    assert payload["src"]
    assert payload["tgt"]
    assert "whisper" in payload
    assert "llm" in payload


def test_create_room_generates_room_id_and_state(client):
    created = client.post("/api/rooms", json={})
    assert created.status_code == 200
    room_id = created.json()["room_id"]
    assert room_id

    room_state = client.get(f"/api/rooms/{room_id}")
    assert room_state.status_code == 200
    payload = room_state.json()
    assert payload["room_id"] == room_id
    assert payload["segment_count"] == 0
    assert payload["recording_state"] == "idle"


def test_create_room_preserves_valid_requested_room_id(client):
    requested_room_id = "demo-room-1234"
    created = client.post("/api/rooms", json={"room_id": requested_room_id})
    assert created.status_code == 200
    assert created.json() == {"room_id": requested_room_id}


def test_recording_start_and_pause_update_room_state(client):
    room_id = client.post("/api/rooms", json={"room_id": "recording-room-1234"}).json()[
        "room_id"
    ]

    started = client.post(
        f"/api/rooms/{room_id}/recording/start",
        json={"client_session_id": "presenter-session"},
    )
    assert started.status_code == 200
    started_payload = started.json()
    assert started_payload["recording_state"] == "recording"
    assert started_payload["recording_session_id"]

    paused = client.post(
        f"/api/rooms/{room_id}/recording/pause",
        json={"client_session_id": "presenter-session"},
    )
    assert paused.status_code == 200
    assert paused.json()["recording_state"] == "paused"

    room_state = client.get(f"/api/rooms/{room_id}")
    assert room_state.status_code == 200
    assert room_state.json()["recording_state"] == "paused"


def test_export_job_status_returns_404_for_unknown_job(client):
    response = client.get("/api/export-package/status/missing-job")
    assert response.status_code == 404
