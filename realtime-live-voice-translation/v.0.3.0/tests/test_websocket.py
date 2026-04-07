import pytest

from app.realtime import websocket as websocket_routes


class FakeVADIterator:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return None

    def reset_states(self):
        pass


@pytest.fixture
def mock_vad(monkeypatch):
    monkeypatch.setattr(websocket_routes, "VADIterator", FakeVADIterator)
    monkeypatch.setattr(websocket_routes, "model", object())


def test_presenter_join_receives_joined_room_state_and_snapshot(client, mock_vad):
    with client.websocket_connect("/ws/translate") as websocket:
        websocket.send_json(
            {
                "type": "join",
                "role": "presenter",
                "client_session_id": "presenter-session",
                "room_id": "demo-room-1234",
            }
        )

        joined = websocket.receive_json()
        room_state = websocket.receive_json()
        snapshot = websocket.receive_json()

    assert joined["type"] == "joined"
    assert joined["role"] == "presenter"
    assert joined["room_id"] == "demo-room-1234"

    assert room_state["type"] == "room_state"
    assert room_state["room_id"] == "demo-room-1234"
    assert room_state["recording_state"] == "idle"

    assert snapshot["type"] == "snapshot"
    assert snapshot["role"] == "presenter"
    assert snapshot["room_id"] == "demo-room-1234"
    assert snapshot["segments"] == []


def test_presenter_config_message_returns_ack_and_updated_state(client, mock_vad):
    with client.websocket_connect("/ws/translate") as websocket:
        websocket.send_json(
            {
                "type": "join",
                "role": "presenter",
                "client_session_id": "presenter-session",
                "room_id": "config-room-1234",
            }
        )
        websocket.receive_json()
        websocket.receive_json()
        websocket.receive_json()

        websocket.send_json(
            {
                "type": "config",
                "src": "fr",
                "tgt": "de",
                "whisper": {"base_url": "http://whisper.local", "model": "whisper-test"},
                "llm": {"base_url": "http://llm.local", "model": "llm-test"},
            }
        )

        ack = websocket.receive_json()
        room_state = websocket.receive_json()
        snapshot = websocket.receive_json()

    assert ack["type"] == "ack"
    assert ack["src"] == "fr"
    assert ack["tgt"] == "de"
    assert ack["whisper"]["model"] == "whisper-test"
    assert ack["llm"]["model"] == "llm-test"

    assert room_state["type"] == "room_state"
    assert room_state["src"] == "fr"
    assert room_state["presenter_tgt"] == "de"

    assert snapshot["type"] == "snapshot"
    assert snapshot["src"] == "fr"
    assert snapshot["tgt"] == "de"
    assert snapshot["presenter_tgt"] == "de"


def test_attendee_can_join_existing_room_and_change_target_language(client, mock_vad):
    room_id = client.post("/api/rooms", json={"room_id": "attendee-room-1234"}).json()[
        "room_id"
    ]

    with client.websocket_connect("/ws/translate") as websocket:
        websocket.send_json(
            {
                "type": "join",
                "role": "attendee",
                "room_id": room_id,
                "target_language": "fr",
            }
        )

        joined = websocket.receive_json()
        websocket.receive_json()
        snapshot = websocket.receive_json()

        websocket.send_json(
            {
                "type": "set_target_language",
                "target_language": "es",
            }
        )
        updated_snapshot = websocket.receive_json()

    assert joined["type"] == "joined"
    assert joined["role"] == "attendee"
    assert joined["tgt"] == "fr"
    assert snapshot["type"] == "snapshot"
    assert snapshot["tgt"] == "fr"
    assert updated_snapshot["type"] == "snapshot"
    assert updated_snapshot["tgt"] == "es"


def test_attendee_join_with_unknown_room_receives_error(client, mock_vad):
    with client.websocket_connect("/ws/translate") as websocket:
        websocket.send_json(
            {
                "type": "join",
                "role": "attendee",
                "room_id": "missing-room-1234",
                "target_language": "es",
            }
        )

        error = websocket.receive_json()

    assert error == {
        "type": "error",
        "detail": "That room does not exist. Check the presenter room code and try again.",
    }
