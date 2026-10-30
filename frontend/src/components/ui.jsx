export function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

export function StatusBadge({ status }) {
  return (
    <span className={`badge ${status}`}>
      <span className="dot" />
      {status}
    </span>
  );
}

export function Modal({ title, onClose, children, wide }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className={cx("modal", wide && "wide")} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="mono">{title}</span>
          <button className="btn ghost sm" onClick={onClose}>Close ✕</button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function StepHead({ n, title, hint, action }) {
  return (
    <div className="step-head">
      <span className="step-num">{n}</span>
      <span className="step-title">{title}</span>
      {hint && <span className="step-hint">{hint}</span>}
      {action && <span className="head-action">{action}</span>}
    </div>
  );
}

/** Render stdout: pretty-prints when it is valid JSON, plain text otherwise. */
export function OutputView({ text, className }) {
  const trimmed = (text || "").trim();
  let parsed = null;
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      /* plain text */
    }
  }
  const content = parsed ? JSON.stringify(parsed, null, 2) : trimmed;
  return <pre className={cx("output", className)}>{content || "(no output)"}</pre>;
}
