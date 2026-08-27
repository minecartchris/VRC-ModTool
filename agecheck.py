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

import re
import time

import db

#: Reason text that is really an age verdict. Matched as a substring against
#: the whole reason, which is a comma-joined multi-select plus free text
#: ("Age Baiting, Refused Age Check - sounded 12 and said he was 19"). The two
#: keys never overlap, so order is only for readability.
AGE_REASONS = (("underage", "under"), ("overage", "over"))

#: Moderators write the age straight into the reason ("Overage - 20").
#: Deliberately narrow: only a number attached to the verdict word counts, so
#: prose like "sounded 12 and said he was 19" is left alone, not guessed at.
_AGE_IN_REASON = re.compile(r"(?:over|under)age[d]?\s*[-:]?\s*(\d{1,3})\b", re.I)


def verdict_for_reason(reason: str) -> str | None:
    low = (reason or "").lower()
    return next((v for key, v in AGE_REASONS if key in low), None)


def age_in_reason(reason: str) -> int | None:
    match = _AGE_IN_REASON.search(reason or "")
    if not match:
        return None
    age = int(match.group(1))
    return age if 1 <= age <= 120 else None

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
           players: list[dict] | None = None,
           incident_id: str = "",
           file_incident: bool = True) -> tuple[dict, dict | None]:
    """Write the check (and its incident, if any). Returns (check, incident).

    Pass `incident_id` when the caller has already filed the incident — an
    audit-log kick is one event, and letting this create a second one would
    double-count it in every list and report.

    `file_incident=False` records the verdict on its own. Marking somebody
    over or under range is a note about a person, not a moderation action:
    the web panel uses it constantly while screening a room, and every one of
    those becoming an incident buried the incidents that were really kicks.
    The desktop app leaves it on, because it pairs the verdict with a VRChat
    screenshot and needs somewhere to attach it.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    now = time.time()
    incident = None

    if verdict in INCIDENT_VERDICTS and not incident_id and file_incident:
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
        "incident_id": incident["id"] if incident else incident_id,
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
