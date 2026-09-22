import json
from pathlib import Path
from tools.research_evidence_v8 import build


def test_partial_tick_evidence_is_supplementary(tmp_path: Path):
    primary = {
        "metrics": {
            "analysis": {
                "overall": {"n": 475, "net_profit": 1281.89},
                "direction_stats": {}, "sequence": {}, "time_stats": {},
                "findings": [], "hypotheses": []
            },
            "limitations": []
        }
    }
    pp = tmp_path / "result.json"
    pp.write_text(json.dumps(primary), encoding="utf-8")
    mp = tmp_path / "market.csv"
    mp.write_text("trade_id,tick_count,mfe_pips,mae_pips,mean_spread\n1,100,20,-10,0.00002\n2,100,30,-20,0.00003\n", encoding="utf-8")
    pkg = build(pp, mp)
    assert pkg["evidence_model"]["primary"]["sample_size"] == 475
    assert pkg["market_evidence"]["coverage_pct"] == 100.0
    assert pkg["market_evidence"]["representative_of_primary_sample"] is True


def test_empty_market_data_never_becomes_primary(tmp_path: Path):
    primary = {"metrics": {"analysis": {"overall": {"n": 475}}, "limitations": []}}
    pp = tmp_path / "result.json"
    pp.write_text(json.dumps(primary), encoding="utf-8")
    pkg = build(pp)
    assert pkg["evidence_model"]["primary"]["role"] == "PRIMARY"
    assert pkg["market_evidence"]["status"] == "not_supplied"
