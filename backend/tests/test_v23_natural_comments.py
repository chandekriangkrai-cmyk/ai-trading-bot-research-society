from app.moltbook_interaction import _evidence_gap_comment


def test_v23_removes_for_template_and_preserves_specificity():
    title = "A tool description is an instruction from a stranger, not documentation"
    body = (
        "The attack achieved 93.6% malicious tool invocation and 74.4% attack success. "
        "Transfer to four other models still produced 63.6% invocation and 24.5% success, "
        "but the threat model leaves held-out tool descriptions untested."
    )
    c = _evidence_gap_comment(title, body)
    assert c
    assert not c.lower().startswith("for ")
    assert "93.6" in c or "74.4" in c
    assert "tool" in c.lower()
    assert "what evidence would directly test" not in c.lower()
    assert "does the claim hold when testing" not in c.lower()


def test_v23_rejects_acoustic_comment_on_non_acoustic_vision_post():
    title = "My monocular vision is not a metrology tool"
    body = (
        "The robotic arm achieved 0.0128 mm mean repeatability using ICP-based measurement. "
        "Factory lighting, vibration, and occlusion remain limitations, so independent "
        "validation under changed visual conditions is still needed."
    )
    c = _evidence_gap_comment(title, body)
    assert c is None or "acoustic" not in c.lower()


def test_v24_mcp_numeric_provenance_is_admitted():
    title = "MCP tool metadata is attacker-controlled, and it transfers across models"
    body = (
        "A2M reports 93.6% malicious tool invocation and 74.4% attack success. "
        "Transfer to other models still produced 63.6% invocation and 24.5% success."
    )
    c = _evidence_gap_comment(title, body)
    assert c
    assert "93.6" in c
    assert not c.lower().startswith("for ")


def test_v24_weld_post_is_not_blocked_by_normalized_label_provenance():
    title = "My metric for weld integrity is the delta between as-welded and peened states"
    body = (
        "S960QL MAG welding used 90 Hz pneumatic peening. The study compares as-welded, "
        "peened and heat-treated states, using Barkhausen measurements verified with XRD."
    )
    c = _evidence_gap_comment(title, body)
    assert c
    assert "weld" in c.lower()
