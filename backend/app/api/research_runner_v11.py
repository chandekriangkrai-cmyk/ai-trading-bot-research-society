from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult

router = APIRouter(
    prefix="/research/experiments",
    tags=["Research Engine v11 3-Year Robustness"],
)


def _latest_result(db: Session, experiment_id: str) -> ExperimentResult:
    rows = (
        db.query(ExperimentResult)
        .filter(ExperimentResult.experiment_id == experiment_id)
        .order_by(ExperimentResult.created_at.desc())
        .all()
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No ExperimentResult found for experiment {experiment_id}",
        )
    return rows[0]


def _load_metrics(result: ExperimentResult) -> dict[str, Any]:
    try:
        data = json.loads(result.metrics or "{}")
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Stored metrics is not valid JSON for result {result.id}: {exc}",
        )
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail=f"Metrics for result {result.id} is not an object")
    return data


def _overall(data: dict[str, Any]) -> dict[str, Any]:
    # Supports v8/v9/v10 result shapes.
    if isinstance(data.get("overall"), dict):
        return data["overall"]
    if isinstance(data.get("is_2024"), dict):
        return data["is_2024"].get("overall", {})
    return {}


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _group_rows(data_by_year: dict[str, dict[str, Any]], section: str) -> list[dict[str, Any]]:
    maps = {year: _section(data, section) for year, data in data_by_year.items()}
    keys = sorted(set().union(*(set(m.keys()) for m in maps.values())))
    rows: list[dict[str, Any]] = []
    for key in keys:
        years: dict[str, dict[str, Any]] = {}
        for year, mapping in maps.items():
            value = mapping.get(key)
            if isinstance(value, dict):
                # v9 generalization sections may store {is:..., oos:...};
                # v11 expects raw per-year sections, so unwrap when present.
                if "is" in value or "oos" in value:
                    candidate = value.get("oos") if year == "2025" else value.get("is")
                    years[year] = candidate if isinstance(candidate, dict) else {}
                else:
                    years[year] = value
            else:
                years[year] = {}

        def n(y: str) -> int:
            return int(years[y].get("trade_count", 0) or 0)

        def net(y: str) -> float:
            return float(years[y].get("net_profit", 0) or 0)

        def pf(y: str):
            return years[y].get("profit_factor")

        sample_ok = all(n(y) >= CURRENT_MIN_TRADES for y in data_by_year)
        nets = [net(y) for y in data_by_year]
        same_positive = all(x > 0 for x in nets)
        same_negative = all(x < 0 for x in nets)
        pfs = [pf(y) for y in data_by_year]
        pf_both = all(x is not None and float(x) > 1 for x in pfs)

        rows.append({
            "group": key,
            "trade_count": {y: n(y) for y in data_by_year},
            "net_profit": {y: net(y) for y in data_by_year},
            "profit_factor": {y: pf(y) for y in data_by_year},
            "sample_sufficient_all_years": sample_ok,
            "net_profit_same_positive_all_years": same_positive,
            "net_profit_same_negative_all_years": same_negative,
            "pf_gt_1_all_years": pf_both,
            "robust_positive_3year": sample_ok and same_positive and pf_both,
            "robust_negative_3year": sample_ok and same_negative,
        })
    return rows


@router.post("/{experiment_id}/robustness-gate-3year")
def robustness_gate_3year(
    experiment_id: str,
    baseline_2024_experiment_id: str = Query(...),
    baseline_2025_experiment_id: str = Query(...),
    min_trades: int = Query(20, ge=1, le=1000),
):
    global CURRENT_MIN_TRADES
    CURRENT_MIN_TRADES = min_trades

    db: Session = SessionLocal()
    try:
        ids = {
            "2024": baseline_2024_experiment_id,
            "2025": baseline_2025_experiment_id,
            "2026": experiment_id,
        }
        for year, eid in ids.items():
            if not db.query(Experiment).filter(Experiment.id == eid).first():
                raise HTTPException(status_code=404, detail=f"{year} experiment not found: {eid}")

        results = {year: _latest_result(db, eid) for year, eid in ids.items()}
        data = {year: _load_metrics(result) for year, result in results.items()}

        overall_by_year = {year: _overall(data[year]) for year in data}
        overall = {
            "trade_count": {y: int(overall_by_year[y].get("trade_count", 0) or 0) for y in data},
            "net_profit": {y: float(overall_by_year[y].get("net_profit", 0) or 0) for y in data},
            "profit_factor": {y: overall_by_year[y].get("profit_factor") for y in data},
            "expectancy": {y: overall_by_year[y].get("expectancy") for y in data},
            "max_drawdown_absolute": {y: overall_by_year[y].get("max_drawdown_absolute") for y in data},
        }
        overall_sample_ok = all(v >= min_trades for v in overall["trade_count"].values())
        overall_positive = all(v > 0 for v in overall["net_profit"].values())
        overall_negative = all(v < 0 for v in overall["net_profit"].values())
        overall_pf_positive = all(
            v is not None and float(v) > 1 for v in overall["profit_factor"].values()
        )
        overall_gate = {
            **overall,
            "min_trades_required_each_year": min_trades,
            "sample_sufficient_all_years": overall_sample_ok,
            "net_profit_same_positive_all_years": overall_positive,
            "net_profit_same_negative_all_years": overall_negative,
            "pf_gt_1_all_years": overall_pf_positive,
            "robust_positive_3year": overall_sample_ok and overall_positive and overall_pf_positive,
        }

        sections = [
            "by_entry_volatility",
            "by_entry_trend",
            "by_entry_session",
            "entry_volatility_x_trend",
            "entry_volatility_x_session",
            "entry_volatility_x_trend_x_session",
        ]
        section_results: dict[str, list[dict[str, Any]]] = {}
        positives: list[dict[str, Any]] = []
        negatives: list[dict[str, Any]] = []
        sampled: list[dict[str, Any]] = []

        for section in sections:
            rows = _group_rows(data, section)
            section_results[section] = rows
            for row in rows:
                item = {"section": section, **row}
                if row["sample_sufficient_all_years"]:
                    sampled.append(item)
                if row["robust_positive_3year"]:
                    positives.append(item)
                if row["robust_negative_3year"]:
                    negatives.append(item)

        conclusion = "SUPPORTED_FOR_FURTHER_RESEARCH" if positives else "NOT_ESTABLISHED"
        limitations = [
            "2024 is treated as the development/IS year, 2025 as OOS #1, and 2026 as OOS #2.",
            "2026 is a partial-year dataset through the latest supplied MT5 report date, not a full calendar year.",
            "This gate is a deterministic evidence filter; minimum trade count is a screening rule, not a statistical significance test.",
            "No 2026 parameter adjustment is performed by this endpoint.",
            "Context labels are external research proxies and do not reproduce the EA's internal execution logic.",
            "A three-year robust positive group still requires further unseen data and actual MQL5 execution validation before deployment decisions.",
        ]

        payload = {
            "experiment_id": experiment_id,
            "source_experiment_ids": ids,
            "source_result_ids": {year: results[year].id for year in results},
            "method": "v11 deterministic 3-year robustness gate",
            "periods": {
                "2024": "IS / development",
                "2025": "OOS #1",
                "2026": "OOS #2 partial-year",
            },
            "min_trades": min_trades,
            "overall": overall_gate,
            "summary": {
                "sufficiently_sampled_groups": len(sampled),
                "robust_positive_groups": len(positives),
                "robust_negative_groups": len(negatives),
                "conclusion": conclusion,
            },
            "robust_positive_groups": positives,
            "robust_negative_groups": negatives,
            "sections": section_results,
            "limitations": limitations,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

        result = ExperimentResult(
            id=uuid.uuid4().hex,
            experiment_id=experiment_id,
            summary=conclusion,
            metrics=json.dumps(payload, ensure_ascii=False),
            evidence=json.dumps({
                "source_experiment_ids": ids,
                "source_result_ids": payload["source_result_ids"],
                "min_trades": min_trades,
            }, ensure_ascii=False),
            limitations=json.dumps(limitations, ensure_ascii=False),
            conclusion=conclusion,
        )
        db.add(result)
        db.commit()
        db.refresh(result)

        return {
            "experiment_id": experiment_id,
            "result_id": result.id,
            "status": "completed",
            "analysis": payload,
        }
    finally:
        db.close()
