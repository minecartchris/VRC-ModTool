"""A small status page for the mod suite, on its own port.

Deliberately separate from the mod tool: if the thing you are monitoring is
the thing serving the page, the page is useless exactly when you need it. This
is a stdlib HTTP server in its own process and its own systemd unit, so it
keeps answering while the tool is restarting, wedged, or refusing connections.

    python3 monitor_web.py --port 8090

It shows counts and never names anybody — no players, no moderators. There is
no sign-in on it, so it only gets to know things that would not matter if the
page were left open on a screen.

LAN only in practice: nothing forwards 8090, and it must stay out of the
Cloudflare tunnel. Bind to 127.0.0.1 with --host if you want it tighter still.
"""
import argparse
import html
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import collect

REFRESH = 10


def bar(percent: float) -> str:
    """A width and a colour; a number alone does not read as pressure."""
    percent = max(0.0, min(100.0, float(percent)))
    tone = "good" if percent < 70 else ("warn" if percent < 90 else "bad")
    return (f'<div class="bar"><span class="{tone}" '
            f'style="width:{percent:.1f}%"></span></div>')


def row(label: str, value: str, note: str = "") -> str:
    return (f'<div class="row"><span class="k">{html.escape(label)}</span>'
            f'<span class="v">{value}</span>'
            f'<span class="n">{html.escape(note)}</span></div>')


def page(data: dict) -> str:
    c, s, d = data["container"], data["service"], data["database"]
    hb, ht = collect.human_bytes, collect.human_time
    now = data["at"]

    ok = s["state"] == "active" and s["healthy"]
    banner = (f'<p class="banner {"ok" if ok else "bad"}">'
              f'{"The mod tool is up and answering." if ok else "The mod tool is not answering."}'
              f'</p>')

    box = []
    cpu = c["cpu_percent"]
    mem_pct = c["memory_percent"]
    load = "load %.2f %.2f %.2f" % tuple(c["load"])
    box.append("<section><h2>Container</h2>"
               + row("CPU", "%.1f%%" % cpu, "of %d cores" % c["cores"])
               + bar(cpu)
               + row("Memory",
                     "%s / %s" % (hb(c["memory_used"]), hb(c["memory_limit"])),
                     "%.1f%%" % mem_pct)
               + bar(mem_pct))
    for disk in c["disks"]:
        box.append(row("Disk " + disk["mount"],
                       "%s / %s" % (hb(disk["used"]), hb(disk["total"])),
                       "%.1f%%" % disk["percent"]) + bar(disk["percent"]))
    box.append(row("Uptime", ht(c["uptime"]), load))
    box.append("</section>")

    state_tone = "good" if s["state"] == "active" else "bad"
    state = '<b class="%s">%s/%s</b>' % (state_tone, s["state"], s["sub"])
    health = ("answers in %.0f ms" % s["health_ms"] if s["healthy"]
              else '<b class="bad">no answer</b>')
    share = ("%.0f%% of the container"
             % (s["memory"] / c["memory_limit"] * 100)) if c["memory_limit"] else ""
    box.append("<section><h2>Mod tool</h2>"
               + row("State", state, "pid %d" % s["pid"])
               + row("Health", health, "HTTP %s" % (s["health_code"] or "-"))
               + row("Running for", ht(s["running_for"]),
                     "%d restarts" % s["restarts"])
               + row("Memory", hb(s["memory"]), share)
               + "</section>")

    if not d.get("ok"):
        box.append('<section><h2>Database</h2>'
                   + row("Unreadable", html.escape(str(d.get("error", "?"))))
                   + '</section>')
    else:
        queue = ", ".join("%d %s" % (n, k)
                          for k, n in sorted(d["queue"].items())) or "empty"
        box.append("<section><h2>Records</h2>"
                   + row("Database", hb(d["size"]), "+%s wal" % hb(d["wal"]))
                   + row("Incidents", "{:,}".format(d["incidents"]),
                         "%d today" % d["incidents_24h"])
                   + row("Age checks", "{:,}".format(d["age_checks"]),
                         "%d today" % d["age_checks_24h"])
                   + row("Bans from the audit log",
                         "{:,}".format(d["audit_bans"]),
                         "read %s ago" % ht(now - d["audit_watermark"]))
                   + "</section>")
        starts = d.get("starts_24h", 0)
        unclean = d.get("unclean_24h", 0)
        restart_note = ("%d after an unclean stop" % unclean if unclean
                        else "all clean" if starts else "")
        rows_html = (row("Restarts", str(starts),
                         "in 24h - " + restart_note if restart_note else "in 24h")
                     + row("Last start", ht(now - d["last_start"]) + " ago"
                           if d.get("last_start") else "unknown", ""))
        events = "".join(
            "<li>%s &middot; %s%s</li>"
            % (time.strftime("%m-%d %H:%M", time.localtime(e["at"])),
               html.escape(e["event"]),
               " &middot; " + html.escape(e["detail"] or "") if e["detail"] else "")
            for e in d.get("events", [])[:6])
        box.append("<section><h2>Starts &amp; stops</h2>" + rows_html
                   + ('<ul class="errs" style="color:var(--dim)">%s</ul>' % events
                      if events else "")
                   + "</section>")
        box.append("<section><h2>Live</h2>"
                   + row("Agents reporting", str(d["agents_live"]),
                         "of %d known" % d["agents"])
                   + row("Signed in", str(d["sessions"]), "moderators")
                   + row("Awaiting a reason", str(d["pending_actions"]),
                         "kicks and warns")
                   + "</section>")
        # An hour with a non-empty queue and nothing sent is the shape the
        # invite backlog had for a day and a half. Worth saying in red.
        stalled = d["queue_total"] > 0 and (now - d["queue_last_done"]) > 3600
        last = ('<b class="%s">%s ago</b>'
                % ("bad" if stalled else "good", ht(now - d["queue_last_done"])))
        box.append("<section><h2>Ban &amp; invite queue</h2>"
                   + row("Waiting", "{:,}".format(d["queue_total"]), queue)
                   + row("Sent", "%d in the last hour" % d["queue_done_1h"],
                         "%d today" % d["queue_done_24h"])
                   + row("Last sent", last,
                         "queue is stalled" if stalled else "")
                   + "</section>")

    errors = ""
    if data["errors"]:
        items = "".join(f"<li>{html.escape(line[:200])}</li>" for line in data["errors"])
        errors = f'<section><h2>Recent errors</h2><ul class="errs">{items}</ul></section>'

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH}">
<title>Mod Suite status</title>
<style>
  :root {{ color-scheme: dark light;
    --bg:#0f1117; --card:#171a23; --line:#262b38; --text:#e7e9ee; --dim:#9aa3b2;
    --good:#3fb950; --warn:#d29922; --bad:#f85149; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--text);
    font:15px/1.5 ui-sans-serif,system-ui,'Segoe UI',sans-serif; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.08em;
    color:var(--dim); margin:0 0 12px; font-weight:600; }}
  .when {{ color:var(--dim); font-size:13px; margin:0 0 20px; }}
  .banner {{ padding:10px 14px; border-radius:8px; margin:0 0 20px; font-weight:600; }}
  .banner.ok {{ background:rgba(63,185,80,.12); color:var(--good); }}
  .banner.bad {{ background:rgba(248,81,73,.12); color:var(--bad); }}
  .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); }}
  section {{ background:var(--card); border:1px solid var(--line);
    border-radius:10px; padding:16px; }}
  .row {{ display:flex; align-items:baseline; gap:10px; padding:3px 0; }}
  .k {{ color:var(--dim); flex:1; }}
  .v {{ font-variant-numeric:tabular-nums; font-weight:600; }}
  .n {{ color:var(--dim); font-size:12px; min-width:0; }}
  .bar {{ height:5px; background:#0b0d13; border-radius:3px; margin:4px 0 10px;
    overflow:hidden; }}
  .bar span {{ display:block; height:100%; }}
  .good {{ background:var(--good); color:var(--good); }}
  .warn {{ background:var(--warn); color:var(--warn); }}
  .bad {{ background:var(--bad); color:var(--bad); }}
  b.good, b.bad {{ background:none; }}
  .errs {{ margin:0; padding-left:18px; color:var(--bad); font-size:12px;
    font-family:ui-monospace,Consolas,monospace; }}
  footer {{ color:var(--dim); font-size:12px; margin-top:20px; }}
</style></head><body>
<h1>Mod Suite status</h1>
<p class="when">{html.escape(c['hostname'])} &middot; {time.strftime('%H:%M:%S', time.localtime(now))}
   &middot; refreshes every {REFRESH}s</p>
{banner}
<div class="grid">{''.join(box)}</div>
{errors}
<footer>Counts only - this page names no players and no moderators, and has no
sign-in. <a href="/json" style="color:var(--dim)">/json</a></footer>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ModSuiteMonitor/1.0"

    def do_GET(self):
        try:
            if self.path.startswith("/json"):
                body = json.dumps(collect.everything(), indent=2).encode()
                kind = "application/json"
            elif self.path.startswith("/healthz"):
                body, kind = b'{"ok":true}', "application/json"
            elif self.path in ("/", "/index.html"):
                body = page(collect.everything()).encode("utf-8")
                kind = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
        except Exception as e:                       # never take the page down
            body = f"collecting failed: {e}".encode()
            kind = "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass                    # a status page logging every poll is just noise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="0.0.0.0",
                        help="0.0.0.0 for the LAN, 127.0.0.1 for this box only")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mod Suite status on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
