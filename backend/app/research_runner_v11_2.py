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
    tags=["Research Engine v11.2 Robustness 3-Year"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_metrics(result: ExperimentResult) -> dict[str, Any]:
    raw = result.metrics or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"ExperimentResult {result.id} contains invalid JSON metrics",
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=422,
            detail=f"ExperimentResult {result.id} metrics must be a JSON object",
        )
    return data


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _metric_row(node: Any) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None

    # Accept the metric naming used by the research engine.
    trade_count = node.get("trade_count")
    net_profit = node.get("net_profit")
    profit_factor = node.get("profit_factor")

    if trade_count is None and net_profit is None and profit_factor is None:
        return None

    return {
        "trade_count": _as_int(trade_count),
        "net_profit": _as_float(net_profit),
        "profit_factor": _as_float(profit_factor),
    }


def _find_year_block(
    analysis: dict[str, Any],
    candidates: list[str],
) -> dict[str, Any] | None:
    """Find a year block without requiring one exact v9 JSON shape."""
    for key in candidates:
        value = analysis.get(key)
        if isinstance(value, dict):
            return value

    # Some result versions wrap the analysis one level deeper.
    for key, value in analysis.items():
        if not isinstance(value, dict):
            continue
        if key in {"analysis", "result", "walk_forward", "generalization"}:
            for candidate in candidates:
                nested = value.get(candidate)
                if isinstance(nested, dict):
                    return nested

    return None


def _find_overall(block: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("overall", "metrics", "summary"):
        row = _metric_row(block.get(key))
        if row is not None:
            return row
    return _metric_row(block)


def _collect_group_pairs(
    node: Any,
    prefix: str = "",
) -> dict[str, dict[str, dict[str, Any]]]:
    """
    Collect rows in either of these shapes:

      group: {"is": {...}, "oos": {...}}
      group: {"2024": {...}, "2025": {...}}

    It also walks nested segmentation sections.
    """
    found: dict[str, dict[str, dict[str, Any]]] = {}

    if not isinstance(node, dict):
        return found

    for key, value in node.items():
        if not isinstance(value, dict):
            continue

        name = f"{prefix}|{key}" if prefix else key

        is_row = _metric_row(value.get("is"))
        oos_row = _metric_row(value.get("oos"))
        y24_row = _metric_row(value.get("2024"))
        y25_row = _metric_row(value.get("2025"))

        if is_row is not None and oos_row is not None:
            found[name] = {"2024": is_row, "2025": oos_row}
            continue

        if y24_row is not None and y25_row is not None:
            found[name] = {"2024": y24_row, "2025": y25_row}
            continue

        found.update(_collect_group_pairs(value, name))

    return found


def _collect_single_year_groups(
    node: Any,
    year: str,
    prefix: str = "",
) -> dict[str, dict[str, Any]]:
    """Collect segmentation rows for one already-isolated year."""
    found: dict[str, dict[str, Any]] = {}

    if not isinstance(node, dict):
        return found

    for key, value in node.items():
        if not isinstance(value, dict):
            continue

        name = f"{prefix}|{key}" if prefix else key
        row = _metric_row(value)

        if row is not None and key not in {
            "overall",
            "accounting",
            "experiment_scope",
            "method",
            "limitations",
        }:
            found[name] = row
            continue

        found.update(_collect_single_year_groups(value, year, name))

    return found


def _normalise_group_name(name: str) -> str:
    return (
        name.replace("2024|", "")
        .replace("2025|", "")
        .replace("is|", "")
        .replace("oos|", "")
        .strip("|")
    )


def _extract_baseline_split(analysis: dict[str, Any]) -> dict[str, Any]:
    """
    v11.2 accepts several historical result layouts.

    Preferred:
      is_2024 / oos_2025

    Also accepted:
      2024 / 2025
      is / oos
      generalization sections containing paired is/oos groups
      year-keyed segmentation blocks
    """
    is_block = _find_year_block(analysis, ["is_2024", "2024", "is"])
    oos_block = _find_year_block(analysis, ["oos_2025", "2025", "oos"])

    if is_block is not None and oos_block is not None:
        is_overall = _find_overall(is_block)
        oos_overall = _find_overall(oos_block)
        if is_overall is not None and oos_overall is not None:
            return {
                "mode": "explicit_year_blocks",
                "is": is_overall,
                "oos": oos_overall,
                "is_block": is_block,
                "oos_block": oos_block,
            }

    # Walk-forward v9 style:
    generalization = analysis.get("generalization")
    if isinstance(generalization, dict):
        pairs = _collect_group_pairs(generalization)
        if pairs:
            return {
                "mode": "generalization_pairs",
                "is": _metric_row(analysis.get("is_2024", {}).get("overall"))
                if isinstance(analysis.get("is_2024"), dict)
                else None,
                "oos": _metric_row(analysis.get("oos_2025", {}).get("overall"))
                if isinstance(analysis.get("oos_2025"), dict)
                else None,
                "pairs": pairs,
            }

    # A result can store by_entry_year as:
    # {"2024": {...}, "2025": {...}}
    by_year = analysis.get("by_entry_year")
    if isinstance(by_year, dict):
        y24 = by_year.get("2024")
        y25 = by_year.get("2025")
        if isinstance(y24, dict) and isinstance(y25, dict):
            return {
                "mode": "by_entry_year",
                "is": _metric_row(y24),
                "oos": _metric_row(y25),
                "is_block": y24,
                "oos_block": y25,
            }

    raise HTTPException(
        status_code=422,
        detail=(
            "The 2024/2025 baseline result does not contain a recognized "
            "IS/OOS year split. v11.2 accepts is_2024/oos_2025, "
            "2024/2025, is/oos, generalization pairs, or by_entry_year."
        ),
    )


def _looks_like_2026(analysis: dict[str, Any]) -> bool:
    """Return True only when the result explicitly represents calendar year 2026."""
    scope = analysis.get("experiment_scope")
    if isinstance(scope, dict):
        text = " ".join(str(v) for v in scope.values())
        if "2026" in text:
            return True

    for key in ("period", "year", "dataset_year", "target_year"):
        value = analysis.get(key)
        if str(value) == "2026":
            return True

    return any(key in analysis for key in ("2026", "oos_2026", "unseen_2026", "unseen_year"))


def _combine_periods_2026(periods: dict[str, Any]) -> dict[str, Any] | None:
    """Combine v6-style 2026 H1/H2 overall rows into one year row."""
    rows = []
    for value in periods.values():
        if isinstance(value, dict):
            row = _find_overall(value)
            if row is not None:
                rows.append(row)
    if not rows:
        return None

    trade_count = sum(_as_int(r.get("trade_count")) for r in rows)
    net_profit = sum(float(r.get("net_profit") or 0.0) for r in rows)
    gross_profit = sum(float(r.get("gross_profit") or 0.0) for r in rows)
    gross_loss = sum(float(r.get("gross_loss") or 0.0) for r in rows)
    profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else None
    return {
        "trade_count": trade_count,
        "net_profit": net_profit,
        "profit_factor": profit_factor,
    }


def _extract_2026(analysis: dict[str, Any]) -> dict[str, Any]:
    """Extract a genuine 2026 result; never mistake a 2024/2025 gate for 2026."""
    for key in ("2026", "oos_2026", "unseen_2026", "unseen_year"):
        block = analysis.get(key)
        if isinstance(block, dict):
            overall = _find_overall(block)
            if overall is not None:
                return {"mode": key, "overall": overall, "block": block}

    # v6 stores the target year as experiment_scope + H1/H2 under `periods`.
    if _looks_like_2026(analysis):
        periods = analysis.get("periods")
        if isinstance(periods, dict):
            overall = _combine_periods_2026(periods)
            if overall is not None:
                return {"mode": "periods_2026", "overall": overall, "block": periods}

        overall = _metric_row(analysis.get("overall"))
        if overall is not None:
            return {"mode": "top_level_overall_2026", "overall": overall, "block": analysis}

        for wrapper_key in ("analysis", "result", "unseen_year_validation"):
            wrapper = analysis.get(wrapper_key)
            if isinstance(wrapper, dict):
                overall = _metric_row(wrapper.get("overall"))
                if overall is not None:
                    return {"mode": wrapper_key, "overall": overall, "block": wrapper}

    raise HTTPException(
        status_code=422,
        detail="The unseen-year result is not explicitly identified as 2026 or lacks a recognizable 2026 metric block.",
    )


def _extract_2026_groups(analysis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}

    for section_key in (
        "by_entry_volatility",
        "by_entry_trend",
        "by_entry_session",
        "by_trend",
        "by_volatility",
        "segmentation",
    ):
        section = analysis.get(section_key)
        if isinstance(section, dict):
            for name, row in _collect_single_year_groups(section, "2026").items():
                groups[f"{section_key}|{name}"] = row

    return groups


def _build_baseline_group_map(split: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    pairs = split.get("pairs")
    if isinstance(pairs, dict):
        return pairs

    is_block = split.get("is_block")
    oos_block = split.get("oos_block")

    if isinstance(is_block, dict) and isinstance(oos_block, dict):
        is_groups = _collect_single_year_groups(is_block, "2024")
        oos_groups = _collect_single_year_groups(oos_block, "2025")

        common = set(is_groups) & set(oos_groups)
        return {
            _normalise_group_name(name): {
                "2024": is_groups[name],
                "2025": oos_groups[name],
            }
            for name in common
        }

    return {}


def _gate_row(
    group: str,
    baseline: dict[str, dict[str, Any]],
    unseen: dict[str, Any] | None,
    min_trades: int,
) -> dict[str, Any]:
    y24 = baseline.get("2024", {})
    y25 = baseline.get("2025", {})

    n24 = _as_int(y24.get("trade_count"))
    n25 = _as_int(y25.get("trade_count"))
    p24 = _as_float(y24.get("net_profit"))
    p25 = _as_float(y25.get("net_profit"))
    pf24 = _as_float(y24.get("profit_factor"))
    pf25 = _as_float(y25.get("profit_factor"))

    sample_sufficient = n24 >= min_trades and n25 >= min_trades
    same_sign = (
        p24 is not None
        and p25 is not None
        and ((p24 > 0 and p25 > 0) or (p24 < 0 and p25 < 0))
    )
    pf_gt_1_both = (
        pf24 is not None
        and pf25 is not None
        and pf24 > 1
        and pf25 > 1
    )

    unseen_row = unseen or {}
    n26 = _as_int(unseen_row.get("trade_count"))
    p26 = _as_float(unseen_row.get("net_profit"))
    pf26 = _as_float(unseen_row.get("profit_factor"))

    return {
        "group": group,
        "2024_trade_count": n24,
        "2025_trade_count": n25,
        "2026_trade_count": n26,
        "2024_net_profit": p24,
        "2025_net_profit": p25,
        "2026_net_profit": p26,
        "2024_profit_factor": pf24,
        "2025_profit_factor": pf25,
        "2026_profit_factor": pf26,
        "min_trades_required_each_baseline_period": min_trades,
        "baseline_sample_sufficient": sample_sufficient,
        "baseline_same_sign": same_sign,
        "baseline_pf_gt_1_both": pf_gt_1_both,
        "unseen_year_available": unseen is not None,
        "unseen_year_positive": p26 is not None and p26 > 0,
        "unseen_year_pf_gt_1": pf26 is not None and pf26 > 1,
        "positive_2024_2025_and_2026": (
            sample_sufficient
            and p24 is not None and p25 is not None and p26 is not None
            and p24 > 0 and p25 > 0 and p26 > 0
            and pf24 is not None and pf25 is not None and pf26 is not None
            and pf24 > 1 and pf25 > 1 and pf26 > 1
        ),
    }


# ---------------------------------------------------------------------------
# v11.2 endpoint
# ---------------------------------------------------------------------------

@router.post("/{experiment_id}/robustness-gate-3year")
def robustness_gate_3year(
    experiment_id: str,
    baseline_2024_2025_experiment_id: str = Query(
        ...,
        description="Experiment containing the locked 2024 IS / 2025 OOS baseline result.",
    ),
    min_trades: int = Query(20, ge=1, le=1000),
):
    db: Session = SessionLocal()

    try:
        experiment = (
            db.query(Experiment)
            .filter(Experiment.id == experiment_id)
            .first()
        )
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        baseline_experiment = (
            db.query(Experiment)
            .filter(Experiment.id == baseline_2024_2025_experiment_id)
            .first()
        )
        if not baseline_experiment:
            raise HTTPException(
                status_code=404,
                detail="Baseline 2024/2025 experiment not found",
            )

        current_results = (
            db.query(ExperimentResult)
            .filter(ExperimentResult.experiment_id == experiment_id)
            .order_by(ExperimentResult.created_at.desc())
            .all()
        )
        if not current_results:
            raise HTTPException(
                status_code=404,
                detail="No ExperimentResult found for the unseen-year experiment",
            )

        baseline_results = (
            db.query(ExperimentResult)
            .filter(
                ExperimentResult.experiment_id
                == baseline_2024_2025_experiment_id
            )
            .order_by(ExperimentResult.created_at.desc())
            .all()
        )
        if not baseline_results:
            raise HTTPException(
                status_code=404,
                detail="No ExperimentResult found for the baseline 2024/2025 experiment",
            )

        # Try newest first, then older results until a valid year split is found.
        baseline_split: dict[str, Any] | None = None
        baseline_source: ExperimentResult | None = None
        baseline_errors: list[str] = []

        for candidate in baseline_results:
            try:
                candidate_analysis = _load_metrics(candidate)
                baseline_split = _extract_baseline_split(candidate_analysis)
                baseline_source = candidate
                break
            except HTTPException as exc:
                baseline_errors.append(str(exc.detail))

        if baseline_split is None or baseline_source is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        "No usable 2024/2025 IS/OOS split was found in the "
                        "baseline experiment results."
                    ),
                    "baseline_experiment_id": baseline_2024_2025_experiment_id,
                    "checked_results": len(baseline_results),
                    "errors": baseline_errors[-5:],
                },
            )

        # Try newest current result first; it should be the 2026 validation.
        unseen: dict[str, Any] | None = None
        current_source: ExperimentResult | None = None
        current_analysis: dict[str, Any] | None = None
        current_errors: list[str] = []

        for candidate in current_results:
            try:
                candidate_analysis = _load_metrics(candidate)
                unseen = _extract_2026(candidate_analysis)
                current_source = candidate
                current_analysis = candidate_analysis
                break
            except HTTPException as exc:
                current_errors.append(str(exc.detail))

        if unseen is None or current_source is None or current_analysis is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "No usable unseen-year result was found.",
                    "experiment_id": experiment_id,
                    "checked_results": len(current_results),
                    "errors": current_errors[-5:],
                },
            )

        baseline_groups = _build_baseline_group_map(baseline_split)
        unseen_groups = _extract_2026_groups(current_analysis)

        # Match by exact/simplified group name.  We do not fabricate missing
        # 2026 cells; absent cells remain unavailable.
        unseen_by_name = {
            _normalise_group_name(name): row
            for name, row in unseen_groups.items()
        }

        group_rows: list[dict[str, Any]] = []
        for group, baseline_pair in sorted(baseline_groups.items()):
            group_rows.append(
                _gate_row(
                    group,
                    baseline_pair,
                    unseen_by_name.get(_normalise_group_name(group)),
                    min_trades,
                )
            )

        baseline_overall = {
            "2024": baseline_split.get("is"),
            "2025": baseline_split.get("oos"),
        }
        current_overall = unseen["overall"]

        overall_2024 = baseline_overall["2024"] or {}
        overall_2025 = baseline_overall["2025"] or {}

        p24 = _as_float(overall_2024.get("net_profit"))
        p25 = _as_float(overall_2025.get("net_profit"))
        p26 = _as_float(current_overall.get("net_profit"))

        pf24 = _as_float(overall_2024.get("profit_factor"))
        pf25 = _as_float(overall_2025.get("profit_factor"))
        pf26 = _as_float(current_overall.get("profit_factor"))

        overall_gate = {
            "2024_trade_count": _as_int(overall_2024.get("trade_count")),
            "2025_trade_count": _as_int(overall_2025.get("trade_count")),
            "2026_trade_count": _as_int(current_overall.get("trade_count")),
            "2024_net_profit": p24,
            "2025_net_profit": p25,
            "2026_net_profit": p26,
            "2024_profit_factor": pf24,
            "2025_profit_factor": pf25,
            "2026_profit_factor": pf26,
            "baseline_2024_2025_same_sign": (
                p24 is not None
                and p25 is not None
                and ((p24 > 0 and p25 > 0) or (p24 < 0 and p25 < 0))
            ),
            "pf_gt_1_all_three": (
                pf24 is not None
                and pf25 is not None
                and pf26 is not None
                and pf24 > 1
                and pf25 > 1
                and pf26 > 1
            ),
        }

        baseline_positive = (
            p24 is not None and p25 is not None
            and p24 > 0 and p25 > 0
            and pf24 is not None and pf25 is not None
            and pf24 > 1 and pf25 > 1
        )
        unseen_positive = (
            p26 is not None and p26 > 0
            and pf26 is not None and pf26 > 1
        )

        robust_positive_groups = [
            row for row in group_rows
            if row["positive_2024_2025_and_2026"]
        ]

        conclusion = (
            "SUPPORTED_FOR_FURTHER_RESEARCH"
            if robust_positive_groups
            else "NOT_ESTABLISHED"
        )

        analysis = {
            "experiment_id": experiment_id,
            "baseline_2024_2025_experiment_id": baseline_2024_2025_experiment_id,
            "source_result_id": current_source.id,
            "baseline_source_result_id": baseline_source.id,
            "method": "v11.2 three-year robustness gate with flexible result-shape parsing",
            "min_trades": min_trades,
            "periods": {
                "is": "2024",
                "oos": "2025",
                "unseen": "2026",
            },
            "overall": overall_gate,
            "summary": {
                "baseline_positive_2024_2025": baseline_positive,
                "unseen_2026_positive": unseen_positive,
                "group_count": len(group_rows),
                "robust_positive_groups": len(robust_positive_groups),
                "conclusion": conclusion,
            },
            "groups": group_rows,
            "limitations": [
                "This gate does not rerun the MQL5 EA or create new trades.",
                "2026 is treated as an unseen validation period; the supplied 2026 dataset may be partial-year.",
                "Minimum trade count is a screening rule, not a statistical significance test.",
                "A missing 2026 segmentation cell is not treated as positive evidence.",
                "The engine does not optimize parameters or select a winning trading rule.",
            ],
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

        result = ExperimentResult(
            id=str(uuid.uuid4()),
            experiment_id=experiment.id,
            summary="Three-year robustness gate: 2024 IS, 2025 OOS, 2026 unseen validation.",
            metrics=json.dumps(analysis, ensure_ascii=False, default=str),
            evidence=json.dumps(
                {
                    "source_result_id": current_source.id,
                    "baseline_source_result_id": baseline_source.id,
                    "min_trades": min_trades,
                    "method": analysis["method"],
                },
                ensure_ascii=False,
            ),
            limitations=json.dumps(
                analysis["limitations"],
                ensure_ascii=False,
            ),
            conclusion=conclusion,
        )

        db.add(result)
        experiment.status = "completed"
        experiment.completed_at = datetime.utcnow()
        db.commit()
        db.refresh(result)

        return {
            "experiment_id": experiment.id,
            "result_id": result.id,
            "status": experiment.status,
            "analysis": analysis,
        }

    finally:
        db.close()
