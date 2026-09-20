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
# v11.2
# ---------------------------------------------------------------------------
# Purpose:
#   Run a 3-year robustness gate using:
#       - a 2024 IS / 2025 OOS source result
#       - the current experiment's 2026 unseen-year result
#
# Important:
#   This version does NOT assume that the 2024/2025 source result stores the
#   split under one exact JSON shape. It recognizes several shapes produced
#   by earlier research-runner versions and fails with a useful diagnostic
#   instead of silently guessing.
#
# Supported source shapes:
#   A) {
#        "is_2024": {"overall": {...}, "segmentation": {...}},
#        "oos_2025": {"overall": {...}, "segmentation": {...}},
#        "generalization": {
#            "entry_volatility": {
#                "low": {"is": {...}, "oos": {...}}
#            }
#        }
#      }
#
#   B) {
#        "2024": {...},
#        "2025": {...},
#        ...
#      }
#
#   C) {
#        "is": {...},
#        "oos": {...},
#        ...
#      }
#
#   D) A generalization section whose group rows already contain
#      {"is": {...}, "oos": {...}}.
#
# The 2026 result is expected to contain one-year metrics under:
#   - "overall"
#   - and zero or more "by_*" / "entry_*" segmentation dictionaries.
# ---------------------------------------------------------------------------


YEAR_KEYS = {
    "is": ("is_2024", "is", "2024", "in_sample", "development"),
    "oos": ("oos_2025", "oos", "2025", "out_of_sample", "validation"),
}


def _load_json(value: str | None, field_name: str) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"{field_name} is not valid JSON: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=500,
            detail=f"{field_name} must decode to a JSON object.",
        )
    return data


def _is_metric_row(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return any(
        key in value
        for key in (
            "trade_count",
            "net_profit",
            "profit_factor",
            "expectancy",
            "max_drawdown_absolute",
        )
    )


def _find_year_block(analysis: dict[str, Any], side: str) -> dict[str, Any] | None:
    for key in YEAR_KEYS[side]:
        value = analysis.get(key)
        if isinstance(value, dict):
            return value
    return None


def _metrics_from_block(block: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        return None

    overall = block.get("overall")
    if _is_metric_row(overall):
        return overall

    # Some earlier builds put the metrics directly in the year block.
    if _is_metric_row(block):
        return block

    return None


def _walk_is_oos_rows(
    node: Any,
    prefix: str = "",
) -> list[tuple[str, dict[str, Any]]]:
    """
    Find nested rows that already have an explicit is/oos pair.
    """
    rows: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(node, dict):
        return rows

    # Direct pair.
    if isinstance(node.get("is"), dict) and isinstance(node.get("oos"), dict):
        rows.append((prefix.rstrip("|") or "overall", node))
        return rows

    for key, value in node.items():
        if not isinstance(value, dict):
            continue
        next_prefix = f"{prefix}{key}|"
        rows.extend(_walk_is_oos_rows(value, next_prefix))

    return rows


def _flatten_metric_groups(
    block: dict[str, Any],
    prefix: str = "",
) -> dict[str, dict[str, Any]]:
    """
    Flatten segmentation dictionaries containing metric rows.

    Example:
      {"entry_volatility": {"low": {...}, "high": {...}}}
    becomes:
      {
        "entry_volatility|low": {...},
        "entry_volatility|high": {...},
      }
    """
    out: dict[str, dict[str, Any]] = {}

    if not isinstance(block, dict):
        return out

    for key, value in block.items():
        if _is_metric_row(value):
            out[f"{prefix}{key}".strip("|")] = value
        elif isinstance(value, dict):
            # Do not descend into accounting/method metadata.
            if key in {
                "accounting",
                "method",
                "limitations",
                "experiment_scope",
                "comparison",
                "entry_vs_exit_context_changes",
            }:
                continue
            out.update(_flatten_metric_groups(value, f"{prefix}{key}|"))

    return out


def _match_period_groups(
    is_block: dict[str, Any] | None,
    oos_block: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(is_block, dict) or not isinstance(oos_block, dict):
        return {}

    is_groups = _flatten_metric_groups(is_block)
    oos_groups = _flatten_metric_groups(oos_block)

    matched: dict[str, dict[str, Any]] = {}
    for name in sorted(set(is_groups) & set(oos_groups)):
        matched[name] = {
            "is": is_groups[name],
            "oos": oos_groups[name],
        }
    return matched


def _extract_generalization(
    analysis: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], str]:
    """
    Return {group_name: {"is": metrics, "oos": metrics}}.

    Priority:
      1) explicit generalization rows
      2) paired year blocks (is_2024/oos_2025 etc.)
    """
    generalization = analysis.get("generalization")
    if isinstance(generalization, dict):
        explicit = {}
        for name, row in _walk_is_oos_rows(generalization):
            if _is_metric_row(row.get("is")) and _is_metric_row(row.get("oos")):
                explicit[name] = row

        if explicit:
            return explicit, "generalization.is_oos_rows"

    is_block = _find_year_block(analysis, "is")
    oos_block = _find_year_block(analysis, "oos")

    matched = _match_period_groups(is_block, oos_block)
    if matched:
        return matched, "paired_year_blocks"

    return {}, "none"


def _robust_row(
    name: str,
    row: dict[str, Any],
    min_trades: int,
) -> dict[str, Any]:
    is_data = row.get("is") or {}
    oos_data = row.get("oos") or {}

    is_n = int(is_data.get("trade_count", 0) or 0)
    oos_n = int(oos_data.get("trade_count", 0) or 0)

    is_net = float(is_data.get("net_profit", 0) or 0)
    oos_net = float(oos_data.get("net_profit", 0) or 0)

    is_pf = is_data.get("profit_factor")
    oos_pf = oos_data.get("profit_factor")

    sufficient = is_n >= min_trades and oos_n >= min_trades

    same_sign = (
        (is_net > 0 and oos_net > 0)
        or (is_net < 0 and oos_net < 0)
    )

    pf_gt_1_both = (
        is_pf is not None
        and oos_pf is not None
        and float(is_pf) > 1
        and float(oos_pf) > 1
    )

    positive_and_robust = (
        sufficient
        and is_net > 0
        and oos_net > 0
        and pf_gt_1_both
    )

    return {
        "group": name,
        "is_trade_count": is_n,
        "oos_trade_count": oos_n,
        "min_trades_required_each_period": min_trades,
        "sample_sufficient": sufficient,
        "is_net_profit": is_net,
        "oos_net_profit": oos_net,
        "is_profit_factor": is_pf,
        "oos_profit_factor": oos_pf,
        "net_profit_same_sign": same_sign,
        "pf_gt_1_both": pf_gt_1_both,
        "positive_and_robust": positive_and_robust,
    }


def _source_result_for_experiment(
    db: Session,
    experiment_id: str,
) -> ExperimentResult:
    results = (
        db.query(ExperimentResult)
        .filter(ExperimentResult.experiment_id == experiment_id)
        .order_by(ExperimentResult.created_at.desc())
        .all()
    )

    if not results:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No ExperimentResult found for source experiment "
                f"{experiment_id}"
            ),
        )

    # Prefer a result that actually exposes an IS/OOS structure.
    diagnostics = []
    for result in results:
        analysis = _load_json(result.metrics, "Stored metrics")
        pairs, shape = _extract_generalization(analysis)
        diagnostics.append({
            "result_id": str(result.id),
            "shape": shape,
            "pair_count": len(pairs),
            "top_level_keys": sorted(list(analysis.keys()))[:50],
        })
        if pairs:
            return result

    raise HTTPException(
        status_code=400,
        detail={
            "message": (
                "The 2024/2025 source experiment has results, but none "
                "contains a recognized IS/OOS split."
            ),
            "supported_shapes": [
                "is_2024 + oos_2025",
                "is + oos",
                "2024 + 2025",
                "generalization rows containing is + oos",
            ],
            "diagnostics": diagnostics,
            "hint": (
                "If the source result is a single-year result, pass a "
                "source experiment/result that actually contains both "
                "2024 IS and 2025 OOS data."
            ),
        },
    )


def _find_2026_metrics(
    analysis: dict[str, Any],
) -> dict[str, Any]:
    overall = analysis.get("overall")
    if _is_metric_row(overall):
        return overall

    # Accept a few explicit year labels as a fallback.
    for key in ("2026", "unseen_2026", "oos_2026"):
        block = analysis.get(key)
        metrics = _metrics_from_block(block)
        if metrics:
            return metrics

    raise HTTPException(
        status_code=400,
        detail={
            "message": (
                "The 2026 result does not contain recognizable one-year "
                "overall metrics."
            ),
            "expected": ["overall", "2026", "unseen_2026", "oos_2026"],
            "top_level_keys": sorted(list(analysis.keys()))[:50],
        },
    )


def _three_year_group_row(
    name: str,
    baseline_row: dict[str, Any],
    unseen_2026: dict[str, Any],
    min_trades: int,
) -> dict[str, Any]:
    is_data = baseline_row.get("is") or {}
    oos_data = baseline_row.get("oos") or {}

    y26_n = int(unseen_2026.get("trade_count", 0) or 0)
    y26_net = float(unseen_2026.get("net_profit", 0) or 0)
    y26_pf = unseen_2026.get("profit_factor")

    base = _robust_row(name, baseline_row, min_trades)

    three_year_sample_sufficient = (
        base["sample_sufficient"] and y26_n >= min_trades
    )

    # 3-year positive evidence requires positive net and PF>1 in all
    # three observed periods.
    three_year_positive = (
        three_year_sample_sufficient
        and float(is_data.get("net_profit", 0) or 0) > 0
        and float(oos_data.get("net_profit", 0) or 0) > 0
        and y26_net > 0
        and is_data.get("profit_factor") is not None
        and oos_data.get("profit_factor") is not None
        and y26_pf is not None
        and float(is_data["profit_factor"]) > 1
        and float(oos_data["profit_factor"]) > 1
        and float(y26_pf) > 1
    )

    return {
        **base,
        "unseen_2026_trade_count": y26_n,
        "unseen_2026_net_profit": y26_net,
        "unseen_2026_profit_factor": y26_pf,
        "three_year_sample_sufficient": three_year_sample_sufficient,
        "positive_all_three_years": three_year_positive,
    }


@router.post("/{experiment_id}/robustness-gate-3year")
def robustness_gate_3year(
    experiment_id: str,
    baseline_2024_2025_experiment_id: str = Query(
        ...,
        description=(
            "Experiment containing the 2024 IS / 2025 OOS "
            "walk-forward evidence."
        ),
    ),
    min_trades: int = Query(20, ge=1, le=1000),
):
    """
    3-year robustness gate:

      baseline source = 2024 IS + 2025 OOS
      current experiment result = 2026 unseen year

    The gate is deterministic and does not optimize parameters or select
    a trading policy.
    """
    db: Session = SessionLocal()

    try:
        current = (
            db.query(Experiment)
            .filter(Experiment.id == experiment_id)
            .first()
        )
        if not current:
            raise HTTPException(
                status_code=404,
                detail="Current 2026 experiment not found",
            )

        baseline_experiment = (
            db.query(Experiment)
            .filter(Experiment.id == baseline_2024_2025_experiment_id)
            .first()
        )
        if not baseline_experiment:
            raise HTTPException(
                status_code=404,
                detail="2024/2025 baseline experiment not found",
            )

        # ---------------------------------------------------------------
        # 1) Load the 2024/2025 source result.
        # ---------------------------------------------------------------
        baseline_result = _source_result_for_experiment(
            db,
            baseline_2024_2025_experiment_id,
        )
        baseline_analysis = _load_json(
            baseline_result.metrics,
            "Baseline stored metrics",
        )

        baseline_pairs, baseline_shape = _extract_generalization(
            baseline_analysis
        )

        # ---------------------------------------------------------------
        # 2) Load the newest 2026 result.
        # ---------------------------------------------------------------
        current_results = (
            db.query(ExperimentResult)
            .filter(ExperimentResult.experiment_id == experiment_id)
            .order_by(ExperimentResult.created_at.desc())
            .all()
        )
        if not current_results:
            raise HTTPException(
                status_code=404,
                detail="No ExperimentResult found for current 2026 experiment",
            )

        current_result = current_results[0]
        current_analysis = _load_json(
            current_result.metrics,
            "Current stored metrics",
        )
        overall_2026 = _find_2026_metrics(current_analysis)

        # ---------------------------------------------------------------
        # 3) Apply the 2026 result to matching regime groups.
        #
        # The 2026 result is a single-year result. Therefore a 2026
        # subgroup can only be attached to a baseline group when the
        # current result contains a matching group name.
        # ---------------------------------------------------------------
        current_groups = _flatten_metric_groups(current_analysis)

        sections: dict[str, list[dict[str, Any]]] = {}
        all_rows: list[dict[str, Any]] = []

        for baseline_name, baseline_row in sorted(baseline_pairs.items()):
            # Baseline names look like:
            #   entry_volatility|low
            #   entry_volatility_x_trend|low|up
            #
            # Current 2026 keys are usually the same section/name layout.
            y26 = current_groups.get(baseline_name)

            if y26 is None:
                # Also try the leaf name for compatibility with a result
                # whose 2026 payload omits the section prefix.
                leaf = baseline_name.split("|")[-1]
                candidates = [
                    (key, value)
                    for key, value in current_groups.items()
                    if key == leaf or key.endswith(f"|{leaf}")
                ]
                if len(candidates) == 1:
                    y26 = candidates[0][1]

            if y26 is None:
                continue

            row = _three_year_group_row(
                baseline_name,
                baseline_row,
                y26,
                min_trades,
            )

            section = baseline_name.split("|", 1)[0]
            sections.setdefault(section, []).append(row)
            all_rows.append(row)

        # ---------------------------------------------------------------
        # 4) Overall 3-year evidence.
        # ---------------------------------------------------------------
        is_overall = _metrics_from_block(
            _find_year_block(baseline_analysis, "is")
        )
        oos_overall = _metrics_from_block(
            _find_year_block(baseline_analysis, "oos")
        )

        if is_overall is None or oos_overall is None:
            # If the source had an explicit overall generalization pair,
            # use that before failing.
            explicit_overall = baseline_pairs.get("overall")
            if explicit_overall:
                is_overall = explicit_overall.get("is")
                oos_overall = explicit_overall.get("oos")

        if is_overall is None or oos_overall is None:
            overall_2024_2025 = None
        else:
            overall_2024_2025 = {
                "2024": is_overall,
                "2025": oos_overall,
            }

        overall_gate = None
        if overall_2024_2025 is not None:
            y24 = overall_2024_2025["2024"]
            y25 = overall_2024_2025["2025"]
            y26 = overall_2026

            overall_gate = {
                "2024_trade_count": y24.get("trade_count"),
                "2025_trade_count": y25.get("trade_count"),
                "2026_trade_count": y26.get("trade_count"),
                "2024_net_profit": y24.get("net_profit"),
                "2025_net_profit": y25.get("net_profit"),
                "2026_net_profit": y26.get("net_profit"),
                "2024_profit_factor": y24.get("profit_factor"),
                "2025_profit_factor": y25.get("profit_factor"),
                "2026_profit_factor": y26.get("profit_factor"),
                "sample_sufficient_all_three": (
                    int(y24.get("trade_count", 0) or 0) >= min_trades
                    and int(y25.get("trade_count", 0) or 0) >= min_trades
                    and int(y26.get("trade_count", 0) or 0) >= min_trades
                ),
                "positive_net_all_three": (
                    float(y24.get("net_profit", 0) or 0) > 0
                    and float(y25.get("net_profit", 0) or 0) > 0
                    and float(y26.get("net_profit", 0) or 0) > 0
                ),
                "pf_gt_1_all_three": (
                    y24.get("profit_factor") is not None
                    and y25.get("profit_factor") is not None
                    and y26.get("profit_factor") is not None
                    and float(y24["profit_factor"]) > 1
                    and float(y25["profit_factor"]) > 1
                    and float(y26["profit_factor"]) > 1
                ),
            }

            overall_gate["positive_all_three"] = (
                overall_gate["sample_sufficient_all_three"]
                and overall_gate["positive_net_all_three"]
                and overall_gate["pf_gt_1_all_three"]
            )

        robust_positive = [
            row for row in all_rows
            if row["positive_all_three_years"]
        ]

        sufficiently_sampled = [
            row for row in all_rows
            if row["three_year_sample_sufficient"]
        ]

        # Keep the original v10-style robust negatives as a diagnostic.
        robust_negative = [
            row for row in all_rows
            if (
                row["three_year_sample_sufficient"]
                and row["is_net_profit"] < 0
                and row["oos_net_profit"] < 0
                and row["unseen_2026_net_profit"] < 0
            )
        ]

        conclusion = (
            "SUPPORTED_FOR_FURTHER_RESEARCH"
            if robust_positive
            else "NOT_ESTABLISHED"
        )

        payload = {
            "experiment_id": experiment_id,
            "source_baseline_experiment_id": baseline_2024_2025_experiment_id,
            "source_baseline_result_id": str(baseline_result.id),
            "source_2026_result_id": str(current_result.id),
            "method": "v11.2 three-year robustness gate: 2024 IS / 2025 OOS / 2026 unseen-year",
            "baseline_parser_shape": baseline_shape,
            "min_trades": min_trades,
            "overall": overall_gate,
            "summary": {
                "matched_groups": len(all_rows),
                "sufficiently_sampled_groups": len(sufficiently_sampled),
                "robust_positive_groups": len(robust_positive),
                "robust_negative_groups": len(robust_negative),
                "conclusion": conclusion,
            },
            "robust_positive_groups": robust_positive,
            "robust_negative_groups": robust_negative,
            "sections": sections,
            "limitations": [
                "This gate is a deterministic evidence filter; it does not optimize parameters.",
                "2026 is treated as an unseen validation year and is not used to adjust parameters.",
                "Minimum trade count is a screening rule, not a statistical significance test.",
                "A three-year positive subgroup is evidence for further research, not proof of future profitability.",
                "The 2026 source is a partial-year result if the supplied MT5 data does not cover the full calendar year.",
                "Subgroups that cannot be matched between the 2024/2025 baseline and 2026 are excluded from the three-year subgroup gate rather than guessed.",
            ],
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

        result = ExperimentResult(
            id=str(uuid.uuid4()),
            experiment_id=current.id,
            summary=f"v11.2 three-year robustness gate: {conclusion}",
            metrics=json.dumps(payload, ensure_ascii=False, default=str),
            evidence=json.dumps(
                {
                    "source_baseline_experiment_id": baseline_2024_2025_experiment_id,
                    "source_baseline_result_id": str(baseline_result.id),
                    "source_2026_result_id": str(current_result.id),
                    "baseline_parser_shape": baseline_shape,
                    "min_trades": min_trades,
                },
                ensure_ascii=False,
            ),
            limitations=json.dumps(
                payload["limitations"],
                ensure_ascii=False,
            ),
            conclusion=conclusion,
        )

        db.add(result)
        db.commit()
        db.refresh(result)

        return {
            "experiment_id": experiment_id,
            "result_id": str(result.id),
            "status": "completed",
            "analysis": payload,
        }

    finally:
        db.close()
