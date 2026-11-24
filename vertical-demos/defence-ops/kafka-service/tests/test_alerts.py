"""Tests for the sample alerts matched to the feeds on screen."""

import random

import pytest
from pydantic import ValidationError

import alerts
import main


def test_every_sample_video_has_its_own_profile() -> None:
    assert set(alerts.FEED_PROFILES.values()) == set(alerts.PROFILES)


@pytest.mark.parametrize(
    ("video", "profile"),
    [
        ("naval_squadron_transit.mp4", "fast_air"),  # the footage shows jets, not ships
        ("my_patrol_clip.mov", "infantry"),
        ("Harbour-Drone.mp4", "naval_fires"),
        ("holiday.mp4", None),
    ],
)
def test_profiles_by_name_then_keyword(video: str, profile: str | None) -> None:
    assert alerts.profile_for(video) == profile


def test_alerts_take_turns_between_the_feeds() -> None:
    made = alerts.sample_alerts(
        ["convoy.mp4", "naval_exercises.mp4", "convoy.mp4"], random.Random(7)
    )
    places = [a["location"].split(" · ")[0] for a in made]
    assert 3 <= len(made) <= 5
    assert places[:2] == ["Convoy", "Naval Exercises"]
    assert set(places) == {"Convoy", "Naval Exercises"}
    assert all("{" not in a["message"] for a in made)


def test_every_feed_gets_an_alert_even_with_four() -> None:
    feeds = ["burning_tank.mp4", "operation.mp4", "thermal_imaging.mp4", "tank_footage.mp4"]
    made = alerts.sample_alerts(feeds, random.Random(1))
    assert {a["location"].split(" · ")[0] for a in made} == {
        "Burning Tank",
        "Operation",
        "Thermal Imaging",
        "Tank Footage",
    }


def test_unknown_or_no_feeds_get_general_alerts() -> None:
    for feeds in ([], ["holiday.mp4"]):
        made = alerts.sample_alerts(feeds, random.Random(3))
        assert 3 <= len(made) <= 5
        assert all(" · " not in a["location"] for a in made)


def test_generate_rejects_paths_in_feed_names() -> None:
    with pytest.raises(ValidationError):
        main.GenerateRequest.model_validate({"feeds": ["../etc/passwd"]})
    assert main.GenerateRequest.model_validate({}).feeds == []
