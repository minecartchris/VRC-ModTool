"""FastAPI app: web UI for incidents and age checks, plus the desktop sync API.

Run it with `python run_web.py` (or run_web.bat). It reads and writes the same
modtool.db as the Tkinter app, so a check filed in VR from the desktop and one
filed from a phone in the browser land in the same table.

Two ways in:
  * moderators — VRChat sign-in, authorised by staff-group membership (auth.py)
  * desktop clients — a shared token on /api/sync/* (no VRChat login needed)
"""

import secrets
import time
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
from webapp.auth import AuthError, SessionManager

SESSION_COOKIE = "modsession"
HERE = Path(__file__).resolve().parent


class LoginRequired(Exception):
    """Raised by page handlers when there is no valid session."""


def create_app(cfg: dict | None = None, database: "db.Database | None" = None):
    cfg = cfg or webconfig.load()
    database = database or db.Database()
    sessions = SessionManager(database, cfg)

    app = FastAPI(title="VRChat Mod Suite", docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.db = database
    app.state.sessions = sessions

    app.mount("/static", StaticFiles(directory=str(HERE / "static")),
              name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(
        fmt_time=_fmt_time, fmt_ago=_fmt_ago, verdict_label=agecheck.LABELS,
        short_instance=_short_instance)

    # ---------------- plumbing ----------------
    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        return RedirectResponse(
            f"/login?next={quote(request.url.path)}", status_code=303)

    def session_of(request: Request) -> dict | None:
        return sessions.get(request.cookies.get(SESSION_COOKIE))

    def require(request: Request) -> dict:
        sess = session_of(request)
        if not sess:
            raise LoginRequired()
        return sess

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        sess = ctx.pop("session", None)
        return templates.TemplateResponse(request, name, {
            "session": sess, "cfg": cfg,
            # What the page was rendered from; the browser polls /api/state
            # and reloads when this moves. See static/refresh.js.
            "state_version": database.state_version() if sess else "",
            "live_api": bool(sessions.client(
                request.cookies.get(SESSION_COOKIE))),
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
        rosters = database.all_rosters()
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
        return page(request, "dashboard.html", session=sess, stats=stats,
                    recent_incidents=sorted(
                        incidents, key=lambda i: i["created_at"] or 0,
                        reverse=True)[:8],
                    recent_checks=checks[:8], rosters=rosters)

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

    @app.post("/incidents/{inc_id}/delete")
    def incident_delete(request: Request, inc_id: str):
        require(request)
        database.delete_incident(inc_id)
        return RedirectResponse("/incidents", status_code=303)

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
        rosters = database.all_rosters()
        roster = rosters[0]["players"] if rosters else []
        return page(request, "age_checks.html", session=sess, checks=rows,
                    verdict=verdict, q=q, roster=roster,
                    current=rosters[0] if rosters else None,
                    verdicts=agecheck.VERDICTS)

    @app.post("/age-checks")
    def age_check_create(request: Request, name: str = Form(...),
                         user_id: str = Form(""), verdict: str = Form(...),
                         reported_age: str = Form(""), note: str = Form(""),
                         world_name: str = Form(""), world_id: str = Form(""),
                         instance_id: str = Form("")):
        sess = require(request)
        if verdict not in agecheck.VERDICTS:
            return RedirectResponse("/age-checks", status_code=303)
        age = int(reported_age) if reported_age.strip().isdigit() else None
        agecheck.record(
            database, name=name.strip(), user_id=user_id.strip(),
            verdict=verdict, reported_age=age, note=note.strip(),
            world_name=world_name, world_id=world_id, instance_id=instance_id,
            checked_by=sess["name"], checked_by_id=sess["user_id"],
            source="web")
        return RedirectResponse("/age-checks", status_code=303)

    @app.post("/age-checks/{check_id}/delete")
    def age_check_delete(request: Request, check_id: str):
        require(request)
        database.delete_age_check(check_id)
        return RedirectResponse("/age-checks", status_code=303)

    # ---------------- screening ----------------
    @app.get("/screening", response_class=HTMLResponse)
    def screening(request: Request, show: str = "", q: str = ""):
        sess = require(request)
        rosters = database.all_rosters()
        current = rosters[0] if rosters else None
        cached = database.all_users()
        latest = agecheck.latest_by_user(database.all_age_checks())
        note_word = (cfg.get("note_filter") or "").strip().lower()
        rows = []
        for p in sorted(current["players"] if current else [],
                        key=lambda p: (p.get("name") or "").lower()):
            uid = p.get("user_id") or ""
            rec = cached.get(uid, {})
            note = rec.get("note", "")
            tagged = bool(note_word and note_word in note.lower())
            check = latest.get(uid)
            rows.append({
                "name": p.get("name", ""), "user_id": uid,
                "note": note, "tagged": tagged,
                "groups": rec.get("groups", []),
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
                    counts=counts, verdicts=agecheck.VERDICTS)

    @app.post("/screening/tag")
    def screening_tag(request: Request, user_id: str = Form(...),
                      name: str = Form("")):
        """Write the verification word into your VRChat note for this user."""
        sess = require(request)
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
        return RedirectResponse("/screening", status_code=303)

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
    def require_token(token: str | None) -> None:
        want = (cfg.get("sync_token") or "").strip()
        if not want:
            raise _ApiError(503, "sync API disabled: no sync_token configured")
        if not token or not secrets.compare_digest(token, want):
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
        if payload.get("client_id") and payload.get("roster"):
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
        require_token(x_sync_token)
        database.upsert_roster(payload.get("client_id", "default"),
                               payload.get("roster", {}),
                               payload.get("client_name", ""))
        return {"ok": True}

    @app.get("/api/state")
    def api_state(request: Request):
        """Polled by open pages to notice new records without a full reload."""
        if not session_of(request):
            return JSONResponse({"error": "signed out"}, status_code=401)
        return {"version": database.state_version()}

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "time": time.time()}

    return app


# ---------------- helpers ----------------
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
