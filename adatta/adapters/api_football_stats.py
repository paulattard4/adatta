"""API-Football /fixtures?ids `statistics` block -> per-team stat rows (Phase 1b, D8)."""

from __future__ import annotations

from typing import Any

COUNTS = {
    "Total Shots": "shots_total",
    "Shots on Goal": "shots_on_target",
    "Shots insidebox": "shots_inside_box",
    "Corner Kicks": "corners",
    "Fouls": "fouls",
    "Yellow Cards": "yellow_cards",
    "Red Cards": "red_cards",
}


def _float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).rstrip("%"))
    except ValueError:
        return None


def team_stats(fx: dict[str, Any]) -> list[dict[str, Any]]:
    """One dict per team with a statistics block: team_ref, the typed columns and `stats`
    (every provider type -> raw value). A listed count with a null value is 0 (the provider
    omits zero counts); an unlisted count is None. xG null stays None."""
    out = []
    for block in fx.get("statistics") or []:
        entries = block.get("statistics") or []
        if not entries:
            continue
        raw = {e["type"]: e.get("value") for e in entries if e.get("type")}
        row: dict[str, Any] = {"team_ref": str(block["team"]["id"]), "stats": raw}
        for name, col in COUNTS.items():
            row[col] = (int(_float(raw[name]) or 0)) if name in raw else None
        row["xg"] = _float(raw.get("expected_goals"))
        row["possession"] = _float(raw.get("Ball Possession"))
        out.append(row)
    return out


def match_coaches(fx: dict[str, Any]) -> list[dict[str, Any]]:
    """One dict per team whose lineup names a coach: team_ref, coach_ref, coach_name (D12).
    A lineup without a coach id gives no row (absent, never imputed)."""
    out = []
    for lu in fx.get("lineups") or []:
        coach = lu.get("coach") or {}
        if coach.get("id") is None or not (lu.get("team") or {}).get("id"):
            continue
        out.append({"team_ref": str(lu["team"]["id"]), "coach_ref": str(coach["id"]),
                    "coach_name": coach.get("name")})  # fmt: skip
    return out
