from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.research_models import Experiment, ExperimentResult

router = APIRouter(
    prefix="/research/experiments",
    tags=["Research Engine v10 Robustness"],
)


def _walk_groups(node: dict, prefix: str = "") -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    if not isinstance(node, dict):
        return rows
    for key, value in node.items():
        if not isinstance(value, dict):
            continue
        if "is" in value and "oos" in value:
            rows.append((f"{prefix}{key}", value))
        else:
            rows.extend(_walk_groups(value, f"{prefix}{key}|"))
    return rows


def _robust_row(name: str, row: dict, min_trades: int) -> dict:
    is_data = row.get("is") or {}
    oos_data = row.get("oos") or {}
    is_n = int(is_data.get("trade_count", 0) or 0)
    oos_n = int(oos_data.get("trade_count", 0) or 0)
    is_net = float(is_data.get("net_profit", 0) or 0)
    oos_net = float(oos_data.get("net_profit", 0) or 0)
    is_pf = is_data.get("profit_factor")
    oos_pf = oos_data.get("profit_factor")

    sufficient = is_n >= min_trades and oos_n >= min_trades
    same_sign = (is_net > 0 and oos_net > 0) or (is_net < 0 and oos_net < 0)
    positive_both = (
        sufficient
        and is_net > 0
        and oos_net > 0
        and is_pf is not None
        and oos_pf is not None
        and float(is_pf) > 1
        and float(oos_pf) > 1
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
        "pf_gt_1_both": (
            is_pf is not None and oos_pf is not None
            and float(is_pf) > 1 and float(oos_pf) > 1
        ),
        "positive_and_robust": positive_both,
    }


@router.post("/{experiment_id}/robustness-gate")
def robustness_gate(
    experiment_id: str,
    min_trades: int = Query(20, ge=1, le=1000),
):
    db: Session = SessionLocal()
    try:
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        if not experiment:
            raise HTTPException(status_code=404, detail="Experiment not found")

        results = (
            db.query(ExperimentResult)
            .filter(ExperimentResult.experiment_id == experiment_id)
            .order_by(ExperimentResult.created_at.desc())
            .all()
        )
        if not results:
            raise HTTPException(
                status_code=404,
                detail="No ExperimentResult found for this experiment",
            )

        latest = results[0]
        try:
            analysis = json.loads(latest.metrics or "{}")
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Stored metrics is not valid JSON: {exc}",
            )

        generalization = analysis.get("generalization", {})
        if not generalization:
            raise HTTPException(
                status_code=400,
                detail="Experiment result does not contain v9 generalization data",
            )

        sections = {}
        all_rows = []
        for section_name, section_data in generalization.items():
            if not isinstance(section_data, dict):
                continue
            section_rows = []
            for name, row in _walk_groups(section_data):
                item = _robust_row(name, row, min_trades)
                section_rows.append(item)
                all_rows.append((section_name, item))
            sections[section_name] = section_rows

        robust_positive = [
            {"section": s, **item}
            for s, item in all_rows
            if item["positive_and_robust"]
        ]

        robust_negative = [
            {"section": s, **item}
            for s, item in all_rows
            if (
                item["sample_sufficient"]
                and item["is_net_profit"] < 0
                and item["oos_net_profit"] < 0
            )
        ]

        sufficiently_sampled = [
            {"section": s, **item}
            for s, item in all_rows
            if item["sample_sufficient"]
        ]

        overall = generalization.get("overall", {})
        # v9 does not currently store an overall IS/OOS pair in all builds.
        # Derive the overall pair from the ExperimentResult evidence when available.
        overall_is = analysis.get("is_2024", {}).get("overall", {})
        overall_oos = analysis.get("oos_2025", {}).get("overall", {})

        overall_gate = {
            "is_trade_count": overall_is.get("trade_count"),
            "oos_trade_count": overall_oos.get("trade_count"),
            "is_net_profit": overall_is.get("net_profit"),
            "oos_net_profit": overall_oos.get("net_profit"),
            "is_profit_factor": overall_is.get("profit_factor"),
            "oos_profit_factor": overall_oos.get("profit_factor"),
            "net_profit_same_sign": (
                (overall_is.get("net_profit", 0) > 0 and overall_oos.get("net_profit", 0) > 0)
                or (overall_is.get("net_profit", 0) < 0 and overall_oos.get("net_profit", 0) < 0)
            ),
            "pf_gt_1_both": (
                overall_is.get("profit_factor") is not None
                and overall_oos.get("profit_factor") is not None
                and overall_is.get("profit_factor") > 1
                and overall_oos.get("profit_factor") > 1
            ),
        }

        conclusion = (
            "NOT_ESTABLISHED"
            if not robust_positive
            else "SUPPORTED_FOR_FURTHER_RESEARCH"
        )

        payload = {
            "experiment_id": experiment_id,
            "source_result_id": latest.id,
            "method": "v10 robustness gate on v9 walk-forward result",
            "min_trades": min_trades,
            "overall": overall_gate,
            "summary": {
                "sufficiently_sampled_groups": len(sufficiently_sampled),
                "robust_positive_groups": len(robust_positive),
                "robust_negative_groups": len(robust_negative),
                "conclusion": conclusion,
            },
            "robust_positive_groups": robust_positive,
            "robust_negative_groups": robust_negative,
            "sections": sections,
            "limitations": [
                "This gate does not create new market data or new years.",
                "It is a deterministic evidence filter over the v9 2024 IS / 2025 OOS result.",
                "Minimum trade count is a screening rule, not a statistical significance test.",
                "Positive groups must still be validated on additional unseen periods and with the actual MQL5 execution path.",
            ],
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }

        result = ExperimentResult(
            id=__import__("uuid").uuid4().hex,
            experiment_id=experiment_id,
            summary=conclusion,
            metrics=json.dumps(payload, ensure_ascii=False),
            evidence=json.dumps(
                {
                    "source_result_id": latest.id,
                    "min_trades": min_trades,
                },
                ensure_ascii=False,
            ),
            limitations=json.dumps(payload["limitations"], ensure_ascii=False),
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
