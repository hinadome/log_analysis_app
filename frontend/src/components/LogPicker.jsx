import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { cx, fmtBytes, fmtDate } from "../fmt.js";
import { Modal, StepHead } from "./ui.jsx";

export default function LogPicker({ logs, value, onSelect, fanout, onToggleFanout, logsDir, onUploadFiles }) {
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState(null);
  const [tail, setTail] = useState(false);
  const [loading, setLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [customPath, setCustomPath] = useState("");
  const fileInput = useRef(null);
  const rowSelected = useRef(null); // last log picked by clicking a row

  // Keep the restore point in sync with any non-custom selection — a row
  // click, or the auto-select after an upload (App-level state change).
  useEffect(() => {
    if (value && !value.custom) rowSelected.current = value;
  }, [value]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? logs.filter((l) => l.name.toLowerCase().includes(q)) : logs;
  }, [logs, query]);

  const openPreview = async (log, fromEnd = tail) => {
    setLoading(true);
    try {
      const data = await api.previewLog(log.path, 200, fromEnd);
      setPreview({ ...data, size: log.size ?? data.size });
    } catch (e) {
      setPreview({ name: log.name, lines: [String(e.message || e)], error: true });
    } finally {
      setLoading(false);
    }
  };

  // Typing a path selects it like clicking a row would: a synthetic log
  // whose name is the full path (what the job will carry as log_name).
  // Clearing the box restores the last row selection.
  const applyCustomPath = (v) => {
    setCustomPath(v);
    const t = v.trim();
    if (t) {
      onSelect({ name: t, path: t, custom: true });
    } else {
      onSelect(rowSelected.current);
    }
  };

  const handleFiles = (files) => {
    if (files?.length) onUploadFiles(files);
  };

  return (
    <section className="card step">
      <StepHead n={1} title="Log file" hint={logs.length ? `${logs.length} file(s) · ${logsDir}` : `No logs found in ${logsDir}`} />
      <input
        className="search"
        placeholder="Filter log files…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="custom-log">
        <input
          className="mono"
          spellCheck="false"
          autoComplete="off"
          placeholder="…or type a path to any file on this machine — e.g. /var/log/system.log"
          title="Overrides the selection above while filled. Absolute paths reach outside the logs directory; a relative name resolves inside it."
          value={customPath}
          onChange={(e) => applyCustomPath(e.target.value)}
        />
        <button
          className="btn ghost sm"
          disabled={!customPath.trim() || loading}
          title="Check the typed path exists and peek at its first lines"
          onClick={() => openPreview({ name: customPath.trim(), path: customPath.trim() })}
        >
          Preview
        </button>
      </div>
      <div
        className={cx("dropzone", dragging && "over")}
        onClick={() => fileInput.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          handleFiles(e.dataTransfer?.files);
        }}
        title="Uploaded files land in the server's logs directory"
      >
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          onChange={(e) => { handleFiles(e.target.files); e.target.value = ""; }}
        />
        ⬆ Drop log file{""}
        <span className="muted">s here, or click to browse — they're saved to {logsDir}</span>
      </div>
      <div className="log-list">
        {filtered.length === 0 && (
          <div className="empty">No log files match. Put files in the server's logs/ directory.</div>
        )}
        {filtered.map((log) => {
          const isFan = fanout?.some((l) => l.path === log.path);
          return (
            <div
              key={log.path}
              className={cx("log-row", { selected: value?.path === log.path, fan: isFan })}
              onClick={() => { rowSelected.current = log; setCustomPath(""); onSelect(log); }}
            >
              <span className={cx("radio", { on: value?.path === log.path })} />
              <span className="mono log-name">{log.name}</span>
              <span className="muted">{fmtBytes(log.size)}</span>
              <span className="muted hide-sm">{fmtDate(log.modified)}</span>
              <button
                className={cx("btn ghost sm fanout-btn", isFan && "on")}
                title={isFan
                  ? "Remove from the fan-out — this log won't get the submission"
                  : "Fan out: also run the submission on this log"}
                onClick={(e) => { e.stopPropagation(); onToggleFanout(log); }}
              >
                {isFan ? "✓" : "＋"}
              </button>
              <button
                className="btn ghost sm"
                disabled={loading}
                onClick={(e) => { e.stopPropagation(); openPreview(log); }}
              >
                Preview
              </button>
            </div>
          );
        })}
      </div>

      {preview && (
        <Modal title={preview.name} onClose={() => setPreview(null)}>
          <div className="preview-meta muted">
            {fmtBytes(preview.size)}
            {preview.truncated && (tail ? " · earlier lines not shown" : " · truncated preview")}
          </div>
          <div className="preview-toolbar">
            <button
              className={cx("btn ghost sm", !tail && "active")}
              disabled={loading || preview.error}
              onClick={() => { setTail(false); openPreview({ name: preview.name, path: preview.path, size: preview.size }, false); }}
            >
              ⇡ Head
            </button>
            <button
              className={cx("btn ghost sm", tail && "active")}
              disabled={loading || preview.error}
              onClick={() => { setTail(true); openPreview({ name: preview.name, path: preview.path, size: preview.size }, true); }}
            >
              ⇣ Tail
            </button>
          </div>
          <pre className="output log-preview">
            {preview.lines.join("\n")}
          </pre>
        </Modal>
      )}
    </section>
  );
}
