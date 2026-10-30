import { useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { cx, fmtBytes, fmtDate } from "../fmt.js";
import { Modal, StepHead } from "./ui.jsx";

export default function LogPicker({ logs, value, onSelect, logsDir, onUploadFiles }) {
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState(null);
  const [tail, setTail] = useState(false);
  const [loading, setLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? logs.filter((l) => l.name.toLowerCase().includes(q)) : logs;
  }, [logs, query]);

  const openPreview = async (log, fromEnd = tail) => {
    setLoading(true);
    try {
      const data = await api.previewLog(log.path, 200, fromEnd);
      setPreview({ ...data, size: log.size });
    } catch (e) {
      setPreview({ name: log.name, lines: [String(e.message || e)], error: true });
    } finally {
      setLoading(false);
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
        {filtered.map((log) => (
          <div
            key={log.path}
            className={cx("log-row", { selected: value?.path === log.path })}
            onClick={() => onSelect(log)}
          >
            <span className={cx("radio", { on: value?.path === log.path })} />
            <span className="mono log-name">{log.name}</span>
            <span className="muted">{fmtBytes(log.size)}</span>
            <span className="muted hide-sm">{fmtDate(log.modified)}</span>
            <button
              className="btn ghost sm"
              disabled={loading}
              onClick={(e) => { e.stopPropagation(); openPreview(log); }}
            >
              Preview
            </button>
          </div>
        ))}
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
