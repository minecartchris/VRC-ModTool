/* Keeps an open page current, the way the desktop app's roster is.
 *
 * Polls /api/state for a fingerprint of the record set and reloads when it
 * moves. The one rule that matters: never reload out from under someone who
 * is mid-entry. Screening rows contain an age box and a set of verdict
 * buttons, and silently wiping a typed age — or worse, reloading between the
 * typing and the click — would be a good way to file the wrong verdict on the
 * wrong player. When the page is busy we surface a pill instead and let the
 * moderator choose the moment.
 */
(function () {
  var meta = document.querySelector('meta[name="state-version"]');
  if (!meta || !meta.content) return;      // signed out, nothing to track

  var INTERVAL = 5000;
  var STORE_KEY = "modsuite.autorefresh";
  var known = meta.content;
  var paused = false;
  try {
    paused = localStorage.getItem(STORE_KEY) === "off";
  } catch (e) { /* private mode: default to live */ }

  var toggle = document.getElementById("live-toggle");
  var pill = document.getElementById("stale-pill");

  function paintToggle() {
    if (!toggle) return;
    toggle.textContent = paused ? "paused" : "live";
    toggle.classList.toggle("off", paused);
    toggle.title = paused
      ? "Auto-refresh is off - click to resume"
      : "Auto-refresh is on - click to pause";
  }

  /* True when a reload would destroy something the user is working on. */
  function busy() {
    var el = document.activeElement;
    if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return true;
    var fields = document.querySelectorAll("input, textarea");
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (f.type === "hidden" || f.type === "submit") continue;
      if (f.value && f.value !== f.defaultValue) return true;
    }
    return false;
  }

  function showPill() {
    if (pill) pill.hidden = false;
  }

  /* Pages showing one instance ask only about that one, so a moderator
     agenting a different world does not reload this page on every join.
     Every agent in the same room shares the scope, so a colleague's report of
     your own instance still refreshes you. */
  var instance = meta.getAttribute("data-instance") || "";
  var stateUrl = "/api/state" +
    (instance ? "?instance=" + encodeURIComponent(instance) : "");

  function tick() {
    if (paused || document.hidden) return;
    fetch(stateUrl, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; return null; }
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data || data.version === known) return;
        if (busy()) showPill(); else location.reload();
      })
      .catch(function () { /* server down: keep showing what we have */ });
  }

  /* A second click on Submit is never a second kick. The server files one
     log either way, but a button that keeps looking clickable invites the
     click that made people doubt it. */
  document.querySelectorAll("form[data-once]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var btn = form.querySelector('button[type="submit"]');
      // After the event, so the submission itself still goes out.
      if (btn) setTimeout(function () { btn.disabled = true; }, 0);
    });
  });

  if (toggle) {
    toggle.addEventListener("click", function () {
      paused = !paused;
      try { localStorage.setItem(STORE_KEY, paused ? "off" : "on"); } catch (e) {}
      paintToggle();
      if (!paused) tick();
    });
    paintToggle();
  }
  if (pill) {
    pill.addEventListener("click", function () { location.reload(); });
  }

  setInterval(tick, INTERVAL);
  // Coming back to the tab should feel instant, not up to INTERVAL stale.
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();
  });
})();
