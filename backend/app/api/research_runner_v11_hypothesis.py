from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.research_models import Experiment, ExperimentResult


router = APIRouter(
    prefix="/research/experiments",
    tags=["Research Engine v11"],
)


class HypothesisPreregistration(BaseModel):
    prediction: str = Field(..., min_length=10)
    target_section: str = Field(
        "entry_volatility_x_trend",
        description="Result section to evaluate, e.g. entry_volatility_x_trend.",
    )
    min_trades: int = Field(20, ge=1)
    require_same_sign: bool = True
    require_pf_gt_1_both: bool = True
    notes: str | None = None


def _load_preregistration(experiment: Experiment) -> dict | None:
    marker = "V11_PREREGISTRATION:"
    specification = experiment.specification or ""
    for line in specification.splitlines():
        if line.startswith(marker):
            try:
                return json.loads(line[len(marker):])
            except json.JSONDecodeError:
                return None
    return None


def _latest_walk_forward_result(
    db: Session,
    experiment_id: str,
) -> ExperimentResult | None:
    results = (
        db.query(ExperimentResult)
        .filter(ExperimentResult.experiment_id == experiment_id)
        .order_by(ExperimentResult.created_at.desc())
        .all()
    )

    for result in results:
        try:
            payload = json.loads(result.metrics or "{}")
        except json.JSONDecodeError:
            continue

        if (
            isinstance(payload, dict)
            and "is_2024" in payload
            and "oos_2025" in payload
        ):
            return result

    return None


def _group_gate(is_row: dict, oos_row: dict, prereg: dict) -> dict:
    min_trades = prereg["min_trades"]
    is_count = int(is_row.get("trade_count") or 0)
    oos_count = int(oos_row.get("trade_count") or 0)

    same_sign = (
        (is_row.get("net_profit", 0) > 0 and oos_row.get("net_profit", 0) > 0)
        or
        (is_row.get("net_profit", 0) < 0 and oos_row.get("net_profit", 0) < 0)
    )

    pf_both = (
        is_row.get("profit_factor") is not None
        and oos_row.get("profit_factor") is not None
        and is_row["profit_factor"] > 1
        and oos_row["profit_factor"] > 1
    )

    sample_ok = is_count >= min_trades and oos_count >= min_trades

    passed = sample_ok
    if prereg["require_same_sign"]:
        passed = passed and same_sign
    if prereg["require_pf_gt_1_both"]:
        passed = passed and pf_both

    return {
        "is_trade_count": is_count,
        "oos_trade_count": oos_count,
        "is_net_profit": is_row.get("net_profit"),
        "oos_net_profit": oos_row.get("net_profit"),
        "is_profit_factor": is_row.get("profit_factor"),
        "oos_profit_factor": oos_row.get("profit_factor"),
        "sample_sufficient": sample_ok,
        "net_profit_same_sign": same_sign,
        "pf_gt_1_both": pf_both,
        "passes_preregistered_gate": passed,
    }


@router.post("/{experiment_id}/hypothesis-preregister")
def hypothesis_preregister(
    experiment_id: str,
    body: HypothesisPreregistration,
    db: Session = Depends(get_db),
):
    experiment = (
        db.query(Experiment)
        .filter(Experiment.id == experiment_id)
        .first()
    )
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    if _load_preregistration(experiment):
        raise HTTPException(
            status_code=409,
            detail="Hypothesis is already preregistered for this experiment",
        )

    prereg = {
        "prediction": body.prediction,
        "target_section": body.target_section,
        "min_trades": body.min_trades,
        "require_same_sign": body.require_same_sign,
        "require_pf_gt_1_both": body.require_pf_gt_1_both,
        "notes": body.notes,
        "locked_at": datetime.utcnow().isoformat() + "Z",
        "method": "v11 preregistration; criteria are locked before evaluation",
    }

    original = experiment.specification or ""
    experiment.specification = (
        original.rstrip()
        + "\n\nV11_PREREGISTRATION:"
        + json.dumps(prereg, ensure_ascii=False, separators=(",", ":"))
    )
    db.commit()
    db.refresh(experiment)

    return {
        "experiment_id": experiment.id,
        "status": "preregistered",
        "preregistration": prereg,
    }


@router.post("/{experiment_id}/hypothesis-evaluate")
def hypothesis_evaluate(
    experiment_id: str,
    db: Session = Depends(get_db),
):
    experiment = (
        db.query(Experiment)
        .filter(Experiment.id == experiment_id)
        .first()
    )
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    prereg = _load_preregistration(experiment)
    if not prereg:
        raise HTTPException(
            status_code=409,
            detail="No v11 preregistration found. Preregister the hypothesis first.",
        )

    source = _latest_walk_forward_result(db, experiment_id)
    if not source:
        raise HTTPException(
            status_code=409,
            detail="No walk-forward result with 2024 IS and 2025 OOS evidence found.",
        )

    try:
        analysis = json.loads(source.metrics or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail="Latest walk-forward result contains invalid JSON metrics.",
        ) from exc

    is_seg = analysis.get("is_2024", {}).get("segmentation", {})
    oos_seg = analysis.get("oos_2025", {}).get("segmentation", {})
    section = prereg["target_section"]

    is_groups = is_seg.get(section)
    oos_groups = oos_seg.get(section)

    if not isinstance(is_groups, dict) or not isinstance(oos_groups, dict):
        raise HTTPException(
            status_code=400,
            detail=f"Target section '{section}' is not available in both IS and OOS results.",
        )

    group_results = {}
    passed_groups = []
    sampled_groups = []

    for name, is_row in is_groups.items():
        oos_row = oos_groups.get(name)
        if not isinstance(oos_row, dict):
            continue

        gate = _group_gate(is_row, oos_row, prereg)
        group_results[name] = gate

        if gate["sample_sufficient"]:
            sampled_groups.append(name)
        if gate["passes_preregistered_gate"]:
            passed_groups.append(name)

    overall_is = analysis.get("is_2024", {}).get("overall", {})
    overall_oos = analysis.get("oos_2025", {}).get("overall", {})
    overall_gate = _group_gate(overall_is, overall_oos, prereg)

    conclusion = (
        "SUPPORTED_FOR_FURTHER_RESEARCH"
        if passed_groups
        else "NOT_ESTABLISHED"
    )

    payload = {
        "experiment_id": experiment_id,
        "source_result_id": source.id,
        "method": "v11 preregistered hypothesis evaluation over existing walk-forward evidence",
        "preregistration": prereg,
        "overall_gate": overall_gate,
        "target_section": section,
        "groups": group_results,
        "summary": {
            "sampled_groups": len(sampled_groups),
            "passed_groups": len(passed_groups),
            "passed_group_names": passed_groups,
            "conclusion": conclusion,
        },
        "limitations": [
            "This endpoint evaluates existing MT5 walk-forward evidence; it does not execute the MQL5 EA.",
            "The volatility and trend labels are external research labels, not necessarily the EA's internal logic.",
            "Minimum trade count is a screening rule, not a statistical significance test.",
            "The same 2024/2025 sample is reused; unseen future periods are still required.",
            "No parameter optimization or automatic EA modification is performed.",
        ],
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    result = ExperimentResult(
        id=str(uuid.uuid4()),
        experiment_id=experiment.id,
        summary=conclusion,
        metrics=json.dumps(payload, ensure_ascii=False),
        evidence=json.dumps(
            {
                "source_result_id": source.id,
                "preregistration": prereg,
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
        "experiment_id": experiment.id,
        "result_id": result.id,
        "status": "completed",
        "analysis": payload,
    }
