"""Import the Teen Chillout Firestore moderation log into the mod suite.

The web tool (team-chillo-mod-tool) keeps its history in Firestore:

    kick_logs      one document per moderation action — moderator, reason(s),
                   target users with VRChat profile links, timestamp
    allowed_users  the moderator allowlist, keyed by usr_ id, with a role of
                   Mod or HR

This pulls both into modtool.db so the two tools show one history instead of
two. Records are pushed through the sync API, so it works against the hosted
server without putting Firebase credentials on it.

    python import_teenchillout.py --service-account sa.json \
        --server https://vrcmod.example.cc --token SYNC_TOKEN

Safe to re-run: every imported row gets an id derived from its Firestore
document id, and db.upsert_* only counts a write when content actually
changes, so a second run reports zero changes rather than duplicating.

Needs `google-cloud-firestore` (not in requirements — this is a one-off tool).
"""

import argparse
import hashlib
import re
import sys
import time
from datetime import datetime, timezone

import requests

# VRChat display names are full of non-ASCII; a cp1252 console would otherwise
# crash the import halfway through printing its summary.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import agecheck

_USR = re.compile(r"(usr_[0-9a-f-]{36})")


def stable_id(prefix: str, *parts: str) -> str:
    """12-char id derived from the Firestore document, so re-imports collide
    with the previous run instead of inserting a duplicate."""
    digest = hashlib.sha1("|".join(parts).encode()).hexdigest()
    return (prefix + digest)[:12]


def parse_ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return time.time()


def players_of(doc: dict) -> list[dict]:
    """Targets, handling both the current shape and the legacy single-user one."""
    out, seen = [], set()
    entries = list(doc.get("kickedUsersData") or [])
    if not entries and doc.get("kickedUser"):
        entries = [{"name": doc["kickedUser"], "link": doc.get("userLink", "")}]
    for e in entries:
        if not isinstance(e, dict):
            continue
        link = str(e.get("link") or "")
        match = _USR.search(link)
        uid = match.group(1) if match else ""
        name = (e.get("name") or "").strip() or uid or "(unknown)"
        if (uid, name) in seen:
            continue
        seen.add((uid, name))
        out.append({"name": name, "user_id": uid})
    # Some documents carry userLinks without names; keep those ids too.
    for link in doc.get("userLinks") or []:
        match = _USR.search(str(link))
        if match and not any(p["user_id"] == match.group(1) for p in out):
            out.append({"name": match.group(1), "user_id": match.group(1)})
    return out


def convert(doc_id: str, doc: dict) -> tuple[dict, list[dict]]:
    """One Firestore log -> one incident, plus an age check per target when the
    reason was an age verdict."""
    reason = str(doc.get("reason") or "").strip()
    # The form writes `action`; the API route reads `actionType`. Older
    # documents have neither, and those were all kicks.
    action = str(doc.get("action") or doc.get("actionType") or "Kick").strip()
    moderator = str(doc.get("moderator") or "").strip()
    created = parse_ts(doc.get("timestamp"))
    people = players_of(doc)

    trigger = f"{action} — {reason}" if reason else action
    incident = {
        "id": stable_id("tc", doc_id),
        "created_at": created,
        "trigger": trigger[:160],
        "transcript": [f"Reason: {reason}"] if reason else [],
        "world_name": "", "world_id": "", "instance_id": "",
        "players": people,
        "clip_path": "", "screenshot_path": "",
        "notes": "", "status": "reported",   # historical: already actioned
        "reported_by": moderator,
        "origin": "teenchillout",
    }

    checks = []
    verdict = agecheck.verdict_for_reason(reason)
    if verdict:
        age = agecheck.age_in_reason(reason)
        for person in people:
            checks.append({
                "id": stable_id("ta", doc_id, person["user_id"] or person["name"]),
                "user_id": person["user_id"], "name": person["name"],
                "verdict": verdict, "reported_age": age,
                "world_name": "", "world_id": "", "instance_id": "",
                "incident_id": incident["id"],
                "checked_by": moderator, "checked_by_id": "",
                "note": reason, "source": "teenchillout",
                "created_at": created,
            })
    return incident, checks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--service-account", required=True,
                    help="path to the Firebase service account JSON")
    ap.add_argument("--project", default="teen-chillout-logs")
    ap.add_argument("--server", required=True, help="mod suite base URL")
    ap.add_argument("--token", required=True, help="the FULL sync_token")
    ap.add_argument("--dry-run", action="store_true",
                    help="convert and summarise without sending anything")
    args = ap.parse_args()

    import os
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = args.service_account
    from google.cloud import firestore

    client = firestore.Client(project=args.project)

    print("Reading Firestore…")
    incidents, checks = [], []
    for doc in client.collection("kick_logs").stream():
        incident, doc_checks = convert(doc.id, doc.to_dict() or {})
        incidents.append(incident)
        checks.extend(doc_checks)

    staff = []
    for doc in client.collection("allowed_users").stream():
        data = doc.to_dict() or {}
        staff.append({"user_id": doc.id,
                      "name": data.get("displayName", ""),
                      "role": data.get("role", "Mod"),
                      "added_at": parse_ts(data.get("addedAt"))})

    verdicts = {}
    for c in checks:
        verdicts[c["verdict"]] = verdicts.get(c["verdict"], 0) + 1
    print(f"  {len(incidents)} logs -> incidents")
    print(f"  {len(checks)} age checks ({verdicts or 'none'})")
    print(f"  {len(staff)} moderators "
          f"({sum(1 for s in staff if s['role'] == 'HR')} HR)")

    if args.dry_run:
        print("\n(dry run — nothing sent)")
        for i in sorted(incidents, key=lambda x: x["created_at"])[:3]:
            who = ", ".join(p["name"] for p in i["players"]) or "—"
            print(f"   {time.strftime('%Y-%m-%d', time.localtime(i['created_at']))}"
                  f"  {i['trigger'][:60]:62} {who[:30]}")
        return 0

    base = args.server.rstrip("/")
    headers = {"X-Sync-Token": args.token}
    applied = {"incidents": 0, "age_checks": 0}
    # Chunked so one oversized request can't time out the whole import.
    for start in range(0, max(len(incidents), len(checks)), 50):
        payload = {"incidents": incidents[start:start + 50],
                   "age_checks": checks[start:start + 50]}
        r = requests.post(f"{base}/api/sync/push", json=payload,
                          headers=headers, timeout=120)
        if not r.ok:
            print(f"push failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
            return 1
        got = r.json().get("applied", {})
        applied["incidents"] += got.get("incidents", 0)
        applied["age_checks"] += got.get("age_checks", 0)

    r = requests.post(f"{base}/api/sync/staff", json={"staff": staff},
                      headers=headers, timeout=60)
    staff_applied = r.json().get("applied") if r.ok else f"failed {r.status_code}"

    print(f"\nApplied: {applied['incidents']} incidents, "
          f"{applied['age_checks']} age checks, {staff_applied} moderators")
    print("(zero on a re-run means everything was already imported)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
