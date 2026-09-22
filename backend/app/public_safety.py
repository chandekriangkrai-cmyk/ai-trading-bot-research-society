"""Public-output safety layer.

Keeps internal research identifiers and implementation metadata out of public
research posts/replies. This is deliberately conservative: internal storage
still retains the complete research result; only public-facing text is filtered.
"""
from __future__ import annotations
import os
import re
from typing import Any

# Keys that should never be rendered in public research text.
_INTERNAL_KEY_RE = re.compile(
    r"(?i)(?:experiment[_ -]?id|result[_ -]?id|post[_ -]?id|file[_ -]?id|upload[_ -]?id|job[_ -]?id|run[_ -]?id|"
    r"request[_ -]?id|task[_ -]?id|session[_ -]?id|trace[_ -]?id|internal[_ -]?id)"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9_.:/\\-]+[\"']?"
)

# Common infrastructure metadata that has no research value in a public post.
_INTERNAL_LABEL_RE = re.compile(
    r"(?i)\b(?:internal\s+id|database\s+id|api\s+response\s+id|experiment\s+id|post\s+id|"
    r"upload\s+id|job\s+id|run\s+id|request\s+id|session\s+id|file\s+id|trace\s+id)\b\s*[:=]\s*[^\n,;}]+"
)

# Absolute local/container paths should never leak into public discussion.
_PATH_RE = re.compile(r"(?:(?:/mnt|/tmp|/workspace|/app|[A-Za-z]:\\)[^\s\]\[\"']+)")

# API keys / bearer tokens. Do not try to be clever: redact obvious secrets.
_SECRET_RE = re.compile(
    r"(?i)\b(?:bearer\s+)?(?:sk-[A-Za-z0-9_-]{12,}|molt(?:dev|book)?_[A-Za-z0-9_-]{12,}|"
    r"api[_-]?key\s*[:=]\s*[A-Za-z0-9_.-]{12,})\b"
)

# Internal UUIDs are retained only in private storage. Public payloads remove
# experiment/result/post identifiers by key; text labels are scrubbed as well.


def _custom_secret_terms() -> list[str]:
    raw = os.getenv("PUBLIC_SECRET_TERMS", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def sanitize_public_text(text: Any) -> str:
    """Return text safe for public research posts/comments.

    The sanitizer removes internal identifiers and infrastructure metadata while
    leaving normal research statistics and the public experiment name intact.
    """
    s = "" if text is None else str(text)
    s = _INTERNAL_KEY_RE.sub("[redacted internal identifier]", s)
    s = _INTERNAL_LABEL_RE.sub("[redacted internal metadata]", s)
    s = _SECRET_RE.sub("[redacted secret]", s)
    s = _PATH_RE.sub("[redacted internal path]", s)
    for term in _custom_secret_terms():
        if len(term) >= 3:
            s = re.sub(re.escape(term), "[redacted proprietary term]", s, flags=re.IGNORECASE)
    return s


def sanitize_public_payload(value: Any) -> Any:
    """Recursively remove internal-id fields from a JSON-like public payload."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            if re.fullmatch(r"(?i)(experiment_id|result_id|post_id|file_id|upload_id|job_id|run_id|request_id|task_id|session_id|trace_id|internal_id)", key):
                continue
            out[key] = sanitize_public_payload(v)
        return out
    if isinstance(value, list):
        return [sanitize_public_payload(x) for x in value]
    if isinstance(value, str):
        return sanitize_public_text(value)
    return value


def public_ids_enabled() -> bool:
    return os.getenv("PUBLIC_EXPOSE_INTERNAL_IDS", "false").strip().lower() in {"1", "true", "yes", "on"}
