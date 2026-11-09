/* ScriptEngine python-UI terminal client.
 *
 * Progressive enhancement for /terminal: when this script (plus the
 * vendored xterm.js) loads, the page's interactive shell mounts and the
 * no-JS command box is hidden. Without JavaScript the box stays — the
 * page never *requires* JS, this feature does.
 *
 * Same protocol as the React tab: POST /api/terminal/sessions to create,
 * an EventSource for output (base64 JSON frames, one exit frame at the
 * end), keystrokes POSTed verbatim, a debounced resize POST, and DELETE
 * on leave. Cookies ride along (same origin), exactly like every other
 * call the python UI's browser makes.
 */
(function () {
  "use strict";

  var mount = document.getElementById("term-mount");
  if (!mount || !window.Terminal || !window.FitAddon) {
    return; // no xterm (or no JS): the command box is the page
  }

  var statusEl = document.getElementById("term-status");
  var restartBtn = document.getElementById("term-restart");
  var fallback = document.getElementById("term-fallback");
  if (fallback) fallback.hidden = true; // it stays for no-JS visitors

  var term = null;
  var es = null;
  var sessionId = null;
  var ended = false;
  var resizeTimer = null;
  var observer = null;

  function sessionUrl(suffix) {
    return "/api/terminal/sessions/" + sessionId + (suffix || "");
  }

  function setStatus(text, cls) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = "badge" + (cls ? " " + cls : "");
  }

  function b64bytes(b64) {
    var bin = atob(b64);
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  /* POST helper: raw body, optional content type; a 401 means the session
     cookie expired — the python UI convention is to bounce to /login. */
  function post(path, body, type) {
    return fetch(path, {
      method: "POST",
      headers: type ? { "Content-Type": type } : undefined,
      body: body,
    }).then(function (r) {
      if (r.status === 401) {
        window.location.href = "/login?next=" + encodeURIComponent("/terminal");
        throw new Error("login required");
      }
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          throw new Error(b.detail || (r.status + " " + r.statusText));
        });
      }
      return r;
    });
  }

  function teardown() {
    ended = true;
    if (es) es.close();
    if (observer) observer.disconnect();
    if (resizeTimer) clearTimeout(resizeTimer);
    if (term) { try { term.dispose(); } catch (e) { /* already gone */ } }
    if (sessionId) {
      fetch(sessionUrl(), { method: "DELETE", keepalive: true }).catch(function () {});
    }
    term = null;
    es = null;
    sessionId = null;
  }

  function start() {
    ended = false;
    if (restartBtn) restartBtn.hidden = true;
    setStatus("connecting…");
    fetch("/api/terminal/sessions", { method: "POST" })
      .then(function (r) {
        if (r.status === 401) {
          window.location.href = "/login?next=" + encodeURIComponent("/terminal");
          throw new Error("login required");
        }
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (b) {
            throw new Error(b.detail || (r.status + " " + r.statusText));
          });
        }
        return r.json();
      })
      .then(function (s) { sessionId = s.id; mountTerm(); })
      .catch(function (e) {
        // the shell could not start (flag flipped, session cap, server
        // hiccup): say why where the shell would have been, and give
        // the command box back
        setStatus("error", "bad");
        if (statusEl) statusEl.title = String(e.message || e);
        if (mount) {
          var p = document.createElement("p");
          p.className = "chip warn";
          p.style.margin = "0";
          p.textContent = String(e.message || e);
          mount.appendChild(p);
        }
        if (fallback) fallback.hidden = false;
      });
  }

  function mountTerm() {
    term = new window.Terminal({
      fontSize: 13,
      cursorBlink: true,
      scrollback: 5000,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      theme: { background: "#0c0d10" },
    });
    var fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(mount);
    try { fit.fit(); } catch (e) { /* not measurable yet — the observer will */ }

    es = new EventSource(sessionUrl("/output"));
    var errCount = 0; // consecutive failed (re)connects
    es.onopen = function () {
      // fires on every (re)connect — the badge must recover, not stick
      errCount = 0;
      if (!ended) setStatus("connected", "ok");
    };
    es.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "output") {
        term.write(b64bytes(msg.data));
      } else if (msg.type === "exit") {
        ended = true;
        es.close();
        term.write("\r\n\x1b[90m— session ended —\x1b[0m\r\n");
        setStatus("ended", "bad");
        if (restartBtn) restartBtn.hidden = false;
      }
    };
    es.onerror = function () {
      // EventSource retries by itself; the server resumes the session's
      // queue where it left off. Only the badge says something happened.
      if (ended) return;
      // A server restart (or anything else that outlives retries) means
      // the session is gone for good — stop retrying, offer a new one.
      if (++errCount >= 6) {
        ended = true;
        es.close();
        term.write("\r\n\x1b[90m— connection lost —\x1b[0m\r\n");
        setStatus("ended", "bad");
        if (restartBtn) restartBtn.hidden = false;
        return;
      }
      setStatus("reconnecting…", "");
    };

    term.onData(function (data) {
      if (ended || !sessionId) return;
      post(sessionUrl("/input"), data, "text/plain;charset=UTF-8")
        .catch(function () { /* shell gone mid-keystroke */ });
    });

    function pushSize() {
      if (!sessionId || !term) return;
      post(sessionUrl("/size"), JSON.stringify({ cols: term.cols, rows: term.rows }),
           "application/json").catch(function () {});
    }
    term.onResize(function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(pushSize, 150); // window drags fire often
    });
    pushSize();

    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(function () {
        try { fit.fit(); } catch (e) { /* hidden — next resize refits */ }
      });
      observer.observe(mount);
    }

    setStatus("connected", "ok");
    term.focus();
  }

  if (restartBtn) {
    restartBtn.addEventListener("click", function () {
      teardown();
      // a fresh mount element each time: xterm disposes the old one
      var fresh = mount.cloneNode(false);
      mount.parentNode.replaceChild(fresh, mount);
      mount = fresh;
      start();
    });
  }

  window.addEventListener("beforeunload", function () {
    if (sessionId) {
      fetch(sessionUrl(), { method: "DELETE", keepalive: true }).catch(function () {});
    }
  });

  start();
})();
