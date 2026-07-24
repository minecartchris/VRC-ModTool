# Web app + sync server

A browser front end for the parts of the mod suite that aren't tied to your
gaming PC: **incident logs** and **age checks**. The desktop app keeps doing the
things only it can do — listening to voice chat, pressing the Medal hotkey,
screenshotting VRChat, reading the local log — and syncs its records here.

Sign-in is your own VRChat account, and access is decided by **staff-group
membership**: if VRChat says you're in the configured group, you're a
moderator here. Remove someone from the group and they lose access.

## Quick start (same PC as the desktop app)

```bash
python run_web.py --init
```

That writes `web_config.json` with a generated `sync_token`. Edit it and set:

| Key | What to put |
|---|---|
| `staff_group` | your moderator group's `grp_` ID, short code, or part of its name |
| `vrc_contact` | a real email or Discord handle — VRChat's API 403s without it |

Then start it:

```bash
python run_web.py
```

Open <http://127.0.0.1:8787> and sign in with your VRChat account (2FA
supported). It reads and writes the same `modtool.db` as the desktop app, so
your existing incidents are there immediately — no import step.

## Connecting the desktop app

On the desktop **Settings** tab, under *Server sync*:

1. tick **Sync with server**
2. **Server URL** — `http://127.0.0.1:8787`, or wherever you host it
3. **Token** — the `sync_token` from `web_config.json`
4. **Save settings** (or **Sync now** for a one-shot round)

The status line shows `Synced HH:MM:SS — N up, M down`. The desktop also
publishes its current world and roster, which is what fills the web
**Screening** page.

Running the server on the same machine as the desktop app? Both processes open
the same SQLite file in WAL mode, so they already share everything and sync is
optional. It matters when the server lives elsewhere, or when a second
moderator's PC files incidents into the same set.

## Hosting it for a team

```bash
pip install -r requirements-web.txt   # no audio stack; installs on Linux
python run_web.py --host 0.0.0.0
```

- **Put it behind HTTPS.** Sign-in sends VRChat credentials to your server,
  which forwards them to VRChat. Over plain HTTP on a network you don't control
  that is a credential leak. Terminate TLS with Caddy/nginx and set
  `"https_only": true` so the session cookie is marked Secure.
- Move the database with `MODTOOL_DB=/var/lib/modsuite/modtool.db`.
- Clips and screenshots stay on the PC that captured them. The server only
  serves media under `incident_shots/` plus anything you list in
  `media_roots` — paths outside those are refused, because incident records
  arrive over the sync API and must not be able to name arbitrary files.

## What the server stores

| Table | Contents |
|---|---|
| `incidents` | one row per trigger or manual filing; transcript, roster, world, clip/screenshot paths, status, notes |
| `age_checks` | one row per verdict: player, over/under/in-range, reported age, who checked, which incident it filed |
| `screening_users` | cached VRChat note + groups per user, so nobody is looked up twice |
| `rosters` | last instance snapshot each desktop client reported |
| `web_sessions` | opaque session tokens; **no passwords, no VRChat cookies** |

Your VRChat auth cookie lives in server memory only, for as long as the process
runs. Restart the server and you stay signed in for browsing records, but
writing VRChat notes needs a fresh sign-in.

## How sync works

Both sides run the same `db.py`. Every record carries `updated_at` and a
`deleted` tombstone, and `upsert_*` bumps `updated_at` **only when content
actually changes**. That single rule is what makes the loop terminate: a record
pulled from the server and written locally is byte-identical, so it never
re-enters the outbound queue.

Each round is pull-then-push against a per-peer cursor in `sync_state`.
Conflicts are last-write-wins, which is safe in practice because the two sides
edit different fields — the desktop writes new incidents and clip paths, the
web writes statuses, notes and age checks.

## Routes

| Route | Purpose |
|---|---|
| `/` | counts, live instance, recent activity |
| `/incidents`, `/incidents/{id}` | list/search, detail with evidence, notes, status, paste-ready report |
| `/age-checks` | log + record a check (over/under also files an incident) |
| `/screening` | live roster with per-player verdict buttons and VRChat note tagging |
| `/api/sync/push`, `/api/sync/pull`, `/api/sync/roster` | desktop sync; `X-Sync-Token` header |
| `/healthz` | liveness |

## Security notes

- Login is rate-limited per IP (8 attempts / 5 min) — this endpoint proxies to
  VRChat, so it must not become a brute-force tool.
- Session cookies are `HttpOnly` + `SameSite=Lax`, which is what blocks
  cross-site form posts; there is no separate CSRF token.
- `sync_token` grants full read/write to every record. Treat it as a password,
  and don't reuse it as the staff group.
- Age-check records are notes about real people, including minors. Keep the
  server private and the database off shared drives.
