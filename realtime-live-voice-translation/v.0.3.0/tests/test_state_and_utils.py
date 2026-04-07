from app.state.rooms import is_valid_room_id, normalize_room_id
from app.utils.text import format_seconds_label, normalize_text


def test_room_id_validation_rules():
    assert is_valid_room_id("valid-room-1234")
    assert not is_valid_room_id("bad room")
    assert not is_valid_room_id("short")


def test_normalize_room_id_keeps_valid_ids_and_generates_invalid_ones():
    assert normalize_room_id("valid-room-1234") == "valid-room-1234"
    generated = normalize_room_id("bad room")
    assert generated != "bad room"
    assert len(generated) == 32


def test_text_helpers_cover_basic_formatting():
    assert normalize_text("  Hello   WORLD  ") == "hello world"
    assert format_seconds_label(65.432) == "01:05.432"
