"""Research Evidence Builder v8.

Builds a research-grade evidence package from:
  1) the primary EA + MT5 backtest result.json, and
  2) optional supplementary market/tick evidence produced by the external
     v7.x Colab audit.

Design rule: the full MT5 backtest remains PRIMARY evidence. Tick-derived
MFE/MAE/spread evidence is explicitly labeled SUPPLEMENTARY and is never
presented as representative of the full trade sample unless coverage is
actually complete.

No proprietary EA source, exact indicators, parameters, thresholds, or
internal database/result IDs are copied into the evidence package.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import median

ENGINE_VERSION = "research_evidence_v8"

INTERNAL_KEYS = {
    "result_id", "experiment_id", "research_run_id", "file_id", "upload_id",
    "job_id", "run_id", "request_id", "session_id", "trace_id", "uuid",
}


def _read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _num(v):
    try:
        return float(str(v).strip().replace(",", ""))
    except Exception:
        return None


def _int(v):
    try:
        return int(float(str(v).strip()))
    except Exception:
        return None


def _pick(row, *names):
    norm = {str(k).strip().lower().replace(" ", "_"): v for k, v in row.items()}
    for name in names:
        key = name.strip().lower().replace(" ", "_")
        if key in norm:
            return norm[key]
    return None


def _safe_public(obj):
    """Remove operational identifiers recursively from public-safe evidence."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in INTERNAL_KEYS or any(token in lk for token in ("api_key", "token", "secret", "password")):
                continue
            if lk in {"path", "filepath", "file_path", "source_path", "storage_path"}:
                continue
            out[k] = _safe_public(v)
        return out
    if isinstance(obj, list):
        return [_safe_public(x) for x in obj]
    return obj


def _market_summary(rows: list[dict]) -> dict:
    if not rows:
        return {
            "status": "not_supplied",
            "sample_size": 0,
            "coverage": 0.0,
            "representative_of_primary_sample": False,
        }

    total = len(rows)
    covered = 0
    mfe, mae, spreads = [], [], []
    for r in rows:
        tick_count = _int(_pick(r, "tick_count", "ticks", "tick_count_total"))
        if tick_count is not None and tick_count > 0:
            covered += 1
        x = _num(_pick(r, "mfe_pips", "mfe"))
        y = _num(_pick(r, "mae_pips", "mae"))
        s = _num(_pick(r, "mean_spread", "mean_spread_price"))
        if x is not None:
            mfe.append(x)
        if y is not None:
            mae.append(y)
        if s is not None:
            spreads.append(s)

    coverage = covered / total if total else 0.0
    return {
        "status": "available",
        "sample_size": total,
        "covered_trades": covered,
        "coverage": round(coverage, 6),
        "coverage_pct": round(coverage * 100, 2),
        "representative_of_primary_sample": coverage >= 0.95,
        "mfe_pips": {
            "n": len(mfe),
            "median": round(median(mfe), 4) if mfe else None,
            "max": round(max(mfe), 4) if mfe else None,
        },
        "mae_pips": {
            "n": len(mae),
            "median": round(median(mae), 4) if mae else None,
            "min": round(min(mae), 4) if mae else None,
        },
        "mean_spread": {
            "n": len(spreads),
            "median": round(median(spreads), 8) if spreads else None,
        },
        "interpretation": (
            "Supplementary market-quote evidence only; coverage is insufficient "
            "to generalize MFE/MAE/spread behavior to the full backtest."
            if coverage < 0.95 else
            "Market evidence has high coverage and may be used alongside the primary backtest evidence."
        ),
    }


def _coverage_audit(rows: list[dict]) -> dict:
    if not rows:
        return {"status": "not_supplied"}
    out = {"status": "available", "records": len(rows)}
    years = {}
    for r in rows:
        year = str(_pick(r, "year") or "unknown")
        years.setdefault(year, {"trades": 0, "covered": 0})
        years[year]["trades"] += _int(_pick(r, "trades")) or 0
        years[year]["covered"] += _int(_pick(r, "covered")) or 0
    if years:
        for v in years.values():
            v["coverage_pct"] = round(v["covered"] / v["trades"] * 100, 2) if v["trades"] else 0.0
        out["by_year"] = years
    return out


def build(primary_path: Path, market_path: Path | None = None, coverage_path: Path | None = None) -> dict:
    primary = json.loads(primary_path.read_text(encoding="utf-8"))
    metrics = primary.get("metrics", primary)
    analysis = metrics.get("analysis", {})
    overall = analysis.get("overall", {})
    primary_n = overall.get("n") or metrics.get("data_quality", {}).get("reconstructed_trades") or 0

    market_rows = _read_csv(market_path) if market_path and market_path.exists() else []
    coverage_rows = _read_csv(coverage_path) if coverage_path and coverage_path.exists() else []
    market = _market_summary(market_rows)
    coverage = _coverage_audit(coverage_rows)

    limitations = list(metrics.get("limitations", []) or [])
    if market["status"] == "available" and not market["representative_of_primary_sample"]:
        limitations.append(
            f"Supplementary raw-tick evidence covers {market['coverage_pct']}% of the available market-evidence sample; it must not be treated as representative of the {primary_n}-trade primary backtest."
        )
    limitations.append("Execution prices are not inferred from SL/TP when the MT5 export does not provide a non-zero execution price.")
    limitations.append("No timezone offset is applied to force tick coverage; coverage is accepted only where timestamps genuinely overlap.")

    package = {
        "engine": ENGINE_VERSION,
        "status": "completed",
        "evidence_model": {
            "primary": {
                "source": "MT5 backtest",
                "sample_size": primary_n,
                "role": "PRIMARY",
            },
            "supplementary_market": {
                "source": "raw tick / market quote evidence",
                "role": "SUPPLEMENTARY",
                **market,
            },
        },
        "primary_metrics": {
            "overall": overall,
            "direction_stats": analysis.get("direction_stats", {}),
            "sequence": analysis.get("sequence", {}),
            "time_stats": analysis.get("time_stats", {}),
            "finding_count": len(analysis.get("findings", [])),
            "hypothesis_count": len(analysis.get("hypotheses", [])),
        },
        "market_evidence": market,
        "coverage_audit": coverage,
        "research_rules": {
            "primary_sample_is_authoritative": True,
            "supplementary_tick_evidence_is_not_full_sample": market.get("coverage", 0) < 0.95,
            "execution_price_policy": "Never substitute SL/TP for missing execution price.",
            "timezone_policy": "Do not apply an arbitrary offset to manufacture coverage.",
            "causality": "Descriptive associations and falsifiable hypotheses only; no causal claim from backtest alone.",
        },
        "limitations": limitations,
    }
    return package


def main():
    p = argparse.ArgumentParser(description="Build Research Evidence v8")
    p.add_argument("--primary", required=True, help="Existing result.json from the EA + backtest research run")
    p.add_argument("--market", help="Optional v7.2 market evidence CSV")
    p.add_argument("--coverage", help="Optional v7.4 trade coverage CSV")
    p.add_argument("--output", default="research_evidence_v8.json")
    p.add_argument("--public-output", default="research_evidence_v8_public_safe.json")
    args = p.parse_args()

    package = build(Path(args.primary), Path(args.market) if args.market else None, Path(args.coverage) if args.coverage else None)
    Path(args.output).write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.public_output).write_text(json.dumps(_safe_public(package), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": "completed",
        "engine": ENGINE_VERSION,
        "output": args.output,
        "public_output": args.public_output,
        "primary_sample": package["evidence_model"]["primary"]["sample_size"],
        "market_coverage_pct": package["market_evidence"].get("coverage_pct", 0),
        "market_representative": package["market_evidence"].get("representative_of_primary_sample", False),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
