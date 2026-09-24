import inspect
import app.moltbook_interaction as mi


def test_v39_batch_size_is_hard_capped_at_four():
    src = inspect.getsource(mi.discover_and_analyze)
    assert 'min(4, max(1, int(os.getenv("MOLTBOOK_AI_BATCH_SIZE", "4"))))' in src


def test_v39_max_split_depth_is_one():
    src = inspect.getsource(mi._analyze_posts_batch_once)
    assert 'split_depth < 1' in src
    assert 'allow_split=False' in src


def test_v39_partial_json_salvage_exists():
    src = inspect.getsource(mi._ai_json_batch)
    assert 'Individual row extraction' in src
    assert 'finish_reason == "length"' in src
