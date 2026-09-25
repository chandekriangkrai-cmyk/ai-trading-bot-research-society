import inspect
import app.moltbook_interaction as mi


def test_v42_3_batch_size_is_hard_capped_at_two():
    src = inspect.getsource(mi.discover_and_analyze)
    assert 'AI_BATCH_SIZE_MAX' in src
    assert mi.AI_BATCH_SIZE_MAX == 2


def test_v42_3_max_split_depth_is_one():
    src = inspect.getsource(mi._analyze_posts_batch_once)
    assert 'split_depth < 1' in src
    assert 'allow_split=False' in src


def test_v42_3_partial_json_salvage_exists():
    src = inspect.getsource(mi._ai_json_batch)
    assert 'Individual row extraction' in src
    assert 'finish_reason == "length"' in src
