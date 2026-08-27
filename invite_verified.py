"""Queue a group invite for everyone a moderator has ever verified.

Auto-invite only fires on new verdicts, so the people cleared before it was
switched on were never asked. This walks the age checks and queues the ones
that were missed.

    python invite_verified.py                 # count them, queue nothing
    python invite_verified.py --apply         # queue them

Nothing is sent from here. Rows go on the same queue the server drains with a
signed-in moderator's permissions, paced so VRChat does not rate limit us, and
anyone who turns out to be a member already — or to have an invite waiting —
is closed on the spot rather than asked twice.
"""

import argparse
import os
import sys

import db as dbmod


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually queue; without it, nothing is written")
    ap.add_argument("--group", default="",
                    help="group id; defaults to the server's action group")
    ap.add_argument("--limit", type=int, default=0,
                    help="queue at most this many, for a cautious first pass")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from webapp import config as webconfig

    cfg = webconfig.load()
    group = (args.group or cfg.get("action_group")
             or cfg.get("roster_group") or "").strip()
    if not group:
        print("No group configured (action_group / roster_group).")
        return 1

    d = dbmod.Database()

    # Latest verdict per person: somebody marked under range last week and in
    # range today should be invited, and somebody in the other order should
    # not.
    latest: dict[str, dict] = {}
    for c in sorted(d.all_age_checks(), key=lambda c: c["created_at"] or 0):
        if c["deleted"] or not c["user_id"]:
            continue
        latest[c["user_id"]] = c

    verified = {uid: c for uid, c in latest.items()
                if c["verdict"] == "in_range"}

    # Anyone already on the queue — waiting or long since sent — is left alone.
    # This is what makes the script safe to run twice.
    seen = {r["user_id"] for r in d.group_actions(limit=100000)
            if r["kind"] == "invite" and r["group_id"] == group}

    todo = [(uid, c) for uid, c in verified.items() if uid not in seen]
    todo.sort(key=lambda pair: pair[1]["created_at"] or 0, reverse=True)
    if args.limit:
        todo = todo[:args.limit]

    print(f"group            : {group}")
    print(f"people verified  : {len(verified)}")
    print(f"already queued   : {len(verified) - len([1 for u in verified if u not in seen])}")
    print(f"to queue now     : {len(todo)}")
    if todo:
        print()
        print("newest few:")
        for uid, c in todo[:5]:
            print(f"   {(c['name'] or uid)[:28]:<28} verified by {c['checked_by'] or '?'}")

    if not args.apply:
        print()
        print("Dry run — nothing written. Re-run with --apply to queue these.")
        return 0

    queued = 0
    for uid, c in todo:
        row = d.queue_group_action(
            "invite", group_id=group, user_id=uid,
            user_name=c["name"] or "", reason="verified in range (backfill)",
            asked_by="backfill", asked_by_id="")
        queued += 1 if row else 0
    print()
    print(f"queued {queued}. The server sends them a few at a time whenever a "
          f"moderator holding group-invites-manage is signed in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
