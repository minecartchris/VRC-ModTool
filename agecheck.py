"""Recording an age-check verdict, shared by the desktop app and the web UI.

Both front ends call record() so a check filed from either side produces the
same rows: always an `age_checks` entry, plus — for an over/under call, which
is a moderation event someone may have to act on — a linked incident carrying
the world, instance and roster context.

Verdicts:
    over      the player states/appears to be above the range you screen for
    under     below it — the case that usually needs a report
    in_range  verified; the desktop also writes the note tag in VRChat
"""

import time

import db

VERDICTS = ("over", "under", "in_range")

#: Verdicts that also file an incident.
INCIDENT_VERDICTS = ("over", "under")

LABELS = {"over": "Over range", "under": "Under range",
          "in_range": "In range (verified)"}


def record(database: "db.Database", *, name: str, user_id: str = "",
           verdict: str, reported_age: int | None = None,
           world_name: str = "", world_id: str = "", instance_id: str = "",
           checked_by: str = "", checked_by_id: str = "",
           source: str = "web", note: str = "",
           players: list[dict] | None = None) -> tuple[dict, dict | None]:
    """Write the check (and its incident, if any). Returns (check, incident)."""
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    now = time.time()
    incident = None

    if verdict in INCIDENT_VERDICTS:
        age_txt = f" — reported age {reported_age}" if reported_age else ""
        subject = players or [{"name": name, "user_id": user_id,
                               "joined_at": now}]
        incident = {
            "id": db.new_id(),
            "created_at": now,
            "trigger": f"age {verdict} range"
                       + (f" ({reported_age})" if reported_age else ""),
            "transcript": [f"Manual screening: {name} marked "
                           f"{verdict.upper()} range{age_txt}."]
                          + ([note] if note else []),
            "world_name": world_name, "world_id": world_id,
            "instance_id": instance_id,
            "players": subject,
            "clip_path": "", "screenshot_path": "",
            "notes": note, "status": "new",
            "reported_by": checked_by,
            # Tells report.py not to describe this as a speech-to-text hit.
            "origin": source,
        }
        database.upsert_incident(incident)

    check = {
        "id": db.new_id(),
        "user_id": user_id, "name": name,
        "verdict": verdict, "reported_age": reported_age,
        "world_name": world_name, "world_id": world_id,
        "instance_id": instance_id,
        "incident_id": incident["id"] if incident else "",
        "checked_by": checked_by, "checked_by_id": checked_by_id,
        "note": note, "source": source, "created_at": now,
    }
    database.upsert_age_check(check)
    return check, incident


def latest_by_user(checks: list[dict]) -> dict[str, dict]:
    """Most recent check per user_id, for showing a player's current status."""
    out: dict[str, dict] = {}
    for c in sorted(checks, key=lambda c: c.get("created_at") or 0):
        if c.get("user_id"):
            out[c["user_id"]] = c
    return out
