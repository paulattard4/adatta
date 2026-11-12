import type { Capability, Freshness } from "@/lib/types";
import { capLabel, dateLocal } from "@/lib/format";

/** Capability flags for the competition-season (§3.4); only the ones that shape markets. */
export function CapabilityBadge({ caps }: { caps: Capability[] }) {
  const shown = caps.filter((c) => capLabel(c.capability));
  if (!shown.length) return <span className="cap cap-unknown">capabilities not audited</span>;
  return (
    <span className="caps">
      {shown.map((c) => (
        <span key={c.capability} className={`cap cap-${c.status}`}
          title={`${capLabel(c.capability)}: ${c.status}${c.coverage != null ? ` (${Math.round(100 * c.coverage)}% coverage)` : ""}`}>
          {capLabel(c.capability)}
        </span>
      ))}
    </span>
  );
}

export function FreshnessBadge({ f }: { f: Freshness }) {
  if (!f.refit_as_of) return <span className="fresh s-missing">no passing refit</span>;
  return (
    <span className={`fresh ${f.stale ? "s-stale" : "s-ok"}`}
      title={`Refit model_run ${f.model_run_id}, ${f.gates_pass ? "inference gates pass" : "gates FAIL"}`}>
      {f.tier} refit · as of <span className="num">{dateLocal(f.refit_as_of)}</span> ·{" "}
      <span className="num">{f.age_days?.toFixed(1)}</span> d
    </span>
  );
}
