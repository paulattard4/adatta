import type { Component } from "@/lib/types";

const STATUS_TEXT: Record<string, string> = {
  ok: "ok", limited: "limited", stale: "stale", missing: "missing", not_modelled: "not modelled",
};

/** The named confidence components of §13.2, never collapsed into a single score. */
export function ConfidenceBadge({ components }: { components: Component[] }) {
  return (
    <span className="conf-badge" aria-label="Confidence components">
      {components.map((c) => (
        <span key={c.key} className={`dot s-${c.status}`} title={`${c.label}: ${c.value} (${STATUS_TEXT[c.status]})\n${c.detail}`} />
      ))}
    </span>
  );
}

export function ConfidencePanel({ components }: { components: Component[] }) {
  return (
    <div className="conf-grid">
      {components.map((c) => (
        <div key={c.key} className={`conf-item b-${c.status}`}>
          <div className="conf-head">
            <span className={`dot s-${c.status}`} />
            <span className="conf-label">{c.label}</span>
            <span className={`tag s-${c.status}`}>{STATUS_TEXT[c.status]}</span>
          </div>
          <div className="num conf-value">{c.value}</div>
          <div className="conf-detail">{c.detail}</div>
        </div>
      ))}
    </div>
  );
}
