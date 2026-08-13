"""FastAPI app: web UI for incidents and age checks, plus the desktop sync API.

Run it with `python run_web.py` (or run_web.bat). It reads and writes the same
modtool.db as the Tkinter app, so a check filed in VR from the desktop and one
filed from a phone in the browser land in the same table.

Two ways in:
  * moderators — VRChat sign-in, authorised by staff-group membership (auth.py)
  * desktop clients — a shared token on /api/sync/* (no VRChat login needed)
"""

import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, Form, Header, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import agecheck
import db
import report
from paths import SHOTS_DIR
from webapp import config as webconfig
from webapp import discord
from webapp.audit import AuditWatcher
from webapp.auth import AuthError, SessionManager, staff_groups, token_hash
from webapp.groupwork import GroupWorker
from webapp.roster import LOCAL_CLIENT, LocalRosterPublisher

SESSION_COOKIE = "modsession"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
#: How long an agent's pairing code stays good. Long enough to alt-tab, find
#: the browser and sign in; short enough that an abandoned code on somebody's
#: screen is not a standing invitation.
PAIR_TTL = 600.0
#: Deliberately missing I, O, 0 and 1 — this gets read off one screen and typed
#: into another, sometimes over voice chat.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class LoginRequired(Exception):
    """Raised by page handlers when there is no valid session."""


def create_app(cfg: dict | None = None, database: "db.Database | None" = None):
    cfg = cfg or webconfig.load()
    database = database or db.Database()
    sessions = SessionManager(database, cfg)
    started_at = time.time()

    app = FastAPI(title="VRChat Mod Suite", docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.db = database
    app.state.sessions = sessions

    # Read this machine's VRChat log directly, so the roster stays current
    # whether or not the desktop app happens to be running.
    publisher = None
    if cfg.get("read_local_log", True) and LocalRosterPublisher.available():
        publisher = LocalRosterPublisher(
            database, allow=lambda snap: _roster_allowed(cfg, snap))
        publisher.start()
    app.state.roster = publisher

    # Ask moderators why, while they still remember.
    audit = AuditWatcher(database, cfg, sessions,
                         interval=float(cfg.get("audit_poll_seconds", 60) or 60))
    audit.start()
    app.state.audit = audit

    # Bans and invites wait here for a moderator whose VRChat permissions can
    # carry them out — see webapp/groupwork.py.
    groupwork = GroupWorker(database, cfg, sessions)
    groupwork.start()
    app.state.groupwork = groupwork

    app.mount("/static", StaticFiles(directory=str(HERE / "static")),
              name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(
        fmt_time=_fmt_time, fmt_ago=_fmt_ago, verdict_label=agecheck.LABELS,
        short_instance=_short_instance)
    # Exposed as a global rather than {% import %}: a macro imported in
    # base.html is not visible to the templates that extend it, and every page
    # needs icons.
    templates.env.globals["icon"] = (
        templates.env.get_template("_icons.html").module.icon)
    templates.env.globals["reason_form"] = (
        templates.env.get_template("_reason_form.html").module.reason_form)

    # ---------------- plumbing ----------------
    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        # A fetch() caller wants JSON, not a login page rendered into its
        # response — redirecting there makes the failure look like malformed
        # data instead of "you are signed out".
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "not signed in"}, status_code=401)
        return RedirectResponse(
            f"/login?next={quote(request.url.path)}", status_code=303)

    def session_of(request: Request) -> dict | None:
        return sessions.get(request.cookies.get(SESSION_COOKIE))

    def require(request: Request) -> dict:
        sess = session_of(request)
        if not sess:
            raise LoginRequired()
        return sess

    def shown_rosters() -> list[dict]:
        """Rosters this server is willing to display.

        Ingest already refuses instances the group does not own, but rows
        stored before the setting existed — or pushed by an older desktop
        client — are filtered here too, so turning it on takes effect at once
        rather than after every stale row has aged out.
        """
        return [r for r in database.all_rosters() if _roster_allowed(cfg, r)]

    def hides_others(user_id: str) -> bool:
        """Whether this moderator has turned off other people's prompts."""
        return database.pref(user_id, "hide_others") == "1"

    def pending_count(sess: dict) -> int:
        """What the nav badge shows — the same set the page will show."""
        rows = database.pending_actions()
        if hides_others(sess["user_id"]):
            rows = [a for a in rows if a["actor_id"] == sess["user_id"]]
        return len(rows)

    def action_group() -> str:
        """The group invites and bans apply to."""
        return ((cfg.get("action_group") or "").strip()
                or (cfg.get("roster_group") or "").strip())

    def queue_ban(target: dict, *, reason: str, sess: dict,
                  incident_id: str = "") -> None:
        """Ask for a group ban. Carried out when someone able to is signed in."""
        group = action_group()
        if not group or not target.get("user_id"):
            return
        database.queue_group_action(
            "ban", group_id=group, user_id=target["user_id"],
            user_name=target.get("name", ""), reason=reason,
            incident_id=incident_id, asked_by=sess.get("name", ""),
            asked_by_id=sess.get("user_id", ""))

    def queue_invite_if_verified(verdict: str, *, user_id: str, name: str,
                                 sess: dict) -> None:
        """An in-range verdict is also a decision that they belong here.

        Membership is checked when the invite is actually sent, not now — by
        then they may have joined on their own.
        """
        group = action_group()
        if (verdict != "in_range" or not user_id or not group
                or not cfg.get("auto_invite_verified")):
            return
        database.queue_group_action(
            "invite", group_id=group, user_id=user_id, user_name=name,
            reason="verified in range", asked_by=sess.get("name", ""),
            asked_by_id=sess.get("user_id", ""))

    def is_admin(user_id: str) -> bool:
        """Admin on top of being a moderator, not instead of it.

        Root admins come from the config so the table can never lock everyone
        out — remove yourself from the list and you are still in.
        """
        if not user_id:
            return False
        roots = {str(r).strip() for r in (cfg.get("root_admins") or [])}
        return user_id in roots or database.is_admin(user_id)

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        sess = ctx.pop("session", None)
        # Pages showing one instance narrow the fingerprint to that reporter,
        # so another moderator's instance filling up doesn't reload them.
        roster_scope = ctx.get("roster_scope", "")
        return templates.TemplateResponse(request, name, {
            "session": sess, "cfg": cfg,
            # What the page was rendered from; the browser polls /api/state
            # and reloads when this moves. See static/refresh.js.
            "state_version": (database.state_version(roster_scope)
                              if sess else ""),
            # Whether this server can see VRChat itself. False on a hosted
            # box, where the roster can only arrive from a client that is
            # actually in the instance.
            "local_reader": publisher is not None,
            "live_api": bool(sessions.client(
                request.cookies.get(SESSION_COOKIE))),
            # Lets a form send you back exactly where you were, filters and
            # search included, instead of dumping you on a default page.
            "current_url": request.url.path + (
                f"?{request.url.query}" if request.url.query else ""),
            "my_role": (database.all_staff().get(sess["user_id"], {}).get("role")
                        if sess else None),
            # Drives the Admin nav item and every admin-only control.
            "am_admin": is_admin(sess["user_id"]) if sess else False,
            # Drives the "you kicked someone — why?" prompt on every page.
            "my_pending": (database.pending_actions(sess["user_id"])
                           if sess else []),
            "pending_total": pending_count(sess) if sess else 0,
            **ctx})

    def set_session_cookie(resp: Response, token: str) -> None:
        resp.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="lax",
            secure=bool(cfg.get("https_only")),
            max_age=int(float(cfg.get("session_hours", 12)) * 3600))

    # ---------------- auth ----------------
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        if session_of(request):
            return RedirectResponse(next or "/", status_code=303)
        return page(request, "login.html", next=next, error=None, ticket=None)

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, username: str = Form(...),
              password: str = Form(...), next: str = Form("/")):
        try:
            sessions.check_rate(_client_ip(request))
            result = sessions.begin_login(username, password)
        except AuthError as e:
            return page(request, "login.html", next=next, error=str(e),
                        ticket=None)
        if result["status"] == "2fa":
            return page(request, "login.html", next=next, error=None,
                        ticket=result["ticket"], method=result["method"])
        resp = RedirectResponse(next or "/", status_code=303)
        set_session_cookie(resp, result["session"]["token"])
        return resp

    @app.post("/login/2fa", response_class=HTMLResponse)
    def login_2fa(request: Request, ticket: str = Form(...),
                  code: str = Form(...), next: str = Form("/")):
        try:
            sessions.check_rate(_client_ip(request))
            sess = sessions.complete_2fa(ticket, code)
        except AuthError as e:
            return page(request, "login.html", next=next, error=str(e),
                        ticket=ticket, method="totp")
        resp = RedirectResponse(next or "/", status_code=303)
        set_session_cookie(resp, sess["token"])
        return resp

    @app.post("/logout")
    def logout(request: Request):
        sessions.logout(request.cookies.get(SESSION_COOKIE))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ---------------- dashboard ----------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        sess = require(request)
        incidents = database.all_incidents()
        checks = database.all_age_checks()
        rosters = shown_rosters()
        day = time.time() - 86400
        stats = {
            "incidents": len(incidents),
            "incidents_new": sum(1 for i in incidents if i["status"] == "new"),
            "incidents_today": sum(1 for i in incidents
                                   if (i["created_at"] or 0) > day),
            "checks": len(checks),
            "checks_under": sum(1 for c in checks if c["verdict"] == "under"),
            "checks_today": sum(1 for c in checks
                                if (c["created_at"] or 0) > day),
        }
        mine, live = _pick_instance(rosters, sess["user_id"], "", publisher)
        return page(request, "dashboard.html", session=sess, stats=stats,
                    recent_incidents=sorted(
                        incidents, key=lambda i: i["created_at"] or 0,
                        reverse=True)[:8],
                    recent_checks=checks[:8],
                    # Only instances someone is reporting *now*. A reporter
                    # that went quiet days ago is history, not a room.
                    rosters=live, my_instance=mine["key"] if mine else "")

    # ---------------- incidents ----------------
    @app.get("/incidents", response_class=HTMLResponse)
    def incidents_page(request: Request, status: str = "", q: str = ""):
        sess = require(request)
        rows = database.all_incidents()
        rows.sort(key=lambda i: i["created_at"] or 0, reverse=True)
        if status:
            rows = [i for i in rows if i["status"] == status]
        if q:
            needle = q.lower()
            rows = [i for i in rows if needle in _incident_haystack(i)]
        return page(request, "incidents.html", session=sess, incidents=rows,
                    status=status, q=q)

    @app.get("/incidents/{inc_id}", response_class=HTMLResponse)
    def incident_detail(request: Request, inc_id: str):
        sess = require(request)
        inc = database.get_incident(inc_id)
        if not inc or inc["deleted"]:
            return page(request, "not_found.html", session=sess,
                        what="incident")
        checks = [c for c in database.all_age_checks()
                  if c["incident_id"] == inc_id]
        return page(request, "incident_detail.html", session=sess,
                    inc=inc, checks=checks, report_text=report.build_report(inc),
                    has_clip=_media_ok(cfg, inc["clip_path"]),
                    has_shot=_media_ok(cfg, inc["screenshot_path"]))

    @app.get("/incidents/{inc_id}/report.txt", response_class=PlainTextResponse)
    def incident_report(request: Request, inc_id: str):
        require(request)
        inc = database.get_incident(inc_id)
        if not inc or inc["deleted"]:
            return PlainTextResponse("Not found", status_code=404)
        return PlainTextResponse(report.build_report(inc))

    @app.post("/incidents/{inc_id}/update")
    def incident_update(request: Request, inc_id: str, notes: str = Form(""),
                        status: str = Form("")):
        sess = require(request)
        inc = database.get_incident(inc_id)
        if inc and not inc["deleted"]:
            inc["notes"] = notes
            if status:
                inc["status"] = status
            inc["reported_by"] = inc["reported_by"] or sess["name"]
            database.upsert_incident(inc)
        return RedirectResponse(f"/incidents/{inc_id}", status_code=303)

    @app.post("/incidents/{inc_id}/edit")
    def incident_edit(request: Request, inc_id: str, action: str = Form(""),
                      reason: str = Form(""), names: list[str] = Form(default=[]),
                      user_ids: list[str] = Form(default=[])):
        """Correct a kick/warn/ban log. Admins only.

        A log is evidence, so the correction is recorded rather than quietly
        applied: the transcript keeps what it said before and who changed it.
        """
        sess = require(request)
        inc = database.get_incident(inc_id)
        if not is_admin(sess["user_id"]) or not inc or inc["deleted"]:
            return RedirectResponse(f"/incidents/{inc_id}", status_code=303)

        action = (action or "Kick").strip()
        reason = (reason or "").strip()
        # Same shape as the Kick Log form, so a corrected log is indexed and
        # searched exactly like one filed correctly first time.
        targets = []
        for name, uid in zip(names, list(user_ids) + [""] * len(names)):
            name, uid = name.strip(), _user_id_from(uid)
            if name or uid:
                targets.append({"name": name or uid, "user_id": uid})
        was = inc["trigger"]
        inc["trigger"] = f"{action} — {reason}"[:160]
        inc["transcript"] = list(inc["transcript"]) + [
            f"Reason: {reason}",
            f"Edited by {sess['name']} — was “{was}”"]
        if targets:
            inc["players"] = targets
        database.upsert_incident(inc)
        return RedirectResponse(f"/incidents/{inc_id}", status_code=303)

    @app.post("/incidents/{inc_id}/delete")
    def incident_delete(request: Request, inc_id: str):
        # Deleting a record about a real moderation action is an admin call —
        # everyone else can dismiss it, which keeps the history.
        sess = require(request)
        if is_admin(sess["user_id"]):
            database.delete_incident(inc_id)
            return RedirectResponse("/incidents", status_code=303)
        return RedirectResponse(f"/incidents/{inc_id}", status_code=303)

    # ---------------- age checks ----------------
    @app.get("/age-checks", response_class=HTMLResponse)
    def age_checks_page(request: Request, verdict: str = "", q: str = ""):
        sess = require(request)
        rows = database.all_age_checks()
        if verdict:
            rows = [c for c in rows if c["verdict"] == verdict]
        if q:
            needle = q.lower()
            rows = [c for c in rows
                    if needle in f"{c['name']} {c['user_id']}".lower()]
        # The same instance Screening would show them, so the name suggestions
        # are the people they can actually see.
        current, _ = _pick_instance(shown_rosters(), sess["user_id"],
                                    "", publisher)
        return page(request, "age_checks.html", session=sess, checks=rows,
                    verdict=verdict, q=q,
                    roster=current["players"] if current else [],
                    current=current, verdicts=agecheck.VERDICTS,
                    roster_scope=current["key"] if current else "")

    @app.post("/age-checks")
    def age_check_create(request: Request, name: str = Form(...),
                         user_id: str = Form(""), verdict: str = Form(...),
                         reported_age: str = Form(""), note: str = Form(""),
                         world_name: str = Form(""), world_id: str = Form(""),
                         instance_id: str = Form(""), next: str = Form("")):
        sess = require(request)
        # Recording from the Screening roster must land back on Screening,
        # with the filter and your place in the list intact — being thrown to
        # /age-checks after every verdict makes working through a room painful.
        back = _safe_next(next, "/age-checks")
        if verdict not in agecheck.VERDICTS:
            return RedirectResponse(back, status_code=303)
        age = int(reported_age) if reported_age.strip().isdigit() else None
        agecheck.record(
            database, name=name.strip(), user_id=user_id.strip(),
            verdict=verdict, reported_age=age, note=note.strip(),
            world_name=world_name, world_id=world_id, instance_id=instance_id,
            checked_by=sess["name"], checked_by_id=sess["user_id"],
            source="web",
            # A verdict is a note about a person; an incident is a moderation
            # action. Screening a room produces dozens of the first, and
            # filing each as the second buried the real kicks.
            file_incident=False)
        queue_invite_if_verified(verdict, user_id=user_id.strip(),
                                 name=name.strip(), sess=sess)
        return RedirectResponse(back, status_code=303)

    @app.post("/age-checks/{check_id}/delete")
    def age_check_delete(request: Request, check_id: str,
                         next: str = Form("")):
        require(request)
        database.delete_age_check(check_id)
        return RedirectResponse(_safe_next(next, "/age-checks"),
                                status_code=303)

    def file_moderation_log(*, action: str, reason: str, targets: list[dict],
                            moderator: str, moderator_id: str = "",
                            when: float | None = None, world_id: str = "",
                            instance_id: str = "", origin: str = "web",
                            age_hint: int | None = None,
                            log_id: str = "") -> dict:
        """One kick/warn/ban -> incident, age check, Discord. Shared by the
        manual Kick Log page and the audit-log prompt so both produce
        identical records.

        `log_id` lets a caller with a natural key use it — the audit prompt
        passes one derived from VRChat's own audit id, so filing the same kick
        twice overwrites one incident instead of creating a second.
        """
        # Filed already? Then this is a repeat submit, and everything below —
        # the age check, the Discord post — would happen a second time too.
        # One choke point for both callers.
        if log_id:
            existing = database.get_incident(log_id)
            if existing and not existing["deleted"]:
                return existing

        when = when or time.time()
        incident = {
            "id": log_id or db.new_id(), "created_at": when,
            "trigger": f"{action} — {reason}"[:160],
            "transcript": [f"Reason: {reason}"],
            "world_name": "", "world_id": world_id, "instance_id": instance_id,
            "players": targets,
            "clip_path": "", "screenshot_path": "", "notes": "",
            "status": "reported", "reported_by": moderator,
            "reported_by_id": moderator_id, "origin": origin,
        }
        database.upsert_incident(incident)

        verdict = agecheck.verdict_for_reason(reason)
        if verdict == "over" and cfg.get("auto_ban_overage"):
            # The group's rule: an adult kicked from a teen group is banned,
            # not merely removed. Queued rather than done here, because the
            # moderator filing this may not be the one holding the permission.
            for t in targets:
                if t.get("user_id"):
                    database.queue_group_action(
                        "ban", group_id=action_group(), user_id=t["user_id"],
                        user_name=t.get("name", ""),
                        reason=f"{action} — {reason}"[:160],
                        incident_id=incident["id"], asked_by=moderator,
                        asked_by_id=moderator_id)
        if verdict:
            age = age_hint if age_hint else agecheck.age_in_reason(reason)
            for t in targets:
                agecheck.record(
                    database, name=t.get("name", ""),
                    user_id=t.get("user_id", ""), verdict=verdict,
                    reported_age=age, world_id=world_id,
                    instance_id=instance_id, checked_by=moderator,
                    checked_by_id=moderator_id, source=origin, note=reason,
                    incident_id=incident["id"])

        discord.post(cfg, action=action, moderator=moderator, reason=reason,
                     timestamp=datetime.fromtimestamp(
                         when, tz=timezone.utc).isoformat(),
                     targets=[{**t, "link": discord.profile_url(
                         t.get("user_id", ""))} for t in targets])
        return incident

    def reason_chips(user_id: str) -> list[str]:
        """Shared shortcuts plus this moderator's own, de-duplicated."""
        out = list(cfg.get("common_reasons") or [])
        for r in database.user_reasons(user_id):
            if r not in out:
                out.append(r)
        return out

    # ---------------- one player ----------------
    @app.get("/player/{user_id}", response_class=HTMLResponse)
    def player_page(request: Request, user_id: str, name: str = ""):
        sess = require(request)
        cached = database.all_users().get(user_id, {})
        display = name or cached.get("name", "") or user_id

        # Live profile is a bonus, not a requirement: the record below is the
        # point, and VRChat being slow or the session being stale must not take
        # the page down with it.
        profile, profile_error = None, ""
        api = sessions.client(request.cookies.get(SESSION_COOKIE))
        if not api:
            profile_error = ("no live VRChat session — sign in again to load "
                             "the bio and pronouns")
        elif _USR_ID.fullmatch(user_id):
            try:
                profile = api.get_public_profile(user_id)
            except Exception as e:
                profile_error = f"couldn't load the VRChat profile: {e}"[:160]
        else:
            profile_error = "not a usr_ id, so there is no profile to load"

        if profile and profile.get("displayName"):
            display = profile["displayName"]

        history = database.history_for_user(user_id, display)
        latest = agecheck.latest_by_user(history["age_checks"])
        note_word = (cfg.get("note_filter") or "").strip().lower()
        note = cached.get("note", "")
        return page(
            request, "player.html", session=sess, user_id=user_id,
            display=display, profile=profile, profile_error=profile_error,
            trust=_trust_rank(profile), cached=cached, note=note,
            tagged=bool(note_word and note_word in note.lower()),
            staff=database.all_staff().get(user_id),
            history=history, latest_check=latest.get(user_id),
            verdicts=agecheck.VERDICTS,
            # So the Ban button can say plainly that nothing will be sent yet.
            bans_held=bool(cfg.get("hold_bans")))

    # ---------------- kick log (manual) ----------------
    @app.get("/kick-log", response_class=HTMLResponse)
    def kick_log_page(request: Request, ok: str = "", err: str = ""):
        sess = require(request)
        recent = [i for i in database.all_incidents()
                  if i["origin"] in ("web", "vrchat-audit", "teenchillout")]
        recent.sort(key=lambda i: i["created_at"] or 0, reverse=True)
        return page(request, "kick_log.html", session=sess,
                    reasons=reason_chips(sess["user_id"]),
                    my_reasons=database.user_reasons(sess["user_id"]),
                    recent=recent[:12], ok=ok, err=err,
                    # One per rendered form, so a form submitted twice files
                    # one log. A fresh page is a fresh id, so filing the same
                    # kick again on purpose still works.
                    form_id=secrets.token_hex(8))

    @app.post("/kick-log")
    def kick_log_submit(request: Request, action: str = Form("Kick"),
                        names: list[str] = Form(default=[]),
                        user_ids: list[str] = Form(default=[]),
                        reasons: list[str] = Form(default=[]),
                        detail: str = Form(""), form_id: str = Form("")):
        sess = require(request)
        targets = []
        for name, uid in zip(names, user_ids + [""] * len(names)):
            name, uid = name.strip(), _user_id_from(uid)
            if name or uid:
                targets.append({"name": name or uid, "user_id": uid})
        if not targets:
            return RedirectResponse("/kick-log?err=no_target", status_code=303)

        reason = ", ".join(r for r in reasons if r)
        if detail.strip():
            reason = f"{reason} - {detail.strip()}" if reason else detail.strip()
        if not reason:
            return RedirectResponse("/kick-log?err=no_reason", status_code=303)
        if action not in ("Kick", "Warn", "Ban"):
            action = "Kick"

        bare = detail.strip()
        file_moderation_log(
            action=action, reason=reason, targets=targets,
            moderator=sess["name"], moderator_id=sess["user_id"], origin="web",
            age_hint=int(bare) if bare.isdigit() and 1 <= int(bare) <= 120
            else None,
            # Derived from the form, so a double-clicked Submit resolves to
            # the same log rather than a second one. Ignored if a client sends
            # something that is not one of ours.
            log_id=f"web-{form_id}" if _clean_form_id(form_id) else "")
        return RedirectResponse("/kick-log?ok=1", status_code=303)

    @app.post("/kick-log/shortcut")
    def kick_log_add_shortcut(request: Request, reason: str = Form(""),
                              remove: str = Form(""), next: str = Form("")):
        sess = require(request)
        if remove:
            database.remove_user_reason(sess["user_id"], remove)
        else:
            database.add_user_reason(sess["user_id"], reason)
        return RedirectResponse(_safe_next(next, "/settings"), status_code=303)

    @app.get("/api/lookup-user")
    def lookup_user(request: Request, q: str = ""):
        """Resolve a VRChat profile link or usr_ id to a display name, using
        the signed-in moderator's own session."""
        require(request)
        uid = _user_id_from(q)
        if not uid:
            return JSONResponse({"error": "no usr_ id in that"}, status_code=400)
        api = sessions.client(request.cookies.get(SESSION_COOKIE))
        if not api:
            return JSONResponse({"error": "no live VRChat session"},
                                status_code=409)
        try:
            user = api.get_user(uid)
        except Exception as e:
            return JSONResponse({"error": str(e)[:120]}, status_code=502)
        return {"user_id": uid, "name": user.get("displayName", "")}

    # ---------------- pending kicks / warns ----------------
    @app.get("/pending", response_class=HTMLResponse)
    def pending_page(request: Request):
        sess = require(request)
        hide_others = hides_others(sess["user_id"])
        mine = database.pending_actions(sess["user_id"])
        others = [a for a in database.pending_actions()
                  if a["actor_id"] != sess["user_id"]]
        return page(request, "pending.html", session=sess, mine=mine,
                    # Hidden means hidden from this moderator's view only;
                    # the prompts stay queued for whoever they belong to.
                    others=[] if hide_others else others,
                    others_count=len(others), hide_others=hide_others,
                    audit=audit.status(),
                    reasons=reason_chips(sess["user_id"]),
                    recent=database.pending_actions(include_done=True)[:15])

    @app.post("/pending/others")
    def pending_others(request: Request, hide: str = Form("1")):
        """Show or hide other moderators' prompts, for this account."""
        sess = require(request)
        database.set_pref(sess["user_id"], "hide_others",
                          "1" if hide == "1" else "0")
        return RedirectResponse("/pending", status_code=303)

    @app.post("/pending/dismiss-others")
    def pending_dismiss_others(request: Request):
        """Clear every prompt that isn't yours.

        The same thing the per-item "not mine / ignore" button already does,
        done in one click. It ends the prompt for its owner as well, which is
        why the button asks first — the kick itself stays in VRChat's audit
        log either way, and can still be filed by hand from the Kick Log.
        """
        sess = require(request)
        for a in database.pending_actions():
            if a["actor_id"] != sess["user_id"]:
                database.dismiss_pending_action(a["id"])
        return RedirectResponse("/pending", status_code=303)

    @app.post("/pending/{action_id}/reason")
    def pending_reason(request: Request, action_id: str,
                       reasons: list[str] = Form(default=[]),
                       detail: str = Form(""), next: str = Form("")):
        """Turn a bare audit-log kick into a real, reasoned log entry."""
        sess = require(request)
        back = _safe_next(next, "/pending")
        action = database.get_pending_action(action_id)
        if not action or action["resolved_at"]:
            return RedirectResponse(back, status_code=303)

        reason = ", ".join(r for r in reasons if r)
        if detail.strip():
            reason = f"{reason} - {detail.strip()}" if reason else detail.strip()
        if not reason:
            return RedirectResponse(f"{back}?err=no_reason", status_code=303)

        # Claim it before filing anything. Two submits — a double click, or
        # the banner in one tab and /pending in another — both pass the check
        # above, and only this decides which one gets to file.
        if not database.claim_pending_action(action_id):
            return RedirectResponse(back, status_code=303)

        target = {"name": action["target_name"], "user_id": action["target_id"]}
        world_id, _, instance_id = (action["location"] or "").partition(":")
        bare = detail.strip()
        incident = file_moderation_log(
            action=action["action"], reason=reason, targets=[target],
            # Credit whoever VRChat recorded as the actor, not whoever typed
            # the reason in - they are often different people.
            moderator=action["actor_name"] or sess["name"],
            moderator_id=action["actor_id"],
            when=action["created_at"] or time.time(),
            world_id=world_id, instance_id=instance_id, origin="vrchat-audit",
            age_hint=int(bare) if bare.isdigit() and 1 <= int(bare) <= 120
            else None,
            # VRChat's own audit id, so even a filing that somehow slips past
            # the claim above lands on the same incident rather than a second.
            log_id=f"aud-{action_id}"[:64])
        database.resolve_pending_action(action_id, reason, incident["id"])
        return RedirectResponse(back, status_code=303)

    @app.post("/pending/{action_id}/dismiss")
    def pending_dismiss(request: Request, action_id: str,
                        next: str = Form("")):
        require(request)
        database.dismiss_pending_action(action_id)
        return RedirectResponse(_safe_next(next, "/pending"), status_code=303)

    # ---------------- screening ----------------
    @app.get("/screening", response_class=HTMLResponse)
    def screening(request: Request, show: str = "", q: str = "",
                  instance: str = "", reporter: str = ""):
        sess = require(request)
        rosters = shown_rosters()
        current, choices = _pick_instance(rosters, sess["user_id"],
                                          instance or reporter, publisher)
        cached = database.all_users()
        latest = agecheck.latest_by_user(database.all_age_checks())
        note_word = (cfg.get("note_filter") or "").strip().lower()
        roster_staff = database.all_staff()
        rows = []
        for p in sorted(current["players"] if current else [],
                        key=lambda p: (p.get("name") or "").lower()):
            uid = p.get("user_id") or ""
            rec = cached.get(uid, {})
            note = rec.get("note", "")
            tagged = bool(note_word and note_word in note.lower())
            check = latest.get(uid)
            groups = rec.get("groups", [])
            # Fellow moderators in the room: worth flagging so nobody wastes a
            # screening pass on staff. Uses the same group match as sign-in, so
            # the badge means exactly "could log into this tool".
            mod_groups = staff_groups(groups, cfg.get("staff_group", ""))
            # The imported allowlist knows the actual rank; fall back to the
            # group check for staff who predate it or aren't listed.
            listed = roster_staff.get(uid)
            rows.append({
                "name": p.get("name", ""), "user_id": uid,
                "note": note, "tagged": tagged,
                "groups": groups,
                "is_mod": bool(mod_groups or listed),
                "role": (listed or {}).get("role", "MOD" if mod_groups else ""),
                "staff_groups": mod_groups,
                "check": check,
                "state": _screen_state(check, tagged),
            })

        counts = {
            "all": len(rows),
            "unverified": sum(1 for r in rows if r["state"] != "verified"),
            "verified": sum(1 for r in rows if r["state"] == "verified"),
            "unchecked": sum(1 for r in rows if r["state"] == "unchecked"),
            "under": sum(1 for r in rows if r["state"] == "under"),
            "over": sum(1 for r in rows if r["state"] == "over"),
        }
        # Unverified is deliberately broader than unchecked: someone marked
        # under or over range has been looked at but is not cleared, and
        # hiding them behind "checked" is how people get missed.
        if show == "unverified":
            rows = [r for r in rows if r["state"] != "verified"]
        elif show in ("verified", "unchecked", "under", "over"):
            rows = [r for r in rows if r["state"] == show]
        if q:
            needle = q.lower()
            rows = [r for r in rows
                    if needle in f"{r['name']} {r['user_id']}".lower()]

        return page(request, "screening.html", session=sess, rows=rows,
                    current=current, rosters=rosters, show=show, q=q,
                    counts=counts, verdicts=agecheck.VERDICTS,
                    # Every instance being reported right now, so somebody
                    # without an agent of their own can pick one, and somebody
                    # with one can look at a colleague's.
                    choices=choices, instance=instance or reporter,
                    mine=bool(current and sess["user_id"] in current["owners"]),
                    # Scopes the reload poll to this instance: a join in a
                    # world you are not looking at must not reload your page.
                    roster_scope=current["key"] if current else "",
                    live=bool(current and current["live"]),
                    # Only claim "read from this PC's log" if this server is
                    # actually the one reading it. A database restored onto a
                    # different host carries the local reporter's row with it.
                    source_local=bool(
                        publisher and current
                        and any(r["client_id"] == LOCAL_CLIENT
                                for r in current["reporters"])))

    @app.post("/screening/tag")
    def screening_tag(request: Request, user_id: str = Form(...),
                      name: str = Form(""), next: str = Form("")):
        """Write the verification word into your VRChat note for this user."""
        sess = require(request)
        back = _safe_next(next, "/screening")
        api = sessions.client(request.cookies.get(SESSION_COOKIE))
        word = (cfg.get("note_filter") or "").strip()
        if not api or not word:
            return RedirectResponse("/screening?err=no_api", status_code=303)
        cached = database.all_users().get(user_id, {})
        existing = cached.get("note", "")
        if word.lower() not in existing.lower():
            new_note = f"{existing} {word}".strip() if existing else word
            try:
                api.update_user_note(user_id, new_note)
            except Exception:
                return RedirectResponse("/screening?err=tag_failed",
                                        status_code=303)
            rec = dict(cached)
            rec.update({"name": name or cached.get("name", ""),
                        "note": new_note, "checked_at": time.time(),
                        "groups": cached.get("groups", [])})
            database.upsert_user(user_id, rec)
        agecheck.record(
            database, name=name, user_id=user_id, verdict="in_range",
            checked_by=sess["name"], checked_by_id=sess["user_id"],
            source="web", note=f"tagged '{word}' in VRChat note")
        queue_invite_if_verified("in_range", user_id=user_id, name=name,
                                 sess=sess)
        return RedirectResponse(back, status_code=303)

    # ---------------- your account ----------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        sess = require(request)
        return page(request, "settings.html", session=sess,
                    my_reasons=database.user_reasons(sess["user_id"]),
                    my_keys=database.agent_keys(sess["user_id"]),
                    agent_exe=_agent_exe(cfg),
                    base_url=_public_base(request, cfg))

    @app.post("/settings/key")
    def settings_key(request: Request, label: str = Form("")):
        """Mint a key by hand, for setting an agent up without pairing."""
        sess = require(request)
        database.add_agent_key(sess["user_id"], sess["name"],
                               label or "Added by hand",
                               secrets.token_urlsafe(24))
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/key/{key_id}/revoke")
    def settings_key_revoke(request: Request, key_id: str,
                            next: str = Form("/settings")):
        """Revoke one agent. Yours always; anybody's if you are an admin."""
        sess = require(request)
        row = database.agent_key(key_id)
        if row and (row["user_id"] == sess["user_id"]
                    or is_admin(sess["user_id"])):
            database.revoke_agent_key(key_id)
        return RedirectResponse(_safe_next(next, "/settings"), status_code=303)

    @app.get("/settings/agent")
    def settings_agent(request: Request):
        # Signed-in only. The download is a build of our own client with a
        # server address in it; there is no reason for it to be public.
        require(request)
        exe = _agent_exe(cfg)
        if not exe:
            return PlainTextResponse(
                "No agent build on this server yet. Build one with "
                "`python build_agent.py --server … --token …` and point "
                "\"agent_exe\" in web_config.json at the result.",
                status_code=404)
        return FileResponse(exe, filename=exe.name,
                            media_type="application/octet-stream")

    # ---------------- moderation log & leaderboard ----------------
    def all_logs(sess: dict) -> list[dict]:
        """Every kick, warn and ban, with who filed it, unfiltered.

        This is what the leaderboards count. A ban *total* is not sensitive —
        knowing a colleague placed three is ordinary team information — while
        the entries themselves say who was banned and why, which is not.
        """
        me = sess["user_id"]
        out = []
        for inc in database.all_incidents():
            if inc["deleted"]:
                continue
            action = _log_action(inc)
            if not action:
                continue
            mine = (inc.get("reported_by_id") == me if inc.get("reported_by_id")
                    # Rows filed before ids were recorded can only be matched
                    # on the name they were filed under.
                    else inc.get("reported_by") == sess["name"])
            out.append({**inc, "action": action, "reason": _log_reason(inc),
                        "mine": mine})
        return out

    def readable_logs(logs: list[dict], sess: dict) -> list[dict]:
        """The entries this moderator may actually read.

        Bans are narrower than the rest: an admin sees all of them, everybody
        else sees only their own. A ban is the heaviest thing this group does
        to somebody, and who has been banned is not general staff reading.
        """
        admin = is_admin(sess["user_id"])
        return [inc for inc in logs
                if inc["action"] != "Ban" or admin or inc["mine"]]


    @app.get("/mod-log", response_class=HTMLResponse)
    def mod_log(request: Request, action: str = "", who: str = "",
                days: str = "30", q: str = "", full: str = ""):
        sess = require(request)
        every = all_logs(sess)
        rows = readable_logs(every, sess)

        window = {"7": 7, "30": 30, "90": 90}.get(days)
        since = time.time() - window * 86400 if window else 0

        # The leaderboard counts the same rows the log shows, so a moderator
        # cannot be credited with bans they are not allowed to see. Totals
        # therefore differ between an admin and everybody else — deliberately.
        board: dict[str, dict] = {}
        # Every log filed before ids were recorded carries only a display
        # name, and almost all of the history is like that. Resolving the name
        # back to an account keeps one person one row instead of splitting
        # them into "before" and "after".
        by_name = {(u["name"] or "").lower(): u["user_id"]
                   for u in database.known_users() if u["name"]}
        for staff_id, staff_row in database.all_staff().items():
            by_name.setdefault((staff_row.get("name") or "").lower(), staff_id)

        def bucket(name: str, uid: str) -> dict:
            uid = uid or by_name.get((name or "").lower(), "")
            key = uid or f"name:{name.lower()}"
            return board.setdefault(key, {
                "name": name or "unknown", "user_id": uid,
                "Kick": 0, "Warn": 0, "Ban": 0, "checks": 0, "total": 0})

        for inc in every:
            if (inc["created_at"] or 0) < since:
                continue
            who_row = bucket(inc["reported_by"], inc.get("reported_by_id", ""))
            who_row[inc["action"]] += 1
            who_row["total"] += 1
        for chk in database.all_age_checks():
            if chk["deleted"] or (chk["created_at"] or 0) < since:
                continue
            who_row = bucket(chk["checked_by"], chk.get("checked_by_id", ""))
            who_row["checks"] += 1
            who_row["total"] += 1

        # One board per kind of work, not a combined total. Kicking and
        # screening are different jobs — someone who works through a room of
        # age checks all night and never kicks anybody is not "behind" the
        # person who kicked four people, and one ranking says they are.
        # Expanded, the boards list everyone who appears in the period at all,
        # including a zero — "who has done none of this" is a fair question and
        # a missing row does not answer it. Collapsed, the top ten with
        # something to their name.
        everyone = full == "1"

        def top(field: str) -> list[dict]:
            ranked = list(board.values()) if everyone else [
                r for r in board.values() if r[field]]
            ranked.sort(key=lambda r: (-r[field], r["name"].lower()))
            return ranked if everyone else ranked[:10]

        boards = [("Kicks", "Kick", top("Kick")),
                  ("Warns", "Warn", top("Warn")),
                  ("Bans", "Ban", top("Ban")),
                  ("Age checks", "checks", top("checks"))]

        shown = [r for r in rows if (r["created_at"] or 0) >= since]
        if action in _LOG_ACTIONS:
            shown = [r for r in shown if r["action"] == action]
        if who:
            shown = [r for r in shown
                     if r.get("reported_by_id") == who
                     or r["reported_by"] == who]
        if q:
            needle = q.lower()
            shown = [r for r in shown if needle in _incident_haystack(r)]
        shown.sort(key=lambda r: r["created_at"] or 0, reverse=True)

        # The chips count what the list will show; the boards count what
        # actually happened. For a non-admin those differ on bans, which is
        # the point — the number is public, the entries are not.
        counts = {a: sum(1 for r in rows if r["action"] == a
                         and (r["created_at"] or 0) >= since)
                  for a in _LOG_ACTIONS}
        totals = {a: sum(1 for r in every if r["action"] == a
                         and (r["created_at"] or 0) >= since)
                  for a in _LOG_ACTIONS}
        return page(request, "mod_log.html", session=sess,
                    boards=boards, rows=shown[:300], counts=counts,
                    totals=totals,
                    action=action, who=who, days=days, q=q,
                    full=everyone, people=len(board),
                    total_shown=len(shown),
                    # So the page can say why a moderator's ban column is
                    # empty rather than looking broken.
                    sees_all_bans=is_admin(sess["user_id"]))

    # ---------------- admin ----------------
    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        sess = require(request)
        roots = {str(r).strip() for r in (cfg.get("root_admins") or [])}
        admins = database.all_admins()
        listed = {a["user_id"] for a in admins}
        # Root admins are in whether or not anybody wrote them down.
        for uid in roots - listed:
            known = {u["user_id"]: u["name"] for u in database.known_users()}
            admins.insert(0, {"user_id": uid, "name": known.get(uid, uid),
                              "added_by": "", "added_at": 0})
        return page(
            request, "admin.html", session=sess, admins=admins, roots=roots,
            am_admin_page=is_admin(sess["user_id"]),
            # Only people who have actually signed in can be appointed: the
            # tool has to have seen the account to know its id is real.
            candidates=[u for u in database.known_users()
                        if u["user_id"] not in {a["user_id"] for a in admins}],
            keys=database.agent_keys(),
            queue=database.group_actions(limit=40),
            queue_status=groupwork.status(),
            auto_ban=bool(cfg.get("auto_ban_overage")),
            auto_invite=bool(cfg.get("auto_invite_verified")),
            act_group=action_group())

    @app.post("/admin/queue/{action_id}/cancel")
    def admin_queue_cancel(request: Request, action_id: str):
        sess = require(request)
        if is_admin(sess["user_id"]):
            database.cancel_group_action(action_id)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/player/{user_id}/ban")
    def player_ban(request: Request, user_id: str, name: str = Form(""),
                   reason: str = Form(""), next: str = Form("")):
        """Ban from the group, and log it. Admins only.

        The ban is queued rather than attempted here: whoever is filing it may
        not be the one holding `group-bans-manage`, and a ban that has to wait
        ten minutes for the right person to sign in is better than one that
        fails silently at the moment of clicking.
        """
        sess = require(request)
        back = _safe_next(next, f"/player/{user_id}")
        if not is_admin(sess["user_id"]) or not _user_id_from(user_id):
            return RedirectResponse(back, status_code=303)
        reason = reason.strip() or "Banned by a moderator"
        target = {"name": name.strip() or user_id, "user_id": user_id}
        incident = file_moderation_log(
            action="Ban", reason=reason, targets=[target],
            moderator=sess["name"], moderator_id=sess["user_id"], origin="web")
        queue_ban(target, reason=reason, sess=sess,
                  incident_id=incident["id"])
        return RedirectResponse(back, status_code=303)

    @app.post("/admin/admins")
    def admin_admins(request: Request, user_id: str = Form(""),
                     remove: str = Form("")):
        sess = require(request)
        if not is_admin(sess["user_id"]):
            return RedirectResponse("/admin", status_code=303)
        roots = {str(r).strip() for r in (cfg.get("root_admins") or [])}
        if remove:
            if remove not in roots:        # a root admin cannot be removed
                database.remove_admin(remove)
        elif user_id:
            known = {u["user_id"]: u["name"] for u in database.known_users()}
            if user_id in known:           # never appoint an id we've not seen
                database.add_admin(user_id, known[user_id], sess["name"])
        return RedirectResponse("/admin", status_code=303)

    # ---------------- agent pairing ----------------
    # The agent asks for a code, shows it with a link, and polls. A moderator
    # opens the link in the panel and approves, and only then does the key
    # travel — server to agent, over the same connection the roster will use.
    # Nobody has to read a key off a screen or paste one into a chat.
    pair_hits: dict[str, list[float]] = {}

    @app.post("/api/agent/pair/start")
    def pair_start(request: Request, payload: dict = Body(default={})):
        # Unauthenticated by necessity — the agent has no credential yet, which
        # is the whole point — so it is rate limited per IP and the rows it
        # creates are worthless until a moderator approves one.
        ip = _client_ip(request)
        now = time.time()
        hits = [t for t in pair_hits.get(ip, []) if now - t < 300]
        if len(hits) >= 10:
            raise _ApiError(429, "too many pairing attempts; wait a few minutes")
        hits.append(now)
        pair_hits[ip] = hits

        code = _pair_code()
        secret = secrets.token_urlsafe(32)
        database.create_pairing(code, token_hash(secret),
                                payload.get("client_name", ""), PAIR_TTL)
        return {"code": code, "secret": secret,
                "url": f"{_public_base(request, cfg)}/pair/{code}",
                "expires_in": PAIR_TTL}

    @app.post("/api/agent/pair/poll")
    def pair_poll(payload: dict = Body(...)):
        code = (payload.get("code") or "").strip().upper()
        secret = payload.get("secret") or ""
        row = database.get_pairing(code)
        # Same answer for a wrong code and a wrong secret: a guessed code
        # should not confirm itself.
        if not row or not secret or not secrets.compare_digest(
                token_hash(secret), row["secret_hash"] or ""):
            raise _ApiError(404, "no such pairing")
        if row["denied_at"]:
            raise _ApiError(410, "that request was declined in the panel")
        if row["claimed_at"]:
            raise _ApiError(410, "that code has already been used")
        if row["expires_at"] < time.time():
            raise _ApiError(410, "that code expired — start again")
        if not row["approved_at"]:
            return {"status": "pending"}
        # Minted here rather than on approval, so a link opened and then
        # abandoned leaves no key behind for its owner to wonder about.
        key = database.add_agent_key(row["user_id"], row["user_name"],
                                     row["client_name"],
                                     secrets.token_urlsafe(24))
        database.claim_pairing(code)
        return {"status": "approved", "token": key["roster_key"],
                "name": row["user_name"]}

    @app.get("/pair/{code}", response_class=HTMLResponse)
    def pair_page(request: Request, code: str):
        """Opening the link *is* the approval, provided you are signed in.

        A pairing is worth nothing without the secret the agent kept, so the
        thing this authorises is "the PC that just showed me this code reports
        as me" — and the page it lands on can revoke it in one click.
        """
        sess = require(request)
        code = code.strip().upper()
        row = database.get_pairing(code)
        if row and not (row["approved_at"] or row["denied_at"]
                        or row["claimed_at"]) \
                and row["expires_at"] >= time.time():
            database.settle_pairing(code, user_id=sess["user_id"],
                                    user_name=sess["name"])
            row = database.get_pairing(code)
        return page(request, "pair.html", session=sess, code=code,
                    pairing=row, now=time.time())

    @app.post("/pair/{code}")
    def pair_deny(request: Request, code: str):
        """Undo: mark it declined, and pull the key if one was already taken."""
        sess = require(request)
        code = code.strip().upper()
        row = database.get_pairing(code)
        if row and row["user_id"] in ("", None, sess["user_id"]):
            database.settle_pairing(code, denied=True)
            for k in database.agent_keys(sess["user_id"]):
                if k["label"] == (row["client_name"] or "") \
                        and k["created_at"] >= (row["approved_at"] or 0):
                    database.revoke_agent_key(k["id"])
        return RedirectResponse(f"/pair/{code}", status_code=303)

    # ---------------- media ----------------
    @app.get("/media/{kind}/{inc_id}")
    def media(request: Request, kind: str, inc_id: str):
        require(request)
        inc = database.get_incident(inc_id)
        if not inc:
            return PlainTextResponse("Not found", status_code=404)
        path = inc["clip_path"] if kind == "clip" else inc["screenshot_path"]
        resolved = _media_ok(cfg, path)
        if not resolved:
            return PlainTextResponse(
                "Not available on this server", status_code=404)
        return FileResponse(resolved)

    # ---------------- sync API ----------------
    def require_token(token: str | None, *,
                      allow_roster: bool = False) -> dict | None:
        """Full sync token, or — on the roster endpoint only — a roster key.

        The roster credentials ship inside a binary handed to moderators, so
        they are assumed public. Keeping them off push/pull is what stops a
        leaked agent from reading age checks and incidents.

        Returns the moderator whose personal key was used, or None for the
        shared tokens, so the roster can be attributed to a person.
        """
        accepted = [(cfg.get("sync_token") or "").strip()]
        if allow_roster:
            accepted.append((cfg.get("roster_token") or "").strip())
        accepted = [t for t in accepted if t]
        if not accepted:
            raise _ApiError(503, "sync API disabled: no sync_token configured")
        if token and any(secrets.compare_digest(token, t) for t in accepted):
            return None
        if allow_roster and token:
            owner = database.agent_key_by_secret(token)
            if owner:
                database.touch_agent_key(token)
                return owner
        raise _ApiError(401, "bad sync token")

    @app.exception_handler(_ApiError)
    async def _api_error(_request: Request, exc: "_ApiError"):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.post("/api/sync/push")
    def sync_push(payload: dict = Body(...),
                  x_sync_token: str | None = Header(None)):
        """Desktop → server. Applies records and reports the server clock."""
        require_token(x_sync_token)
        applied = {"incidents": 0, "age_checks": 0}
        for inc in payload.get("incidents") or []:
            if inc.get("id") and database.upsert_incident(inc):
                applied["incidents"] += 1
        for chk in payload.get("age_checks") or []:
            if chk.get("id") and database.upsert_age_check(chk):
                applied["age_checks"] += 1
        if payload.get("client_id") and payload.get("roster") \
                and _roster_allowed(cfg, payload["roster"]):
            database.upsert_roster(payload["client_id"], payload["roster"],
                                   payload.get("client_name", ""))
        return {"ok": True, "applied": applied, "server_time": time.time()}

    @app.get("/api/sync/pull")
    def sync_pull(since: float = 0.0,
                  x_sync_token: str | None = Header(None)):
        """Server → desktop. Everything touched after `since`, tombstones
        included, plus the watermark to pass as `since` next time."""
        require_token(x_sync_token)
        incidents = database.incidents_since(since)
        checks = database.age_checks_since(since)
        watermark = max([since]
                        + [i["updated_at"] for i in incidents]
                        + [c["updated_at"] for c in checks])
        return {"incidents": incidents, "age_checks": checks,
                "watermark": watermark, "server_time": time.time()}

    @app.post("/api/sync/roster")
    def sync_roster(payload: dict = Body(...),
                    x_sync_token: str | None = Header(None)):
        owner = require_token(x_sync_token, allow_roster=True)
        roster = payload.get("roster", {})
        # Dropped rather than stored and hidden: a moderator's private world is
        # not this server's business to keep a copy of. 200 so the agent takes
        # it as a normal answer and says so, rather than retrying an error.
        if not _roster_allowed(cfg, roster):
            return {"ok": True, "ignored": "not an instance this server "
                                           "moderates — roster discarded"}
        # A paired key names the moderator who is in the instance, which is
        # what Screening should show — the agent otherwise reports a PC's
        # hostname, and "DESKTOP-4F9K2" says nothing about who to ask.
        name = owner["user_name"] if owner else payload.get("client_name", "")
        database.upsert_roster(payload.get("client_id", "default"), roster,
                               name, owner["user_id"] if owner else "")
        return {"ok": True}

    @app.post("/api/sync/staff")
    def sync_staff(payload: dict = Body(...),
                   x_sync_token: str | None = Header(None)):
        """Moderator allowlist, imported from the Teen Chillout web tool."""
        require_token(x_sync_token)
        applied = sum(1 for rec in (payload.get("staff") or [])
                      if database.upsert_staff(rec))
        return {"ok": True, "applied": applied}

    @app.get("/api/state")
    def api_state(request: Request, instance: str = "", reporter: str = ""):
        """Polled by open pages to notice new records without a full reload.

        `instance` is the room the page is showing, so it only hears about
        joins and leaves in that one. `reporter` is the older, per-agent form,
        still accepted for a page loaded before this deploy.
        """
        if not session_of(request):
            return JSONResponse({"error": "signed out"}, status_code=401)
        scope = instance or (f"client:{reporter}" if reporter else "")
        return {"version": database.state_version(scope)}

    @app.get("/healthz")
    def healthz():
        # started_at changes only when the process is replaced, which is how
        # you (and the tests) can tell a code change actually took effect
        # rather than the old process still answering.
        return {"ok": True, "time": time.time(), "started_at": started_at}

    return app


# ---------------- helpers ----------------
#: VRChat writes the owning group into the instance id itself, e.g.
#: "73644~group(grp_7112…)~groupAccessType(public)~region(us)".
_INSTANCE_GROUP = re.compile(r"~group\((grp_[0-9a-fA-F-]+)\)")


#: Logs are stored as "Kick — reason"; the action is the part before the dash.
_LOG_ACTIONS = ("Kick", "Warn", "Ban")


def _log_action(inc: dict) -> str:
    """Kick, Warn or Ban if this incident is a moderation log, else ""."""
    trigger = (inc.get("trigger") or "").strip()
    for action in _LOG_ACTIONS:
        if trigger == action or trigger.startswith(f"{action} "):
            return action
    return ""


def _log_reason(inc: dict) -> str:
    trigger = (inc.get("trigger") or "").strip()
    _, dash, rest = trigger.partition("—")
    return (rest if dash else trigger).strip()


def _instance_group(instance_id: str) -> str:
    match = _INSTANCE_GROUP.search(instance_id or "")
    return match.group(1) if match else ""


def _roster_allowed(cfg: dict, roster: dict) -> bool:
    """Whether this instance is one this server moderates.

    A moderator sitting in a private world, a friend's instance or somebody
    else's group is not on duty, and those rosters have no business on the
    Screening page — that page is where age verdicts get recorded, against
    people the group never invited.
    """
    want = (cfg.get("roster_group") or "").strip()
    if not want:
        return True
    return _instance_group(roster.get("instance_id", "")) == want


def _instance_key(roster: dict) -> str:
    """What counts as "the same room" across reporters.

    A world plus an instance id, because that is what two moderators standing
    together share. Anything that arrives without one is kept to itself rather
    than merged — several unknown rooms are not one room.
    """
    world, inst = roster.get("world_id") or "", roster.get("instance_id") or ""
    return f"{world}:{inst}" if (world or inst) else f"client:{roster['client_id']}"


def _merge_rosters(rosters: list[dict], publisher) -> list[dict]:
    """One entry per instance, however many agents are reporting it.

    Two moderators in the same room see the same list. Their logs are not
    identical — each client learns about a join when it renders the avatar, so
    one is usually a few seconds ahead and someone who joined behind you may be
    missing from your own log entirely — so the union is a better answer than
    either report, and much better than flipping between them.
    """
    grouped: dict[str, list[dict]] = {}
    for r in rosters:
        grouped.setdefault(_instance_key(r), []).append(r)

    out = []
    for key, reporters in grouped.items():
        reporters.sort(key=lambda r: r["seen_at"], reverse=True)
        live = [r for r in reporters if _roster_live(r, publisher)]
        # Only reporters that are still reporting describe the room. An agent
        # that went quiet hours ago left a list of who was there *then*, and
        # merging that in showed everyone who had passed through all day —
        # 184 names for a 36-player instance. With none live at all, the most
        # recent one is the last thing known, and the page says it is stale.
        current = live or reporters[:1]

        players, seen = [], set()
        for r in current:
            for p in r["players"]:
                ident = (p.get("user_id") or "").lower() or \
                    f"name:{(p.get('name') or '').lower()}"
                if ident and ident not in seen:
                    seen.add(ident)
                    players.append(p)
        players.sort(key=lambda p: (p.get("name") or "").lower())

        out.append({
            "key": key,
            # The freshest reporter names the world: an agent that has just
            # moved knows the new name before a quieter one does.
            "world_name": next((r["world_name"] for r in current
                                if r["world_name"]), ""),
            "world_id": current[0]["world_id"],
            "instance_id": current[0]["instance_id"],
            "players": players,
            "reporters": current,
            # Ownership follows the agents actually in there, so a moderator
            # whose agent died does not keep being shown a room as "yours".
            "owners": {r["user_id"] for r in current if r.get("user_id")},
            "updated_at": max(r["updated_at"] for r in current),
            "seen_at": max(r["seen_at"] for r in current),
            "live": bool(live),
        })

    out.sort(key=lambda i: i["seen_at"], reverse=True)
    return out


def _pick_instance(rosters: list[dict], user_id: str, want: str,
                   publisher) -> tuple[dict | None, list[dict]]:
    """Which room this moderator is looking at, and what else is live.

    Yours by default — the room you are standing in is the one you need, and
    mixing a colleague's instance into it is how somebody gets screened against
    a list they were never on.

    Falling back to somebody else's when you have no agent is deliberate: a
    moderator screening from a phone has no way to report a roster, and the
    alternative for them is an empty page. Where there is a choice they are
    asked to make it rather than being handed an arbitrary room.
    """
    instances = _merge_rosters(rosters, publisher)
    live = [i for i in instances if i["live"]]
    if want:
        chosen = next((i for i in instances if i["key"] == want), None)
        if not chosen:      # a link from before the merge, naming one reporter
            chosen = next((i for i in instances
                           if any(r["client_id"] == want
                                  for r in i["reporters"])), None)
        if chosen:
            return chosen, live
    mine = next((i for i in live if user_id in i["owners"]), None)
    if mine:
        return mine, live
    if len(live) == 1:
        return live[0], live
    # Several live and none of them yours: no basis for picking, so don't.
    return (None, live) if live else (instances[0] if instances else None, live)


def _clean_form_id(value: str) -> bool:
    """Ours are 16 hex characters; anything else gets a random id instead."""
    return bool(value) and len(value) == 16 and all(
        c in "0123456789abcdef" for c in value)


def _pair_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _agent_exe(cfg: dict) -> Path | None:
    """The packaged roster agent, if this server has one to hand out.

    Absent is the normal case on a fresh checkout — build_agent.py has to be
    run on Windows — so the settings page checks rather than offering a link
    that 404s.
    """
    configured = (cfg.get("agent_exe") or "").strip()
    path = (Path(configured) if configured
            else ROOT / "dist" / "VRChatRosterAgent.exe")
    return path if path.is_file() else None


def _public_base(request: Request, cfg: dict) -> str:
    """The URL a moderator's agent should be pointed at.

    Read off the request rather than the config because the server has no idea
    what it is called from outside — behind the tunnel it only ever binds
    127.0.0.1 — and forced to https where the session cookie is already
    Secure-only, since the proxy terminates TLS and forwards plain http.
    """
    base = str(request.base_url).rstrip("/")
    if cfg.get("https_only") and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


class _ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "?")


def _incident_haystack(inc: dict) -> str:
    players = " ".join(f"{p.get('name', '')} {p.get('user_id', '')}"
                       for p in inc.get("players", []))
    return (f"{inc['trigger']} {inc['world_name']} {inc['notes']} "
            f"{' '.join(inc['transcript'])} {players}").lower()


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_ago(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = max(0, time.time() - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= size:
            return f"{int(delta // size)}{unit} ago"
    return "just now"


#: A pushed roster older than this is treated as history, not a live list.
_PUSHED_STALE_AFTER = 180.0


def _roster_live(current: dict | None, publisher) -> bool:
    """Whether the roster on screen still reflects who is actually present.

    For a roster this server reads itself, liveness is whether VRChat is still
    writing its log — the row's own timestamp only moves when somebody joins or
    leaves, so a quiet instance would look dead. For one pushed by a remote
    desktop client, the push time is all we have.
    """
    if not current:
        return False
    if current["client_id"] == LOCAL_CLIENT and publisher:
        return publisher.is_live()
    # seen_at, not updated_at: a reporter sitting in a quiet instance where
    # nobody joins or leaves is still very much alive.
    return (time.time() - (current["seen_at"] or 0)) < _PUSHED_STALE_AFTER


_USR_ID = re.compile(r"(usr_[0-9a-f-]{36})")


def _user_id_from(text: str) -> str:
    """Accept a bare usr_ id or a pasted profile link, as moderators send both."""
    match = _USR_ID.search(text or "")
    return match.group(1) if match else ""


#: VRChat exposes trust as tags rather than a rank; highest present wins.
_TRUST = [("system_trust_veteran", "Trusted User"),
          ("system_trust_trusted", "Known User"),
          ("system_trust_known", "User"),
          ("system_trust_basic", "New User")]


def _trust_rank(profile: dict | None) -> str:
    tags = set((profile or {}).get("trustTags") or [])
    return next((label for tag, label in _TRUST if tag in tags), "Visitor")


def _safe_next(value: str, fallback: str) -> str:
    """Where to go after an action, taken from the form but not trusted.

    Only a same-site absolute path is allowed. `//evil.example` is a
    protocol-relative URL that browsers treat as another origin, so rejecting
    it is what stops this becoming an open redirect.
    """
    value = (value or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def _screen_state(check: dict | None, tagged: bool) -> str:
    """Where a player stands: verified | under | over | unchecked.

    Two things can clear someone — an in-range check recorded here, or the
    verification word already sitting in your VRChat note (which is how the
    desktop app has always tracked it, including auto-verified group members).
    """
    if check:
        return "verified" if check["verdict"] == "in_range" else check["verdict"]
    return "verified" if tagged else "unchecked"


def _short_instance(instance_id: str) -> str:
    """Instance ids carry region/access qualifiers; the leading number is
    what people actually refer to."""
    return (instance_id or "").split("~")[0]


def _media_roots(cfg: dict) -> list[Path]:
    roots = [SHOTS_DIR] + [Path(p) for p in (cfg.get("media_roots") or [])]
    return [r.resolve() for r in roots if r.exists()]


def _media_ok(cfg: dict, path: str) -> Path | None:
    """Resolve a recorded media path, but only inside allowed roots.

    Paths in the DB may come from a desktop client over the sync API, so they
    are untrusted input — without this check a crafted incident could ask the
    server to hand back any file it can read.
    """
    if not path or not cfg.get("serve_media", True):
        return None
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    for root in _media_roots(cfg):
        if resolved.is_relative_to(root):
            return resolved
    return None
