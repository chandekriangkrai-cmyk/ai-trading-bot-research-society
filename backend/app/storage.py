import os, json, shutil
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR","./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

def exp_dir(exp_id):
    p = DATA_DIR / exp_id
    p.mkdir(parents=True, exist_ok=True)
    return p

def state_path(exp_id):
    return exp_dir(exp_id) / "state.json"

def load_state(exp_id):
    p = state_path(exp_id)
    if not p.exists():
        return {"experiment_id":exp_id,"status":"missing"}
    return json.loads(p.read_text(encoding="utf-8"))

def save_state(exp_id, state):
    state_path(exp_id).write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

def copy_upload(src, exp_id, filename):
    dst = exp_dir(exp_id) / filename
    shutil.copyfile(src, dst)
    return str(dst)
