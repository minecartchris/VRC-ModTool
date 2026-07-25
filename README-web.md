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

## Code changes and staying signed in

The server watches its own source and restarts when a `.py`, `.html`, `.css`
or `.js` file changes, so edits apply without you touching the terminal.
Templates were always hot; Python is now too.

**A restart no longer signs anyone out.** The session survives, and so does the
live VRChat connection behind it — no "no live VRChat connection" banner, no
re-entering credentials and 2FA to tag one note.

That works because the VRChat auth cookie is stored, encrypted, rather than
held in memory. The scheme:

| In the database | Only in your browser |
|---|---|
| `sha256(session token)` | the session token itself |
| VRChat cookie encrypted with a key derived from the token | — |

So `modtool.db` on its own decrypts nothing. Someone who copies the file gets
neither a usable session nor anybody's VRChat login; they would also need the
cookie out of a signed-in browser. This is stronger than what the desktop app
does, which keeps its cookie in plain text in `vrc_cookies.txt`.

Turn the watcher off for a hosted deployment, where it is just overhead:

```json
"auto_reload": false
```

or run `python run_web.py --no-reload`. Note the watcher runs the app in a
child process, so if you kill the server from a script rather than Ctrl+C,
kill the whole process tree or the child keeps holding the port.

## Look and feel

The UI follows the Teen Chillout Mod console (`team-chillo-mod-tool`): the same
Material 3 dark palette, the 240px navigation rail, the 64px top bar and the
same card, chip and metric-tile shapes. Tokens are copied from that project's
`globals.css`, so changing one theme and porting the values keeps the two
tools looking like one suite.

Two deliberate differences: fonts and icons are **not** fetched from Google.
This server handles records about minors on a LAN that may have no internet,
so it makes no third-party requests — icons are inline SVG
(`templates/_icons.html`) and the type stack falls back to the system UI font.
An icon *font* that fails to load renders its ligature as raw text, which
would leave the nav reading "dashboard flag group" in words.

## Who is a moderator

Two places show it:

- **The signed-in user** carries a `MOD` badge in the top bar. Everyone who can
  sign in has passed the staff-group check, so it is always earned; hover it to
  see which group granted access.
- **Players in the roster** get a `MOD` badge on the Screening page when they
  are in the staff group, using the same match as sign-in. So the badge means
  exactly "this person could log into this tool" — handy for not wasting a
  screening pass on a colleague, and for seeing at a glance whether any staff
  are present in a bad instance.

## Live updates

Pages refresh themselves, so an open browser tracks the instance the way the
desktop app does. Every few seconds the page asks `/api/state` for a
fingerprint of the record set and reloads if it moved — someone joining the
instance, another moderator filing a check, the listener catching a trigger.

Two things keep that from being annoying:

- **It never reloads while you're mid-entry.** If any field has something typed
  in it — a half-entered age, an unsaved note — the page shows a *New activity*
  pill at the bottom instead and waits for you to click it. Reloading between
  someone typing an age and clicking a verdict button is how you file the wrong
  verdict on the wrong player.
- **Background tabs don't poll**, and a tab refreshes the moment you switch back
  to it.

The **live / paused** button in the header turns it off; the choice is
remembered in that browser.

## Filtering the roster

The Screening page has a chip for each state, with counts:

| Chip | Who it shows |
|---|---|
| Everyone | the whole instance |
| **Not verified** | **anyone not cleared — unchecked, under range, or over range** |
| Verified | cleared by an in-range check, or already carrying the note tag |
| Unchecked | nobody has looked at them yet |
| Under range / Over range | their latest verdict |

*Not verified* is deliberately broader than *Unchecked*: a player marked under
or over range has been looked at but is not cleared, and folding them in with
the verified crowd is how people get missed. Unverified rows also carry a red
edge marker in the full list. Chips with a count of zero are hidden.

A player counts as verified two ways — an in-range check recorded here, or the
verification word already in your VRChat note. That second path is what the
desktop app has always used, including for auto-verified staff-group members,
so the two front ends agree on who is cleared.

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
| `/screening` | live roster, filterable by verification state, with per-player verdict buttons and VRChat note tagging |
| `/api/state` | change fingerprint polled by open pages |
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
