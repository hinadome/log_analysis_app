import { useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { api } from "../api.js";

/**
 * The remote terminal tab: a real shell on the server, in the browser.
 *
 * One pty session per mount — POST /sessions to create it, an EventSource
 * for output (base64 JSON frames, same cookie auth as every other call),
 * term.onData POSTs keystrokes verbatim, and unmount (or the tab closing)
 * kills the session. The whole tab only renders while health.terminal
 * says LOGSMITH_TERMINAL is on.
 */
export default function TerminalPanel() {
  const holder = useRef(null);
  const [resetKey, setResetKey] = useState(0);
  const [status, setStatus] = useState("connecting"); // connecting | live | ended | error
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    let term = null;
    let es = null;
    let session = null;
    let resizeTimer = null;
    let observer = null;
    let ended = false; // effect-local: `status` state is stale in these closures

    const endSession = () => {
      ended = true;
      setStatus("ended");
      es?.close();
    };

    (async () => {
      try {
        session = await api.terminalCreate();
        if (!alive) return; // unmounted while creating

        term = new Terminal({
          fontSize: 13,
          cursorBlink: true,
          scrollback: 5000,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          theme: { background: "#0c0d10" },
        });
        const fit = new FitAddon();
        term.loadAddon(fit);
        term.open(holder.current);
        try { fit.fit(); } catch { /* not measurable yet — the observer will */ }

        es = new EventSource(api.terminalOutputUrl(session.id));
        es.onmessage = (ev) => {
          const msg = JSON.parse(ev.data);
          if (msg.type === "output") {
            // base64 → bytes (a straight string decode mangles UTF-8)
            const bytes = Uint8Array.from(atob(msg.data), (c) => c.charCodeAt(0));
            term?.write(bytes);
          } else if (msg.type === "exit") {
            if (term) term.write("\r\n\x1b[90m— session ended —\x1b[0m\r\n");
            endSession();
          }
        };

        term.onData((data) => {
          if (!session || ended) return;
          api.terminalInput(session.id, data).catch(() => { /* shell gone mid-keystroke */ });
        });

        // size: fit locally, tell the pty (debounced — window drags fire often)
        const pushSize = () => {
          if (!term || !session || !alive) return;
          api.terminalResize(session.id, term.cols, term.rows).catch(() => {});
        };
        term.onResize(() => {
          clearTimeout(resizeTimer);
          resizeTimer = setTimeout(pushSize, 150);
        });
        pushSize();

        observer = new ResizeObserver(() => {
          try { fit.fit(); } catch { /* holder hidden — next resize refits */ }
        });
        observer.observe(holder.current);

        setStatus("live");
        term.focus();
      } catch (e) {
        if (!alive) return;
        setError(String(e.message || e));
        setStatus("error");
      }
    })();

    return () => {
      alive = false;
      clearTimeout(resizeTimer);
      observer?.disconnect();
      es?.close();
      if (session) api.terminalKill(session.id).catch(() => {});
      term?.dispose();
    };
    // status is deliberately NOT a dependency: the effect owns the session
    // lifecycle and must not re-run when the badge flips to "live"
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  return (
    <section className="card terminal-panel">
      <div className="terminal-head">
        <h2>
          ⌨ Terminal <span className="muted">— a shell on the server, as the server user</span>
        </h2>
        <span className={`badge ${status === "live" ? "ok" : status === "error" ? "bad" : ""}`}>
          {status === "live" ? "connected" : status}
        </span>
        {status === "ended" && (
          <button className="btn sm" onClick={() => { setStatus("connecting"); setResetKey((k) => k + 1); }}>
            Start a new session
          </button>
        )}
      </div>
      <p className="chip warn terminal-warn">
        Everything typed here runs on the server as the LogSmith process user —
        the same trust as the script editor, without the vetting. Server-side feature:
        LOGSMITH_TERMINAL=1 enables it.
      </p>
      {status === "error" && <p className="chip bad">{error}</p>}
      <div className={`terminal-hold ${status === "connecting" ? "connecting" : ""}`}
           ref={holder} onClick={() => termFocus(holder)} />
    </section>
  );
}

function termFocus(holder) {
  holder.current?.querySelector("textarea")?.focus();
}
