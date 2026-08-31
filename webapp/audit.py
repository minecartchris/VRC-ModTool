"""Watch the VRChat group audit log for kicks, warns and bans.

VRChat records every instance kick and warn against the group, but not *why*.
This polls that log and queues each action so the moderator who issued it is
asked for a reason while they still remember it — instead of the log being
reconstructed from memory hours later, or never.

    group.instance.kick   ->  Kick, queued for a reason
    group.instance.warn   ->  Warn, queued for a reason
    group.user.ban        ->  Ban, logged as it stands

Bans are not queued for a reason, they are written straight into the log. A
ban is usually placed from VRChat's own group page, often by someone who never
opens this tool, and the leaderboard should still credit it. Waiting for a
prompt to be answered would mean those bans were simply never counted.

Everything read here is credited by VRChat user id — `actorId`, not the
display name beside it — so a moderator who renames stays one person on the
leaderboard, and the name is only what gets printed.

Polling borrows the live VRChat session of a signed-in moderator, so no extra
account or stored credential is needed. It requires the `group-audit-view`
permission on the watched group: without it VRChat answers 403 for everyone,
and status() reports that rather than failing silently.
"""

import re
import threading
import time
from datetime import datetime, timezone

EVENT_ACTIONS = {
    "group.instance.kick": "Kick",
    "group.instance.warn": "Warn",
}

#: "MaeMardis2 has issued an instance kick for CollzToons." — the target's
#: display name appears nowhere else in the entry, only its user id.
_TARGET = re.compile(r"\bfor\s+(.+?)\.?\s*$")

#: "Ganja banned CollzToons" / "banned user CollzToons from the group".
_BANNED = re.compile(r"\bbanned\s+(?:the\s+user\s+|user\s+)?(.+?)"
                     r"(?:\s+from\s+the\s+group)?\.?\s*$", re.I)

#: Some entries say only that *a* user was banned. Reading that as a display
#: name puts "a user" in the log where a person's name belongs.
_NOBODY = {"a user", "an user", "user", "the user", "a member", "the member",
           "a group member", "someone", "this user"}

#: Don't queue history on first run; only actions from now on.
BACKFILL_SECONDS = 3600.0

#: How far back to read bans the first time this runs. Kicks are only worth
#: prompting about while they are fresh, but a ban is a permanent record and
#: the leaderboard is meant to show what people have actually done, so the
#: existing history is worth having.
BAN_BACKFILL_DAYS = 180.0
BAN_PAGE = 100
#: Enough to walk back through the backfill window; a normal poll stops after
#: the first page or two, once it reaches entries it has already read.
BAN_MAX_PAGES = 40

#: What VRChat calls a group ban, as confirmed against this group's own log.
#: Asking for it by name is what makes the backfill reach: unfiltered, a busy
#: group's joins and leaves fill forty pages with about a week of history.
BAN_EVENT = "group.user.ban"

#: How often to read the log unfiltered anyway, as a check that the name
#: above is still right. If VRChat renames the event, the filtered scan goes
#: quiet and looks exactly like a group that has stopped banning people —
#: this is what tells the difference.
SWEEP_EVERY = 3600.0


def is_ban(event_type: str) -> bool:
    """Whether this audit entry is somebody being banned from the group.

    Matched on the shape of the name rather than one exact string: VRChat has
    renamed audit events before, and a ban that stops being counted because
    the constant went stale is worse than one matched slightly loosely.
    Unbans and the group's banner image are the two things that would
    otherwise slip through the word "ban".
    """
    kind = (event_type or "").lower()
    return "ban" in kind and "unban" not in kind and "banner" not in kind


def parse_ts(value) -> float:
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return time.time()


def target_name(entry: dict) -> str:
    match = _TARGET.search(str(entry.get("description") or ""))
    if match:
        return match.group(1).strip()
    return entry.get("targetId", "") or "(unknown)"


def ban_target_name(entry: dict, known: dict | None = None) -> str:
    """Who was banned. The id is what counts; this is for reading.

    A ban entry describes itself differently from a kick ("X banned Y", not
    "X has issued an instance kick for Y"), and some of them name nobody at
    all — so anyone the tool has already seen is looked up by id before
    falling back to printing the id itself.
    """
    text = str(entry.get("description") or "")
    match = _BANNED.search(text) or _TARGET.search(text)
    named = match.group(1).strip() if match else ""
    if named and named.lower() not in _NOBODY:
        return named
    target = entry.get("targetId", "") or ""
    return (known or {}).get(target, "") or target or "(unknown)"


class AuditWatcher:
    def __init__(self, database, cfg: dict, sessions, interval: float = 60.0):
        self.db = database
        self.cfg = cfg
        self.sessions = sessions
        self.interval = interval
        self.group_id = (cfg.get("audit_group") or "").strip()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.last_poll = 0.0
        self.last_ok = 0.0
        self.queued = 0
        self.bans = 0
        self.ban_error = ""
        self._last_sweep = 0.0
        #: Every eventType this has actually seen, counted by the hourly
        #: unfiltered sweep — the evidence for what VRChat currently calls
        #: things, and so for whether bans are being recognised at all.
        self.types_seen: dict[str, int] = {}
        #: Whose permissions are currently making this work, shown on /pending
        #: so it's obvious the feature depends on them staying signed in.
        self.provider = ""
        self._last_good_token = ""

    @property
    def configured(self) -> bool:
        return bool(self.group_id)

    def start(self) -> None:
        if not self.configured:
            self.last_error = "no audit_group configured"
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"configured": self.configured, "group_id": self.group_id,
                "last_poll": self.last_poll, "last_ok": self.last_ok,
                "error": self.last_error, "queued": self.queued,
                "provider": self.provider, "bans": self.bans,
                "ban_error": self.ban_error,
                "types_seen": dict(sorted(self.types_seen.items(),
                                          key=lambda kv: -kv[1]))}

    def poll_once(self) -> int:
        """One pass: queue kicks and warns, record bans. Returns the number of
        prompts newly queued.

        The scans share one fetch path, so a dead API shows up as the one
        error both have. `ban_error` is for what only bans can hit — an entry
        that will not parse, a write that fails — which would otherwise be
        invisible next to a kick poll reporting itself healthy.
        """
        queued = self._poll_prompts()
        try:
            self.bans += self._scan_bans()
            self.bans += self._sweep_for_renamed_bans()
        except Exception as e:                        # never kill the poll
            self.ban_error = f"{type(e).__name__}: {e}"
        return queued

    def _poll_prompts(self) -> int:
        """Fetch recent kicks/warns and queue any we haven't seen. Returns the
        number newly queued."""
        page = self._fetch_with_any_session()
        if page is None:
            return 0

        # First run: only look back a little, so turning this on doesn't
        # confront somebody with a backlog of every kick the group ever had.
        watermark = self.db.newest_audit_seen(self.group_id)
        if not watermark:
            watermark = time.time() - BACKFILL_SECONDS

        added = 0
        for entry in page.get("results", []):
            action = EVENT_ACTIONS.get(entry.get("eventType"))
            if not action:
                continue
            created = parse_ts(entry.get("created_at"))
            if created <= watermark:
                continue
            queued = {
                "id": entry.get("id"), "group_id": self.group_id,
                "action": action,
                "actor_id": entry.get("actorId", ""),
                "actor_name": entry.get("actorDisplayName", ""),
                "target_id": entry.get("targetId", ""),
                "target_name": target_name(entry),
                "location": (entry.get("data") or {}).get("location", ""),
                "created_at": created}
            if self.db.add_pending_action(queued):
                added += 1
                # The kick happened whether or not anybody ever says why, so
                # the log entry exists from now. The prompt fills in the
                # reason later; until then it reads as a kick with no reason
                # given, which is the truth. Previously a prompt that timed
                # out or was dismissed took the whole kick with it.
                self._record_unreasoned(queued)

        self.last_ok = time.time()
        self.last_error = ""
        self.queued += added
        return added

    # ---------------- bans ----------------
    @property
    def _ban_mark_key(self) -> str:
        return f"audit_ban_seen:{self.group_id}"

    def _scan_bans(self) -> int:
        """Read bans out of the audit log and write them straight to the log.

        Paged backwards until it reaches something already read, so a busy
        hour cannot push a ban off the first page and out of the record. The
        watermark is stored rather than derived from the bans found: a group
        that goes a month without banning anybody must not re-read the whole
        backfill window every minute looking for one.
        """
        if not self.configured:
            return 0
        mark = float(self.db.get_state(self._ban_mark_key, "0") or 0)
        first_run = mark <= 0
        if first_run:
            mark = time.time() - BAN_BACKFILL_DAYS * 86400

        # Only built if a ban actually turns up: most polls find none, and
        # this reads every player the tool has ever screened.
        known: dict[str, str] | None = None
        recorded, newest, offset = 0, mark, 0
        for _ in range(BAN_MAX_PAGES if first_run else 4):
            page = self._fetch_with_any_session(n=BAN_PAGE, offset=offset,
                                                event_types=BAN_EVENT)
            if page is None:
                return recorded              # no session, or a real failure
            results = page.get("results") or []
            if not results:
                break

            oldest = time.time()
            for entry in results:
                kind = str(entry.get("eventType") or "?")
                if kind not in self.types_seen:
                    # Printed once per name, into the service log: it is how
                    # you check that VRChat still calls a ban what this thinks
                    # it calls it, without adding a debug page for it.
                    print(f"[audit] event type seen: {kind}"
                          f"{' -> Ban' if is_ban(kind) else ''}", flush=True)
                self.types_seen[kind] = self.types_seen.get(kind, 0) + 1
                created = parse_ts(entry.get("created_at"))
                oldest = min(oldest, created)
                newest = max(newest, created)
                if created <= mark or not is_ban(kind):
                    continue
                if known is None:
                    known = self._names()
                if self._record_ban(entry, created, known):
                    recorded += 1

            if oldest <= mark or len(results) < BAN_PAGE:
                break
            offset += len(results)
            # Paced, because the first run walks months of history in one go
            # and a rate limit here costs the moderator whose account is being
            # borrowed, not this thread.
            self._stop.wait(1.0)

        self.ban_error = ""
        # Only move the watermark once a scan has actually completed, so a
        # page that failed halfway is read again rather than skipped over.
        self.db.set_state(self._ban_mark_key, str(newest))
        return recorded

    def _sweep_for_renamed_bans(self) -> int:
        """Once an hour, read the log unfiltered and check nothing ban-shaped
        is being missed by asking for `BAN_EVENT` by name.

        Deliberately does not move the watermark: this is a second opinion on
        one page, not a scan, and letting it mark history as read would hide
        exactly what it exists to catch.
        """
        if not self.configured:
            return 0
        now = time.time()
        if now - self._last_sweep < SWEEP_EVERY:
            return 0
        self._last_sweep = now

        page = self._fetch_with_any_session(n=BAN_PAGE, event_types="")
        if page is None:
            return 0
        mark = float(self.db.get_state(self._ban_mark_key, "0") or 0)
        recorded = 0
        for entry in page.get("results") or []:
            kind = str(entry.get("eventType") or "?")
            if kind not in self.types_seen:
                # Printed once per name, into the service log: it is how you
                # check what VRChat is calling things without a debug page.
                print(f"[audit] event type seen: {kind}"
                      f"{' -> Ban' if is_ban(kind) else ''}", flush=True)
            self.types_seen[kind] = self.types_seen.get(kind, 0) + 1
            created = parse_ts(entry.get("created_at"))
            if created <= mark or not is_ban(kind):
                continue
            if self._record_ban(entry, created, self._names()):
                recorded += 1
                if kind != BAN_EVENT:
                    self.ban_error = (
                        f"VRChat is calling bans {kind!r}, not {BAN_EVENT!r} — "
                        f"the hourly sweep is catching them, but BAN_EVENT "
                        f"needs updating")
                    print(f"[audit] {self.ban_error}", flush=True)
        return recorded

    def _names(self) -> dict:
        """Display names this tool already holds, by user id. Used only to
        print somebody's name when the audit entry gives just an id."""
        known = {uid: rec.get("name", "")
                 for uid, rec in self.db.all_users().items()}
        known.update({r["user_id"]: r["name"]
                      for r in self.db.known_users() if r.get("name")})
        return known

    def _record_unreasoned(self, queued: dict) -> None:
        """File the kick or warn straight away, with no reason on it yet.

        Deliberately silent: no Discord post, no age check. Those belong to
        the moment a moderator says why, and announcing twice would be worse
        than announcing late.
        """
        log_id = f"aud-{queued['id']}"[:64]
        if not queued.get("id") or self.db.get_incident(log_id):
            return
        world_id, _, instance_id = (queued.get("location") or "").partition(":")
        try:
            self.db.upsert_incident({
                "id": log_id, "created_at": queued["created_at"],
                "trigger": queued["action"],          # no reason yet
                "transcript": [], "world_name": "",
                "world_id": world_id, "instance_id": instance_id,
                "players": [{"name": queued.get("target_name", ""),
                             "user_id": queued.get("target_id", "")}],
                "clip_path": "", "screenshot_path": "", "notes": "",
                "status": "reported",
                "reported_by": queued.get("actor_name", ""),
                "reported_by_id": queued.get("actor_id", ""),
                "filed_by": "", "filed_by_id": "",
                "origin": "vrchat-audit",
            })
        except Exception as e:
            print(f"[audit] could not file {log_id}: {e}", flush=True)

    def _record_ban(self, entry: dict, created: float, known: dict) -> bool:
        """Write one audit ban into the log. False if it was already there.

        Keyed on VRChat's own audit id, so the same ban read twice — a retry,
        a restart mid-scan — lands on the same record instead of inflating
        somebody's count.
        """
        log_id = f"aud-{entry.get('id') or ''}"[:64]
        if not entry.get("id"):
            return False
        if self.db.get_incident(log_id):
            return False
        self.db.upsert_incident({
            "id": log_id, "created_at": created,
            # No reason: VRChat does not ask for one when a group ban is
            # placed, and inventing wording here would put words in the
            # moderator's mouth. The log shows it blank, honestly.
            "trigger": "Ban",
            "transcript": [str(entry.get("description") or "").strip()],
            "world_name": "", "world_id": "", "instance_id": "",
            "players": [{"name": ban_target_name(entry, known),
                         "user_id": entry.get("targetId", "") or ""}],
            "clip_path": "", "screenshot_path": "", "notes": "",
            "status": "reported",
            "reported_by": entry.get("actorDisplayName", "") or "",
            "reported_by_id": entry.get("actorId", "") or "",
            "origin": "vrchat-audit",
        })
        return True

    def _fetch_with_any_session(self, *, n: int = 60, offset: int = 0,
                                event_types: str | None = None) -> dict | None:
        """Try every signed-in moderator's own VRChat session in turn.

        Audit access is a per-account permission, so the tool should work as
        long as *somebody* who holds it is logged in — typically senior staff.
        Everyone else's 403 is skipped rather than treated as failure. The
        account that worked is remembered and tried first next time, so the
        steady state is one API call per poll.
        """
        with self.sessions._lock:                     # noqa: SLF001
            clients = dict(self.sessions._clients)
        if not clients:
            self.provider = ""
            self.last_error = ("nobody is signed in — the audit log is read "
                               "with a moderator's own VRChat permissions, so "
                               "someone holding group-audit-view must be "
                               "logged in")
            return None

        ordered = sorted(clients.items(),
                         key=lambda kv: kv[0] != self._last_good_token)
        denied = 0
        for token, api in ordered:
            try:
                page = api.get_group_audit_logs(
                    self.group_id, n=n, offset=offset,
                    event_types=(",".join(EVENT_ACTIONS)
                                 if event_types is None else event_types))
            except Exception as e:
                if "403" in str(e):
                    denied += 1
                    continue                 # this account just lacks the perm
                self.last_error = f"audit poll failed: {e}"
                return None
            self._last_good_token = token
            self.provider = getattr(api, "user", {}).get("displayName", "") \
                if isinstance(getattr(api, "user", None), dict) else ""
            return page

        self.provider = ""
        self.last_error = (
            f"none of the {denied} signed-in moderator(s) hold the "
            f"group-audit-view permission on this group, so VRChat refuses "
            f"the audit log. Someone senior enough needs to sign in.")
        return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.last_poll = time.time()
                self.poll_once()
            except Exception as e:                    # never kill the thread
                self.last_error = f"{type(e).__name__}: {e}"
            self._stop.wait(self.interval)
