from app.ai_first_v28 import source_domain_safe

def test_for_opener_rejected():
    assert not source_domain_safe("MCP tool descriptions","MCP tool-description injection reached 93.6% attack success.","For MCP, what evidence should be tested?")
def test_you_report_rejected():
    assert not source_domain_safe("Redis hash slots","Redis clusters distribute data across 16,384 hash slots.","You report Redis clusters distribute data across 16,384 hash slots. How would you validate this?")
def test_grounded_question_accepted():
    assert source_domain_safe("Redis hash slots","Redis clusters distribute data across 16,384 hash slots and MGET becomes fragmented by slot.","Does the 16,384-slot behavior still create the same MGET fragmentation when hash tags group the routing keys?")
def test_unanchored_rejected():
    assert not source_domain_safe("Solar activity","The reconstruction uses isotope records from tree rings.","Does the result generalize to unrelated datasets and models?")
