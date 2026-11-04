"""Sample tactical alerts for the demo, matched to the feeds on screen.

Each sample video has a profile of alerts describing what it shows (checked against the footage on
2026-11-03), so the ticker and the imagery tell one story and the model can tie them together. The
alert's location starts with the feed name the dashboard shows. Uploaded videos are matched by
keywords in their file name; anything else, or no feed at all, gets general alerts.
"""

import random
import time
from dataclasses import dataclass
from typing import Any

from common.feeds import feed_label


@dataclass(frozen=True)
class Template:
    """One alert: severity, where in the scene, and the message (format fields are filled in)."""

    severity: str
    place: str
    message: str


def _fill(rng: random.Random) -> dict[str, str]:
    return {
        "grid": f"{rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
        "bearing": f"{rng.randrange(0, 360, 5):03d}",
        "mins": str(rng.randint(4, 25)),
        "dist": str(rng.choice([200, 300, 500])),
    }


PROFILES: dict[str, list[Template]] = {
    "burning_armour": [
        Template(
            "CRITICAL",
            "Main road",
            "Vehicle burning at the roadside, heavy black smoke. "
            "Secondary detonations possible: hold traffic {dist} m back.",
        ),
        Template(
            "WARNING",
            "Main road",
            "Armoured vehicles driving past the burning wreck "
            "towards the junction, no halt observed.",
        ),
        Template(
            "WARNING",
            "Junction",
            "Smoke drifting across the route, observation of the junction degraded.",
        ),
        Template(
            "INFO",
            "Junction",
            "Civilian traffic still using the road near the incident at grid {grid}.",
        ),
    ],
    "convoy": [
        Template(
            "WARNING",
            "Supply route",
            "Column of tracked self-propelled guns halted nose "
            "to tail on a dirt track; crews dismounted.",
        ),
        Template(
            "CRITICAL",
            "Supply route",
            "Column bunched on a single track: high exposure to indirect fire and drones.",
        ),
        Template(
            "INFO",
            "Supply route",
            "Dismounted infantry moving alongside the vehicles, small arms only observed.",
        ),
        Template(
            "WARNING",
            "Supply route",
            "Column stationary for {mins} min; possible refuel or maintenance halt.",
        ),
    ],
    "rotary_wing": [
        Template(
            "INFO",
            "Air corridor",
            "Heavy-lift tandem-rotor helicopters in formation, heading {bearing}, low level.",
        ),
        Template(
            "WARNING",
            "Air corridor",
            "Further rotary-wing contacts trailing the lead formation; number unconfirmed.",
        ),
        Template(
            "WARNING",
            "Landing zone",
            "Lift pattern consistent with an air assault or "
            "resupply; expect landing zone activity within the hour.",
        ),
        Template(
            "INFO",
            "Air corridor",
            "Rotary-wing traffic active: deconflict fires and fast air in the corridor.",
        ),
    ],
    "naval_fires": [
        Template(
            "CRITICAL",
            "Harbour",
            "Rocket salvo fired from warship pennant 158; multiple projectiles in flight.",
        ),
        Template(
            "WARNING",
            "Harbour",
            "Second vessel alongside the firing ship; harbour movements restricted.",
        ),
        Template(
            "WARNING",
            "Harbour front",
            "Smoke over the harbour reducing visibility for coastal observation posts.",
        ),
        Template(
            "INFO",
            "Harbour",
            "Live-fire activity continuing; navigation warning in force for the approaches.",
        ),
    ],
    "fast_air": [
        Template(
            "WARNING",
            "Coastal airspace",
            "Fast jets dispensing flares; defensive manoeuvring observed.",
        ),
        Template(
            "CRITICAL",
            "Coastal airspace",
            "Flare release may indicate a surface-to-air "
            "threat in the area; air defence warning raised.",
        ),
        Template(
            "INFO",
            "Coastal airspace",
            "Fast-jet pair transiting heading {bearing}, low to medium level.",
        ),
    ],
    "night_isr": [
        Template(
            "WARNING",
            "Urban sector",
            "Night ISR tracking a single moving contact between buildings at grid {grid}.",
        ),
        Template(
            "CRITICAL",
            "Urban sector",
            "Contact moving towards a cordoned compound; ground team asked to hold.",
        ),
        Template(
            "INFO",
            "Urban sector",
            "Night-vision surveillance of the sector continuing; streets otherwise quiet.",
        ),
    ],
    "targeting_pod": [
        Template(
            "WARNING",
            "Open ground",
            "Airborne sensor tracking a single vehicle on open ground; identification pending.",
        ),
        Template(
            "WARNING",
            "Open ground",
            "Tracked vehicle stopped near a rocky outcrop; possible attempt to hide.",
        ),
        Template(
            "INFO",
            "Open ground",
            "Sensor lock held for {mins} min; no other vehicles in the field of view.",
        ),
    ],
    "infantry": [
        Template(
            "WARNING",
            "Quarry track",
            "Fireteam advancing on foot, weapons raised, vehicle parked behind them.",
        ),
        Template(
            "CRITICAL",
            "Quarry track",
            "Small quadcopter drone above the fireteam; friend or foe unconfirmed.",
        ),
        Template(
            "INFO",
            "Quarry track",
            "Patrol moving along the track towards the ridge line, bearing {bearing}.",
        ),
    ],
    "armour": [
        Template(
            "WARNING",
            "Open grassland",
            "Main battle tank with turret marking T3 moving across open grassland.",
        ),
        Template(
            "INFO",
            "Open grassland",
            "Single tank in view; no supporting infantry or vehicles observed.",
        ),
        Template("WARNING", "Treeline", "Tank heading towards the treeline at grid {grid}."),
    ],
    "thermal": [
        Template(
            "WARNING",
            "Woodland",
            "Thermal sensor tracking a single heat signature moving through woodland.",
        ),
        Template(
            "CRITICAL",
            "Perimeter",
            "Heat signature closing on the perimeter, consistent with a person on foot.",
        ),
        Template("INFO", "Woodland", "Thermal sweep of the woodland sector: one contact only."),
    ],
}

GENERAL: list[Template] = [
    Template("WARNING", "North Perimeter", "Unidentified drone detected over the perimeter fence."),
    Template("CRITICAL", "Comms Relay", "Uplink lost to the forward relay; switching to backup."),
    Template("INFO", "Fuel Depot", "Scheduled resupply convoy departed, ETA {mins} min."),
    Template(
        "WARNING", "East Ridge", "Suspicious vehicle stopped on the approach road at grid {grid}."
    ),
    Template("INFO", "Command Centre", "Sensor network check complete; all stations reporting."),
    Template(
        "WARNING", "Landing Zone Echo", "Optical sensor anomaly reported; technician dispatched."
    ),
]

# The sample videos by name (naval_squadron_transit shows fast jets, not ships), then keywords
# for uploads, checked in order.
FEED_PROFILES: dict[str, str] = {
    "burning_tank.mp4": "burning_armour",
    "convoy.mp4": "convoy",
    "helicopters_formation.mp4": "rotary_wing",
    "naval_exercises.mp4": "naval_fires",
    "naval_squadron_transit.mp4": "fast_air",
    "night_vision_footage.mp4": "night_isr",
    "operation.mp4": "targeting_pod",
    "tactical_unit.mp4": "infantry",
    "tank_footage.mp4": "armour",
    "thermal_imaging.mp4": "thermal",
}
KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("burn", "fire", "wreck"), "burning_armour"),
    (("convoy", "column", "artillery"), "convoy"),
    (("heli", "rotor", "chinook"), "rotary_wing"),
    (("naval", "ship", "maritime", "harbour", "harbor"), "naval_fires"),
    (("jet", "fighter", "flare", "aircraft"), "fast_air"),
    (("night", "nvg"), "night_isr"),
    (("thermal", "infrared", "flir"), "thermal"),
    (("pod", "targeting", "uav", "drone"), "targeting_pod"),
    (("tank", "armour", "armor", "mbt"), "armour"),
    (("infantry", "patrol", "unit", "troop", "soldier"), "infantry"),
]


def profile_for(video: str) -> str | None:
    """Picks the alert profile for a video: by name for the samples, else by keyword."""
    if video in FEED_PROFILES:
        return FEED_PROFILES[video]
    name = video.lower()
    for words, profile in KEYWORDS:
        if any(w in name for w in words):
            return profile
    return None


def sample_alerts(feeds: list[str], rng: random.Random | None = None) -> list[dict[str, Any]]:
    """Makes 3-5 alerts about the given feeds, or general ones if none of them is recognised.

    Args:
        feeds: Video file names on screen, in grid order; duplicates are ignored.
        rng: Random source, for tests.

    Returns:
        Alerts with timestamp, severity, location and message. Feed alerts take turns between
        the recognised feeds, and their location is "<feed name> · <place>".
    """
    rng = rng or random.Random()
    now = time.time()
    known = [(v, p) for v in dict.fromkeys(feeds) if (p := profile_for(v))]
    count = rng.randint(3, 5)
    if not known:
        picks = rng.sample(GENERAL, count)
        return [
            {
                "timestamp": now,
                "severity": t.severity,
                "location": t.place,
                "message": t.message.format(**_fill(rng)),
            }
            for t in picks
        ]

    decks = {v: rng.sample(PROFILES[p], len(PROFILES[p])) for v, p in known}
    alerts: list[dict[str, Any]] = []
    for i in range(max(count, len(known))):
        video, _ = known[i % len(known)]
        if not decks[video]:
            continue
        t = decks[video].pop()
        alerts.append(
            {
                "timestamp": now + i * 0.001,
                "severity": t.severity,
                "location": f"{feed_label(video)} · {t.place}",
                "message": t.message.format(**_fill(rng)),
            }
        )
    return alerts
