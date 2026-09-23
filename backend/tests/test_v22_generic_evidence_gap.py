from app.moltbook_interaction import _extract_evidence_gap, _evidence_gap_comment


def test_v22_admits_14c_research_rich_post():
    title = "Refining 14C yield functions for solar activity reconstructions"
    body = (
        "The Kovaltsov 14C model estimates modern production at 1.64 atoms/cm²/s, "
        "versus 1.88 in the pre-industrial reconstruction. Solar energetic particles "
        "contribute about 0.25% of the production budget. The older cosmic-ray spectra "
        "can bias the inferred production, although the carbon-cycle reservoir comparison "
        "shows agreement. Independent proxy validation is still needed to separate model "
        "bias from genuine solar variability."
    )
    gap = _extract_evidence_gap(title, body)
    assert gap is not None
    assert "1.64" in gap["evidence"] or "1.88" in gap["evidence"]
    comment = _evidence_gap_comment(title, body)
    assert comment is not None
    assert "14C" in comment


def test_v22_admits_elias_long_baseline_post():
    title = "A gap in a disk does not prove a planet exists"
    body = (
        "A 30 AU gap is consistent with a candidate companion, but the background-star "
        "alignment probability is only 0.015%. Keck, ALMA, and VLT observations in 2018 "
        "and 2020 show only 1.5° of orbital movement at 54.9 AU. The candidate could be "
        "1–4 Jupiter masses and 1300–1600 K, however there is no complete orbital solution. "
        "A longer orbital baseline is needed to establish whether the candidate follows a bound orbit."
    )
    gap = _extract_evidence_gap(title, body)
    assert gap is not None
    assert "0.015" in gap["evidence"] or "30" in gap["evidence"]
    comment = _evidence_gap_comment(title, body)
    assert comment is not None
    assert "planet" in comment.lower()


def test_v22_does_not_admit_single_number_slogan():
    title = "A result worth thinking about"
    body = "The result is 42. This is interesting."
    assert _extract_evidence_gap(title, body) is None
