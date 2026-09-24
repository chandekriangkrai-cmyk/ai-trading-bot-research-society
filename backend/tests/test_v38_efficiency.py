import inspect
import app.moltbook_interaction as mi

def test_v38_batch_size_is_four():
    src = inspect.getsource(mi.discover_and_analyze)
    assert 'MOLTBOOK_AI_BATCH_SIZE", "4"' in src

def test_v38_max_split_depth_is_one():
    src = inspect.getsource(mi._analyze_posts_batch_once)
    assert 'split_depth < 1' in src
    assert 'allow_split=False' in src

def test_v38_compact_output_budget():
    src = inspect.getsource(mi._ai_json_batch)
    assert '"620"' in src
    assert 'judge_reason <= 45' in src
