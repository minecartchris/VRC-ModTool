"""Minimal VRChat web-API client (login, 2FA, user lookup).

You log in with your own VRChat account from the Settings tab; the auth
cookie is stored locally in vrc_cookies.txt next to this file, so you only
need to re-enter credentials when the cookie expires. Credentials themselves
are never written to disk.

VRChat asks API clients to identify themselves with a descriptive User-Agent.
"""

import base64
import urllib.parse
from http.cookiejar import LWPCookieJar
from pathlib import Path

import requests

from paths import HERE

API = "https://api.vrchat.cloud/api/1"
APP_NAME = "VRC-ModTool"
APP_VERSION = "0.1"
COOKIE_PATH = HERE / "vrc_cookies.txt"


def build_user_agent(contact: str) -> str:
    """VRChat's WAF (403 / waf_code 13799) blocks requests unless the
    User-Agent names the app, a version, and REAL contact info — placeholders
    like 'example.com' are rejected. `contact` should be your own email/handle,
    kept in local config so it never lands in the public repo."""
    return f"{APP_NAME}/{APP_VERSION} {(contact or '').strip()}".strip()


def is_valid_contact(contact: str) -> bool:
    c = (contact or "").strip().lower()
    return bool(c) and "example.com" not in c and "your-contact" not in c


class VRChatAPIError(RuntimeError):
    pass


class VRChatAPI:
    """`cookie_path=None` keeps the auth cookie in memory only.

    The desktop app passes a path so you stay logged in between runs. The web
    server passes None: it holds one of these per signed-in moderator for the
    life of the process, so nobody else's VRChat session is ever written to
    disk on a shared machine.
    """

    def __init__(self, cookie_path: Path | None = COOKIE_PATH,
                 contact: str = ""):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = build_user_agent(contact)
        if cookie_path is not None:
            jar = LWPCookieJar(str(cookie_path))
            try:
                jar.load(ignore_discard=True)
            except OSError:
                pass
            self.s.cookies = jar
        self.user: dict | None = None

    def _save_cookies(self) -> None:
        if not isinstance(self.s.cookies, LWPCookieJar):
            return          # in-memory jar: nothing to persist
        try:
            self.s.cookies.save(ignore_discard=True)
        except OSError:
            pass

    # ---------------- auth ----------------
    def check_session(self) -> dict | None:
        """Return the logged-in user if the stored cookie is still valid."""
        r = self.s.get(f"{API}/auth/user", timeout=15)
        if r.ok and "requiresTwoFactorAuth" not in r.json():
            self.user = r.json()
            return self.user
        return None

    def login(self, username: str, password: str) -> str:
        """Start a login. Returns 'ok' or a required 2FA method
        ('totp' / 'emailotp')."""
        cred = (urllib.parse.quote(username, safe="")
                + ":" + urllib.parse.quote(password, safe=""))
        auth = base64.b64encode(cred.encode()).decode()
        r = self.s.get(f"{API}/auth/user",
                       headers={"Authorization": f"Basic {auth}"}, timeout=15)
        if r.status_code == 401:
            raise VRChatAPIError("Login failed: wrong username or password.")
        r.raise_for_status()
        j = r.json()
        self._save_cookies()
        methods = j.get("requiresTwoFactorAuth")
        if methods:
            if any(m.lower() == "totp" for m in methods):
                return "totp"
            return "emailotp"
        self.user = j
        return "ok"

    def verify_2fa(self, code: str, method: str) -> dict:
        r = self.s.post(f"{API}/auth/twofactorauth/{method}/verify",
                        json={"code": code.strip()}, timeout=15)
        if not r.ok:
            raise VRChatAPIError("2FA code rejected.")
        self._save_cookies()
        user = self.check_session()
        if not user:
            raise VRChatAPIError("2FA verified but session check failed.")
        return user

    def logout(self) -> None:
        try:
            self.s.put(f"{API}/logout", timeout=15)
        except requests.RequestException:
            pass
        self.s.cookies.clear()
        self._save_cookies()
        self.user = None

    # ---------------- lookups ----------------
    def search_users(self, query: str, n: int = 10) -> list[dict]:
        r = self.s.get(f"{API}/users",
                       params={"search": query, "n": n}, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_user(self, user_id: str) -> dict:
        r = self.s.get(f"{API}/users/{user_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    # ---------------- notes & groups (moderation) ----------------
    def list_user_notes(self) -> list[dict]:
        """Every note you've written, as {targetUserId, note, ...} objects.

        One call instead of one-per-user; we build a targetUserId->note map
        from it to tell which players are already tagged. The single-note
        variant is GET /userNotes/{userNoteId} (get_user_note)."""
        r = self.s.get(f"{API}/userNotes", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_user_note(self, note_id: str) -> dict:
        r = self.s.get(f"{API}/userNotes/{note_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_user_groups(self, user_id: str) -> list[dict]:
        """Groups a user belongs to (each has name, groupId, shortCode, ...)."""
        r = self.s.get(f"{API}/users/{user_id}/groups", timeout=15)
        r.raise_for_status()
        return r.json()

    def update_user_note(self, target_user_id: str, note: str) -> dict:
        """Set (replace) your private note on a user. Read the existing note
        first and merge if you want to append rather than overwrite."""
        r = self.s.post(f"{API}/userNotes",
                        json={"targetUserId": target_user_id, "note": note},
                        timeout=15)
        r.raise_for_status()
        return r.json()
