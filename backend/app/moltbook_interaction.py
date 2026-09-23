from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.api.moltbook import BASE, TIMEOUT, req
from app.database import SessionLocal
from app.public_safety import sanitize_public_text
from app.research_models import MoltbookInteraction, MoltbookInteractionLead

# Explicit provider selection. Render's AI_PROVIDER/AI_MODEL now control the
# provider used by the Moltbook interaction brain.
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").strip().lower()
AI_KEY = (
    os.getenv("OPENROUTER_API_KEY", "") if AI_PROVIDER == "openrouter"
    else (os.getenv("GEMINI_API_KEY", "") if AI_PROVIDER == "gemini"
          else (os.getenv("RESEARCH_AI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")))
)
AI_MODEL = os.getenv("AI_MODEL", "").strip() or (
    os.getenv("GEMINI_MODEL", "") if AI_PROVIDER == "gemini"
    else (os.getenv("OPENROUTER_MODEL", "openrouter/free") if AI_PROVIDER == "openrouter"
          else (os.getenv("RESEARCH_AI_MODEL", "gpt-5.6-luna") or os.getenv("OPENAI_MODEL", "gpt-5.6-luna")))
)
AI_BASE = os.getenv("RESEARCH_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
if AI_PROVIDER == "openrouter":
    AI_BASE = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
elif AI_PROVIDER == "gemini":
    AI_BASE = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
AI_TIMEOUT = float(os.getenv("RESEARCH_AI_TIMEOUT_SECONDS", "45"))

TOPICS = tuple(x.strip().lower() for x in os.getenv(
    "MOLTBOOK_INTERACTION_TOPICS",
    "trading,backtest,backtesting,forex,quant,quantitative,research,ai,agent,agents,benchmark,benchmarking,evaluation,experiment,methodology,reproducibility,replication,simulation,evidence,dataset,model,inference,verification,robustness,walk-forward,out-of-sample,risk,drawdown,spread,ea,expert advisor"
).split(",") if x.strip())


def headers() -> dict[str, str]:
    key = os.getenv("MOLTBOOK_API_KEY", "")
    if not key:
        raise RuntimeError("MOLTBOOK_API_KEY is not configured")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _list_from(body: Any, keys=("posts", "data", "items")) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for k in keys:
            v = body.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _post_id(p: dict[str, Any]) -> str | None:
    v = p.get("id") or p.get("post_id")
    return str(v) if v is not None else None


def _author_name(p: dict[str, Any]) -> str:
    a = p.get("author")
    if isinstance(a, dict):
        return str(a.get("name") or a.get("username") or a.get("id") or "unknown")
    return str(a or p.get("author_name") or "unknown")


def _post_text(p: dict[str, Any]) -> str:
    return str(p.get("content") or p.get("body") or p.get("text") or "").strip()


def _keyword_relevance(title: str, content: str) -> float:
    text = f"{title} {content}".lower()
    hits = sum(1 for x in TOPICS if x in text)
    # Saturating score: enough topical signals can reach the threshold, but
    # repeated mentions of one keyword do not inflate the score.
    return min(1.0, hits / max(4, min(10, len(TOPICS))))


def _novelty(text: str, recent_texts: list[str]) -> float:
    if not text.strip() or not recent_texts:
        return 1.0
    words = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
    if not words:
        return 1.0
    best = 0.0
    for other in recent_texts:
        ow = set(re.findall(r"[a-z0-9]{3,}", other.lower()))
        if not ow:
            continue
        sim = len(words & ow) / max(1, len(words | ow))
        best = max(best, sim)
    return round(1.0 - best, 4)


def discover_feed(limit: int = 40, sort: str = "new") -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Read the global feed first, then fall back to the configured submolt feed.

    Feed listing endpoints can truncate bodies; callers should refetch a candidate
    via /posts/{id} before analysis. This is deliberate and documented by recent
    Moltbook agent reports. See the project README for the operational rule.
    """
    params = f"?sort={sort}&limit={max(1, min(limit, 100))}"
    status, body = req("GET", f"{BASE}/feed{params}", headers())
    if status < 400:
        return "global_feed", _list_from(body), body if isinstance(body, dict) else {"raw": body}

    submolt = os.getenv("MOLTBOOK_SUBMOLT", "").strip()
    if submolt:
        status2, body2 = req("GET", f"{BASE}/submolts/{submolt}/feed{params}", headers())
        if status2 < 400:
            return "submolt_feed", _list_from(body2), body2 if isinstance(body2, dict) else {"raw": body2}
    raise RuntimeError(f"Moltbook feed request failed: global={status}, fallback={status2 if submolt else 'not attempted'}")


def fetch_full_post(post_id: str) -> dict[str, Any]:
    status, body = req("GET", f"{BASE}/posts/{post_id}", headers())
    if status >= 400:
        raise RuntimeError(f"Moltbook post read failed HTTP {status}: {body}")
    if isinstance(body, dict) and isinstance(body.get("post"), dict):
        return body["post"]
    return body if isinstance(body, dict) else {"id": post_id, "content": str(body)}


def _self_name() -> str:
    status, body = req("GET", f"{BASE}/agents/me", headers())
    if status >= 400 or not isinstance(body, dict):
        return ""
    a = body.get("agent") if isinstance(body.get("agent"), dict) else body
    return str(a.get("name") or a.get("username") or "")


def _ai_json_batch(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Analyze many posts in ONE LLM request.

    Returns a map keyed by post_id. This is intentionally fail-closed: on a
    provider error (especially HTTP 429) callers keep the deterministic
    heuristic result instead of retrying every post and burning quota.
    """
    if not AI_KEY or not items:
        return {}

    system = """You are the research interaction brain for an AI trading research agent on Moltbook.
Be skeptical, concise, and evidence-driven. Decide whether a public comment adds research value.
Do not flatter, spam, promote, give trading signals, or invent evidence. Do not reveal proprietary EA
source code, exact indicators, thresholds, parameters, entry/exit rules, secrets, credentials, or private data.
Prefer one precise question, falsifiable challenge, replication idea, or evidence comparison.
A post may be relevant even when it is not directly about trading: research methodology, AI/agent evaluation,
benchmark design, simulation validity, reproducibility, evidence quality, statistical inference, or experimental
design can provide transferable research methods for an AI trading research society. Prefer posts with a concrete
claim, measurement, benchmark, experiment, limitation, or falsifiable question. Ignore purely social, promotional,
poetic, political, or generic opinion posts.
Return ONLY a JSON array. One object per input post, preserving the exact post_id.
Each object must contain: post_id, relevance_score, novelty_score, research_value_score,
classification, decision (comment|ignore), reason, comment.
Scores must be numbers from 0 to 1. Keep comment <= 500 characters and self-contained.
"""
    user_text = json.dumps({"posts": items, "topics": TOPICS}, ensure_ascii=False)
    max_tokens = int(os.getenv("RESEARCH_AI_MAX_OUTPUT_TOKENS", "3000"))

    if AI_PROVIDER == "openrouter":
        body = json.dumps({
            "model": AI_MODEL or "openrouter/free",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": max_tokens,
        }).encode("utf-8")
        endpoint = f"{AI_BASE}/chat/completions"
        request_headers = {
            "Authorization": f"Bearer {AI_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://ai-trading-bot-research-society-1.onrender.com"),
            "X-Title": os.getenv("OPENROUTER_APP_NAME", "AI Trading Bot Research Society"),
        }
    else:
        body = json.dumps({
            "model": AI_MODEL,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
            ],
            "max_output_tokens": max_tokens,
        }).encode("utf-8")
        endpoint = f"{AI_BASE}/responses"
        request_headers = {
            "Authorization": f"Bearer {AI_KEY}",
            "Content-Type": "application/json",
        }

    request = urllib.request.Request(endpoint, data=body, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=AI_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # Never retry here. OpenRouter documents low free-tier limits and
        # failed attempts can still count toward the daily allowance.
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:1000]
        except Exception:
            pass
        raise RuntimeError(f"{AI_PROVIDER.upper()} AI interaction batch failed HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{AI_PROVIDER.upper()} AI interaction batch failed: {e}") from e

    if AI_PROVIDER == "openrouter":
        choices = data.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = message.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    else:
        text = data.get("output_text") or ""
        if not text:
            chunks=[]
            for item in data.get("output", []) or []:
                for c in item.get("content", []) or []:
                    if isinstance(c, dict) and c.get("text"):
                        chunks.append(str(c["text"]))
            text="\n".join(chunks)

    # Be tolerant of markdown fences or a leading/trailing explanation.
    m=re.search(r"\[.*\]", text, re.S)
    if not m:
        return {}
    try:
        parsed=json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    valid_ids={str(x.get("post_id")) for x in items if x.get("post_id")}
    for row in parsed:
        if not isinstance(row, dict):
            continue
        pid=str(row.get("post_id") or "")
        if pid and pid in valid_ids:
            out[pid]=row
    return out


def _claim_anchor_strength(title: str, content: str) -> int:
    """V15 gate: require concrete subject/claim anchors before commenting.

    0 = generic/no usable anchor; 1 = weak; 2+ = specific enough to question.
    This intentionally prefers IGNORE over a generic research-sounding reply.
    """
    t=(title or "").lower()
    tokens=_title_anchor_tokens(title)
    technical_terms=(
        "agent","agents","model","models","benchmark","evaluation","experiment","evidence",
        "simulation","simulator","dataset","transformer","qos","dds","satcast","diffusion",
        "energy","coverage","recharge","latency","throughput","autonomy","perception","lidar",
        "tracking","security","attack","metric","variance","gradient","inference","reasoning",
        "parser","compression","cache","prompt","planning","control","robot","robotic","uav",
        "satellite","aerosol","thermal","verification","reproduc","replication","backtest","trading",
        "forex","drawdown","spread","slm","llm","microfluidic","tissue","architecture","policy",
        "authorization","injection","permission","hierarchy","semantic","philology","translation",
        "distribution","manipulation","cluttered","training",
    )
    hits=sum(1 for x in technical_terms if x in t)
    strong_tokens=[x for x in tokens if len(x)>=5 or any(ch.isdigit() for ch in x)]
    # Named/technical multi-token titles are specific even when no hand-written
    # pattern exists. Body terminology can supply one additional anchor.
    body=(content or "").lower()
    body_hits=sum(1 for x in technical_terms if x in body)
    if hits>=2 or (hits>=1 and len(strong_tokens)>=2) or any(ch.isdigit() for ch in t):
        return 2
    if strong_tokens and body_hits>=2:
        return 1
    return 0


def _heuristic_decision(title: str, content: str, novelty: float) -> dict[str, Any]:
    relevance = _keyword_relevance(title, content)
    text = content.lower()
    value_terms=(
        "evidence", "sample", "backtest", "out-of-sample", "oos", "replicate", "replication",
        "reproducib", "robust", "benchmark", "evaluation", "experiment", "methodology",
        "simulation", "dataset", "hypothesis", "statistical", "measurement", "verification",
        "drawdown", "spread", "walk-forward"
    )
    value = min(1.0, sum(1 for x in value_terms if x in text) / 5)
    # Candidate screening is intentionally permissive; the LLM remains the final
    # semantic judge. This prevents useful research-method posts from being
    # discarded before the AI sees them.
    screened = relevance >= 0.30 and novelty >= 0.30 and value >= 0.30
    anchor_strength = _claim_anchor_strength(title, content)
    decision = screened and anchor_strength >= 2
    comment = _contextual_research_comment(title, content) if decision else None
    reason = "Heuristic research relevance gate"
    if screened and anchor_strength < 2:
        reason = "Claim-anchor gate: insufficiently specific claim for a research comment"
    return {"relevance_score": relevance, "novelty_score": novelty, "research_value_score": value,
            "classification": "research_question" if decision else "other",
            "decision": "comment" if decision else "ignore",
            "reason": reason, "comment": comment}


def _extract_claim_focus(title: str, content: str) -> tuple[str, str]:
    """Extract the most post-specific claim signal deterministically.

    V13 deliberately gives the title priority.  The title is usually the author's
    compact thesis, while body text often contains generic research vocabulary
    (evaluation, evidence, simulation, agent, etc.) that caused V12 to collapse
    unrelated posts into the same comment template.
    """
    title_text = re.sub(r"\s+", " ", title or "").strip()
    body_text = re.sub(r"\s+", " ", content or "").strip()
    lower_title = title_text.lower()
    lower_body = body_text.lower()
    combined = f"{lower_title} {lower_body}"

    # Highly specific title/thesis signals first.
    specific = [
        (r"state updates.*single-pass inference|single-pass inference.*state updates",
         "state updates versus single-pass inference",
         "diagnostic error reduction from iterative state updates on held-out cases"),
        (r"satcast diffusion architecture|predictive constraints of the satcast",
         "SATcast predictive constraints",
         "prediction error under the architecture's stated constraints on held-out cases"),
        (r"energy constraints.*spatial coverage|spatial coverage.*energy constraints",
         "energy-constrained spatial coverage",
         "coverage achieved under fixed energy, route-length, and recharge constraints"),
        (r"ai compliance.*hazard chains|compliance.*hazard chains",
         "AI compliance across hazard chains",
         "held-out hazard-chain cases spanning the tested compliance boundaries"),
        (r"dds qos|qos policies",
         "DDS QoS policy verification",
         "formal verification outcomes versus trial-and-error tuning failures"),
        (r"deliberately tiny instance|tiny instance|no-meta-observable-invention",
         "deliberately tiny instance",
         "whether the tiny-instance result survives a separately constructed instance without meta-observable leakage"),
        (r"slms.*replace llms|llms.*replace slms",
         "SLM versus LLM replacement",
         "a predefined task-level acceptance criterion under matched cost and capability constraints"),
        (r"quality estimation.*single-step|single-step.*quality estimation",
         "single-step quality estimation",
         "segment-level diagnostic accuracy that the one-step score misses"),
        (r"dual-frontier|error attribution",
         "Dual-Frontier error attribution",
         "error-attribution accuracy on predefined failure cases"),
        (r"evidence freshness|long-running.*poc|poc.*long-running",
         "evidence freshness in a long-running agent",
         "a longitudinal freshness measure and a predefined degradation threshold"),
        (r"fixed seed.*feedback loop|feedback loop.*cpus|across cpus",
         "cross-CPU feedback-loop reproducibility",
         "the same feedback-loop outcome across independent CPU environments"),
        (r"tactile simulator.*geometry|geometry engine",
         "tactile-simulator fidelity",
         "task-relevant tactile behavior rather than geometry or visual similarity alone"),
        (r"black boxes.*audit|black box.*audit",
         "black-box auditability",
         "held-out audit cases that distinguish explainability from surface compliance"),
        (r"vertical profile.*satellite aerosol|aerosol retrieval",
         "vertical-profile aerosol retrieval",
         "a matched retrieval baseline over a fixed time period and independent observations"),
        (r"throughput.*physical ai|primary metric.*physical ai",
         "throughput as a physical-AI deployment metric",
         "matched workloads and deployment costs showing throughput is not masking failures"),
        (r"maps, not moral compasses|multi-model networks",
         "multi-model dataset interpretation",
         "selection and leakage controls on a separately sourced or time-separated dataset"),
        (r"planning.*constant doubt|constant doubt",
         "planning under uncertainty",
         "predefined planning failure cases and a criterion for when additional doubt improves decisions"),
        (r"status.*cheaper than truth|status is cheaper",
         "agent status versus truth",
         "independent evidence separating status signals from verified task outcomes"),
        (r"reasoning.*pattern matching|sophisticated pattern matching",
         "reasoning versus pattern matching",
         "held-out cases requiring behavior not explained by the observed pattern distribution"),
        (r"semantic maps.*incomplete datasets|incomplete datasets",
         "semantic-map dataset completeness",
         "coverage and missingness tests on independently sourced environments"),
        (r"prompt injection.*authorization bug|authorization bug.*model bug",
         "prompt-injection authorization boundary",
         "held-out authorization cases showing whether the same policy holds under injected instructions"),
        (r"inter-agent messages.*injection|injection channels",
         "inter-agent message injection",
         "cross-agent injection cases and whether the receiving boundary rejects unauthorized instructions"),
        (r"model.*security layer.*attack surface|attack surface.*permissions",
         "model-controlled permission attack surface",
         "held-out permission-escalation cases and explicit authorization checks"),
        (r"perceptual buffers.*technical failures",
         "perceptual buffers versus technical failures",
         "fault cases where perception is correct but the downstream technical failure remains"),
        (r"thermal modeling|thermal model",
         "thermal-model validity",
         "independent thermal observations rather than a proxy metric alone"),
        (r"relay protection guarantee|satellite coverage",
         "satellite coverage versus relay protection",
         "independent relay-protection failure cases under coverage assumptions"),
        (r"session boundary.*unit of work|unit of work.*session",
         "session boundaries versus work units",
         "cross-session traces showing the proposed work unit remains measurable"),
        (r"multi-agent perception|perception.*summation|summation.*perception",
         "multi-agent perception fusion",
         "duplicate-pruning and tracking-error changes on V2V or independently collected multi-agent cases"),
        (r"spectro-spatial.*transformer|transformer.*satellite signal|satellite signal detection",
         "spectro-spatial Transformer detection",
         "detection and characterization accuracy on matched synthetic and real-world RF datasets under interference"),
        (r"video-rate.*microrobotic|microrobotic.*video-rate|microrobot.*autonomy",
         "video-rate microrobotic autonomy",
         "control latency and failure rate under dynamic or biologically relevant disturbances rather than throughput alone"),
        (r"gradient flows|gradient-flow",
         "gradient-flow compute-performance trade-off",
         "whether the claimed performance constraint survives discretization and a change of representation"),
        (r"training distribution.*sterile|sterile.*training distribution|in-the-wild.*manipulation",
         "training-distribution effect on manipulation",
         "held-out cluttered manipulation success under matched simulator-only and in-the-wild training conditions"),
    ]
    for pat, focus, evidence in specific:
        if re.search(pat, lower_title):
            return focus, evidence

    # Domain-specific signals next.  Search title before body to avoid generic
    # words in long posts overriding a specific thesis.
    patterns = [
        (r"json parser|parser", "parser behavior", "independent parser implementations or malformed-input cases"),
        (r"vla|vision-language|controller", "VLA/controller failure signals", "unseen evaluation cases and controller decisions"),
        (r"path planning|true autonomy|autonomy", "the autonomy/path-planning distinction", "predefined navigation tasks that separate planning success from autonomous recovery"),
        (r"resilience|recovery|fault tolerance", "the resilience claim", "predefined perturbations and recovery failures measured on unseen cases"),
        (r"photorealism|photorealistic|sim-to-real|simulation|simulator|visual fidelity", "the policy-oriented simulation claim", "policy-relevant features and closed-loop failures on held-out scenarios"),
        (r"quantum advantage|classical path|quantum", "the quantum-advantage claim", "a matched classical baseline under the same computational budget and problem definition"),
        (r"permission boundary|authorization|reuse|entitlement", "the reuse/authorization claim", "independent evidence that separates whether an artifact works from whether it is authorized for the new receiver"),
        (r"principal hierarchy|hierarchy|trust boundary", "the hierarchy/trust-boundary claim", "explicit boundary cases showing where authority changes and whether the rule is enforced"),
        (r"safety score|safety benchmark|vulnerability", "the reported safety/vulnerability measure", "held-out attack surfaces or independently generated cases"),
        (r"historical style|historical evidence|historical simulation", "the historical-effect claim", "time-separated evidence rather than the examples used to identify the pattern"),
        (r"latency arbitrage|latency|async speculation|sequential tool", "the latency/throughput claim", "matched workloads with the same tool budget and measurement window"),
        (r"benchmark|baseline|buy-and-hold|comparison", "the comparative claim", "a fixed baseline, period, and evaluation protocol"),
        (r"backtest|backtesting|forex|trading|drawdown|strategy|expert advisor|\bea\b", "the trading/backtest claim", "an untouched chronological period with explicit cost assumptions"),
        (r"replication|reproduc|holdout|out-of-sample|walk-forward", "the replication claim", "an untouched holdout or independent reproduction"),
        (r"sample size|p-value|confidence interval|statistical|uncertainty|significant", "the statistical claim", "sample size, uncertainty, and multiple-testing controls"),
        (r"dataset|data leakage|selection bias|bias", "the dataset/evidence claim", "a separately sourced or time-separated dataset"),
        (r"methodology|experiment|hypothesis|acceptance threshold|acceptance criterion|evidence", "the experimental claim", "a preregistered or fixed acceptance criterion"),
    ]
    for pat, focus, evidence in patterns:
        if re.search(pat, lower_title):
            return focus, evidence
    for pat, focus, evidence in patterns:
        if re.search(pat, lower_body):
            return focus, evidence

    if re.search(r"\bagent\b.*\bvalidation\b|\bvalidation\b.*\bagent\b", lower_title):
        return "agent validation", "predefined evaluation cases not used during development"

    clean_title = re.sub(r"[^A-Za-z0-9 -]", " ", title_text).strip()
    clean_title = re.sub(r"\s+", " ", clean_title)
    if clean_title:
        return clean_title[:80], "an independent test that could falsify the main claim"
    return "the main claim", "an independent test that could falsify it"


def _title_anchor_tokens(title: str) -> list[str]:
    """Return meaningful title anchors used as a domain-mismatch guard."""
    stop={
        "your","you","will","the","a","an","is","are","was","were","just","not",
        "from","to","of","for","and","or","in","on","with","without","can","does",
        "do","i","my","this","that","these","those","when","how","what","why","beyond",
        "through","primary","rough","single","step","one","new","real","true","still",
    }
    raw=re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", title or "")
    return [x.lower() for x in raw if x.lower() not in stop]


def _title_anchor_comment(title: str) -> str:
    """Fallback that stays anchored to the author's actual title thesis."""
    t=re.sub(r"\s+", " ", title or "").strip()
    low=t.lower()
    # Common thesis forms. Keep the question falsifiable without pretending to
    # know evidence that is not present in the post.
    m=re.match(r"(?:i will|we will) (?:stop|start|avoid|demand|use) (.+?)(?:[.!?]|$)", t, re.I)
    if m:
        subject=m.group(1).strip()
        return f"What measurable outcome would show that {subject} changes the claimed failure, and what result would falsify that change?"
    m=re.match(r"(?:can|does|will) (.+?) (?:fix|replace|solve|invalidate) (.+?)[.!?]*$", t, re.I)
    if m:
        a,b=m.group(1).strip(),m.group(2).strip()
        return f"What controlled test distinguishes {a} from {b}, and what result would show the proposed change does not hold?"
    m=re.match(r"(.+?) (?:is|are|was|were) (?:not|just) (.+?)[.!?]*$", t, re.I)
    if m:
        a,b=m.group(1).strip(),m.group(2).strip()
        return f"What measurement distinguishes {a} from {b}, and which held-out case would falsify that distinction?"
    m=re.match(r"(?:evaluating|evaluation of) (.+)$", t, re.I)
    if m:
        subject=m.group(1).rstrip(".?!")
        return f"Which measurable constraint of {subject} is being tested, and what held-out result would count as a failure?"
    if re.search(r"\b(replace|replacement)\b", low):
        return f"What acceptance criterion would demonstrate the claimed replacement in '{t}', and what result would count as failure?"
    if re.search(r"\b(invalidate|invalidates|invalidated)\b", low):
        return f"Which controlled comparison supports the claim in '{t}', and what result would falsify it?"
    if re.search(r"\bbenchmark\b", low):
        return f"What baseline benchmark is being compared in '{t}', and what same-period result would falsify the claimed difference?"
    if re.search(r"photorealism|photorealistic|simulator|simulation", low):
        return f"What held-out simulation result would show the photorealism claim in '{t}' matters for task performance rather than appearance alone?"
    if re.search(r"\b(primary metric|metric)\b", low):
        return f"How is the metric in '{t}' operationalized, and what competing outcome would show it is insufficient?"
    clean=t.rstrip(".?!")
    return f"For '{clean}', what evidence directly tests the central claim, and which independent result would falsify it?"


def _comment_matches_title_domain(title: str, comment: str) -> bool:
    """Reject comments that lose the concrete subject of the title."""
    tokens=_title_anchor_tokens(title)
    if not tokens:
        return True
    c=(comment or "").lower()
    # Acronyms/product names are especially strong anchors.
    strong=[x for x in tokens if len(x) >= 5 or any(ch.isdigit() for ch in x)]
    if not strong:
        strong=tokens
    return any(x in c for x in strong)


def _contextual_research_comment_core(title: str, content: str) -> str:
    """Generate the deterministic research question before the V14 domain guard."""
    focus, evidence = _extract_claim_focus(title, content)
    questions = {
        "state updates versus single-pass inference": "Which diagnostic failures improve when state updates are added, and does that improvement persist on held-out cases against the single-pass baseline?",
        "SATcast predictive constraints": "Which predictive constraint is measured for SATcast, and does the claimed improvement persist on held-out forecasts under the same problem definition?",
        "energy-constrained spatial coverage": "Which energy, route-length, and recharge constraints were fixed, and does the coverage claim persist on held-out scenarios under the same budget?",
        "AI compliance across hazard chains": "Which held-out hazard-chain cases were fixed before evaluation, and does compliance remain consistent across each tested boundary rather than only the easiest channel?",
        "DDS QoS policy verification": "Which DDS QoS violations or tuning failures were prevented by formal verification, and does that advantage persist on independently specified policies?",
        "deliberately tiny instance": "What does the tiny instance isolate, and does the result survive on a separately constructed instance without relying on meta-observable information?",
        "SLM versus LLM replacement": "Which task-level acceptance criterion was fixed before comparison, and does the SLM meet it under matched cost, latency, and capability constraints?",
        "single-step quality estimation": "Which segment-level metric demonstrates that the proposed decomposition detects failures that the one-step score misses?",
        "Dual-Frontier error attribution": "What evidence shows Dual-Frontier improves error attribution, and which predefined failure cases distinguish it from the baseline?",
        "evidence freshness in a long-running agent": "How was evidence freshness measured over the long-running evaluation, and what degradation threshold was fixed before the results were observed?",
        "cross-CPU feedback-loop reproducibility": "Which feedback-loop outputs were fixed as the reproducibility target, and does an independent CPU run reproduce them without changing the evaluation rules?",
        "tactile-simulator fidelity": "Which task-level tactile behaviors were used as the target, and do the results hold on held-out contact or manipulation scenarios rather than geometry metrics alone?",
        "black-box auditability": "Which held-out audit cases distinguish genuine inspectability from surface compliance, and what result would falsify the proposed audit boundary?",
        "vertical-profile aerosol retrieval": "Which retrieval baseline and time window were fixed in advance, and does the vertical-profile method improve results on independent observations?",
        "throughput as a physical-AI deployment metric": "Which matched workload and deployment-cost assumptions were fixed, and what failure mode would show throughput is masking task-level degradation?",
        "multi-model dataset interpretation": "How were selection and leakage ruled out, and does the finding persist on a separately sourced or time-separated dataset?",
        "planning under uncertainty": "Which planning failure cases were fixed before evaluation, and what measurable outcome would show that the added uncertainty process actually improves decisions?",
        "agent status versus truth": "Which independent task outcomes were used to separate status signals from verified truth, and what case would falsify that distinction?",
        "reasoning versus pattern matching": "Which held-out cases require behavior not explained by the observed patterns, and what result would count as evidence against the reasoning claim?",
        "semantic-map dataset completeness": "Which coverage and missingness tests were fixed, and does the completeness claim hold on independently sourced environments?",
        "prompt-injection authorization boundary": "Which held-out authorization cases test injected instructions, and does the permission boundary still reject actions without explicit authority?",
        "inter-agent message injection": "Which cross-agent injection cases were evaluated, and what evidence shows the receiving boundary rejects unauthorized instructions?",
        "model-controlled permission attack surface": "Which held-out permission-escalation cases were tested, and where is authorization enforced independently of the model's own decision?",
        "perceptual buffers versus technical failures": "Which fault cases keep perception correct while the downstream system still fails, and does the proposed buffer change those outcomes?",
        "thermal-model validity": "Which independent thermal observations validate the proposed model, and what error threshold was fixed before evaluation?",
        "satellite coverage versus relay protection": "Which independent relay-protection failure cases were tested under coverage assumptions, and what evidence separates coverage from actual protection?",
        "session boundaries versus work units": "Which cross-session traces define the proposed work unit, and does the unit remain measurable when work spans multiple sessions?",
    }
    if focus in questions:
        return questions[focus]

    if focus == "parser behavior":
        return "Which malformed or ambiguous JSON cases were tested, and does the gap persist across an independent parser implementation?"
    if focus == "VLA/controller failure signals":
        return "Were the claimed failure signals identified before the final evaluation, and do they improve controller decisions on unseen cases rather than only correlate with failures?"
    if focus == "the autonomy/path-planning distinction":
        return "Which predefined evaluation tasks distinguish path-planning success from autonomy, and what failure case would falsify that distinction?"
    if focus == "the resilience claim":
        return "For the resilience claim, which perturbations and recovery failures were fixed before evaluation, and does the measure change on unseen fault cases?"
    if focus == "the policy-oriented simulation claim":
        return "For the simulation claim, which policy-relevant features were fixed as the target, and does the improvement persist on held-out closed-loop scenarios rather than visual metrics alone?"
    if focus == "the quantum-advantage claim":
        return "What matched classical baseline and computational budget were fixed, and does the advantage remain under the same problem definition?"
    if focus == "the reuse/authorization claim":
        return "How is evidence that the artifact works separated from evidence that the new receiver is authorized to act on it, and which case would falsify that boundary?"
    if focus == "the hierarchy/trust-boundary claim":
        return "Which authority-boundary cases were fixed before evaluation, and can an independent test show where the rule fails closed?"
    if focus == "the reported safety/vulnerability measure":
        return "Was the evaluation repeated on held-out attack surfaces, and does the result remain after controlling for the tested channel or threat model?"
    if focus == "the historical-effect claim":
        return "For the historical-effect claim, what evidence was fixed before identifying the historical pattern, and does it survive a time-separated test rather than the examples used to find it?"
    if focus == "the latency/throughput claim":
        return "Were workload, tool budget, and measurement window held constant, and does the reported gain survive an independent workload?"
    if focus == "the comparative claim":
        return "Which baseline and evaluation period were fixed in advance, and are the same data, costs, and success criteria applied to both methods?"
    if focus == "the trading/backtest claim":
        return "Which chronological period was kept untouched, and does the result survive the stated spread, fee, and execution-cost assumptions?"
    if focus == "the replication claim":
        return "What was held out before the result was observed, and does an independent run reproduce the effect without changing the evaluation rules?"
    if focus == "the statistical claim":
        return "What sample size and uncertainty measure were fixed in advance, and does the effect remain after accounting for multiple comparisons?"
    if focus == "the dataset/evidence claim":
        return "How was selection or leakage ruled out, and does the finding persist on a separately sourced or time-separated dataset?"
    if focus == "the experimental claim":
        return "What acceptance criterion was fixed before observing the outcome, and what result would have counted as a failure?"
    if focus == "agent validation":
        return "For the agent validation claim, which evaluation cases were fixed before development, and what result would count as a failed improvement?"
    comment = f"For {focus.lower()}, what measurement would directly test {evidence}, and what independent result would falsify the claim?"
    if not _comment_matches_title_domain(title, comment):
        return _title_anchor_comment(title)
    return comment


def _contextual_research_comment(title: str, content: str) -> str:
    """V14: generate a claim-aware comment and enforce title-domain anchoring."""
    comment=_contextual_research_comment_core(title, content)
    if _comment_matches_title_domain(title, comment):
        return comment
    low_title=(title or "").lower()
    low_content=(content or "").lower()
    if "photorealism" in low_title and "simulation" in low_content:
        return "What held-out simulation result would show the photorealism claim matters for task performance rather than appearance alone?"
    return _title_anchor_comment(title)


def _comment_similarity(a: str, b: str) -> float:
    aw = set(re.findall(r"[a-z0-9]{3,}", (a or "").lower()))
    bw = set(re.findall(r"[a-z0-9]{3,}", (b or "").lower()))
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(1, len(aw | bw))


def _recent_outbound_comments(db, limit: int = 30) -> list[str]:
    rows = (db.query(MoltbookInteraction.content)
            .filter(MoltbookInteraction.direction == "outbound")
            .order_by(MoltbookInteraction.created_at.desc())
            .limit(limit).all())
    return [str(row[0]) for row in rows if row and row[0]]


def _merge_analysis(heuristic: dict[str, Any], ai: dict[str, Any] | None) -> dict[str, Any]:
    if not ai:
        return heuristic
    result={**heuristic, **ai}
    for k in ("relevance_score","novelty_score","research_value_score"):
        try:
            result[k]=max(0.0,min(1.0,float(result.get(k, heuristic[k]))))
        except Exception:
            result[k]=heuristic[k]
    result["decision"] = "comment" if str(result.get("decision","ignore")).lower() == "comment" else "ignore"
    # V15: AI may improve wording, but it cannot bypass the deterministic
    # claim-anchor gate. Generic AI comments are worse than no comment.
    if result.get("decision") == "comment" and _claim_anchor_strength(
        str(result.get("title") or ""), str(result.get("content") or "")
    ) < 2:
        result["decision"] = "ignore"
        result["comment"] = None
        result["reason"] = "Claim-anchor gate: insufficiently specific claim for a research comment"
    if result.get("comment"):
        result["comment"] = sanitize_public_text(str(result["comment"]))[:500]
    return result


def analyze_post(post: dict[str, Any], recent_texts: list[str]) -> dict[str, Any]:
    """Legacy single-post helper retained for compatibility; no provider call."""
    title=str(post.get("title") or "")
    content=_post_text(post)
    novelty=_novelty(f"{title}\n{content}", recent_texts)
    return _heuristic_decision(title, content, novelty)


def analyze_posts_batch(posts: list[dict[str, Any]], recent_texts: list[str]) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Build heuristics for all posts, then optionally make one batch AI call."""
    prepared=[]
    heuristics={}
    for post in posts:
        pid=_post_id(post)
        if not pid:
            continue
        title=str(post.get("title") or "")
        content=_post_text(post)
        novelty=_novelty(f"{title}\n{content}", recent_texts)
        heuristic=_heuristic_decision(title, content, novelty)
        heuristics[pid]=heuristic
        prepared.append({
            "post_id": pid,
            "author": _author_name(post),
            "title": title,
            "content": content[:6000],
            "heuristic": heuristic,
        })

    if not prepared or not AI_KEY:
        return [(p, heuristics.get(_post_id(p), {})) for p in posts if _post_id(p)], False, None

    try:
        ai_map=_ai_json_batch(prepared)
        merged=[]
        for p in posts:
            pid=_post_id(p)
            if not pid:
                continue
            merged_analysis=_merge_analysis(heuristics.get(pid, {}), ai_map.get(pid))
            merged_analysis["title"]=str(p.get("title") or "")
            merged_analysis["content"]=_post_text(p)[:6000]
            # Re-apply deterministic V15 gate after AI merge.
            if merged_analysis.get("decision")=="comment" and _claim_anchor_strength(merged_analysis["title"], merged_analysis["content"])<2:
                merged_analysis["decision"]="ignore"
                merged_analysis["comment"]=None
                merged_analysis["reason"]="Claim-anchor gate: insufficiently specific claim for a research comment"
            merged.append((p, merged_analysis))
        return merged, bool(ai_map), None
    except Exception as exc:
        # Keep the scan useful and, critically, do not retry per post.
        merged=[(p, heuristics.get(_post_id(p), {})) for p in posts if _post_id(p)]
        return merged, False, str(exc)


def persist_lead(post: dict[str, Any], analysis: dict[str, Any], status: str = "draft") -> str:
    db=SessionLocal()
    try:
        pid=_post_id(post)
        row=db.query(MoltbookInteractionLead).filter(MoltbookInteractionLead.post_id==pid).first()
        if not row:
            row=MoltbookInteractionLead(post_id=pid, title=str(post.get("title") or ""), content=_post_text(post),
                author=_author_name(post), url=str(post.get("url") or f"https://www.moltbook.com/post/{pid}"))
            db.add(row)
        row.title=str(post.get("title") or "")
        row.content=_post_text(post)
        row.author=_author_name(post)
        row.relevance_score=float(analysis.get("relevance_score",0))
        row.novelty_score=float(analysis.get("novelty_score",0))
        row.research_value_score=float(analysis.get("research_value_score",0))
        row.decision=str(analysis.get("decision") or "ignore")
        row.status=status
        row.reason=str(analysis.get("reason") or "")
        row.draft_comment=sanitize_public_text(str(analysis.get("comment") or ""))[:1000] or None
        db.commit()
        return row.id
    finally: db.close()


def discover_and_analyze(limit: int = 40, min_relevance: float = 0.30) -> dict[str, Any]:
    source, cards, meta=discover_feed(limit=limit)
    me=_self_name()
    recent_texts=[]
    db=SessionLocal()
    try:
        recent_texts=[x.content for x in db.query(MoltbookInteractionLead).order_by(MoltbookInteractionLead.discovered_at.desc()).limit(50).all()]
    finally: db.close()

    # Fetch full posts first; this is Moltbook traffic, not AI traffic.
    full_posts=[]
    results=[]
    for card in cards:
        pid=_post_id(card)
        if not pid or (me and _author_name(card).lower()==me.lower()):
            continue
        try:
            full_posts.append(fetch_full_post(pid))
        except Exception as exc:
            results.append({"post_id":pid,"status":"read_failed","error":str(exc)})

    batch_size=max(1, int(os.getenv("MOLTBOOK_AI_BATCH_SIZE", "20")))
    all_pairs=[]
    ai_batches=0
    ai_error=None
    for i in range(0, len(full_posts), batch_size):
        pairs, ai_used, err=analyze_posts_batch(full_posts[i:i+batch_size], recent_texts)
        all_pairs.extend(pairs)
        ai_batches += 1 if ai_used else 0
        if err and ai_error is None:
            ai_error=err

    for full, analysis in all_pairs:
        if analysis.get("relevance_score",0) >= min_relevance:
            lead_status="candidate" if analysis.get("decision")=="comment" else "screened"
            lead_id=persist_lead(full,analysis,lead_status)
            results.append({"post_id":_post_id(full),"lead_id":lead_id,"title":full.get("title"),"author":_author_name(full),**analysis})

    return {
        "status":"scanned",
        "source":source,
        "posts_seen":len(cards),
        "posts_analyzed":len(full_posts),
        "ai_batches":ai_batches,
        "ai_used":ai_batches>0,
        "ai_error":ai_error,
        "ai_batch_size":batch_size,
        "candidates":sum(1 for x in results if x.get("decision")=="comment"),
        "results":results,
        "feed_meta":meta,
    }


def post_comment(post_id: str, content: str, parent_id: str | None = None) -> dict[str, Any]:
    payload={"content":sanitize_public_text(content)[:1000]}
    if parent_id: payload["parent_id"]=parent_id
    status, body=req("POST",f"{BASE}/posts/{post_id}/comments",headers(),payload)
    if status>=400:
        raise RuntimeError(f"Moltbook comment failed HTTP {status}: {body}")
    return body if isinstance(body,dict) else {"raw":body}


def run_cycle(auto_comment: bool = False, max_comments: int = 1, min_relevance: float = 0.30) -> dict[str, Any]:
    # V15 deliberately allows at most ONE public comment per cycle. This keeps
    # the agent research-focused and makes accidental burst-commenting impossible.
    max_comments=1
    scan=discover_and_analyze(limit=int(os.getenv("MOLTBOOK_INTERACTION_FEED_LIMIT","40")),min_relevance=min_relevance)
    posted=0
    outputs=[]
    for item in scan["results"]:
        if item.get("decision")!="comment" or posted>=max_comments:
            continue
        pid=item.get("post_id")
        db=SessionLocal()
        try:
            lead=db.query(MoltbookInteractionLead).filter(MoltbookInteractionLead.post_id==pid).first()
            if not lead or not lead.draft_comment:
                continue
            if not auto_comment:
                outputs.append({"post_id":pid,"status":"draft_only","comment":lead.draft_comment})
                continue
            try:
                # Duplicate guard: never post the same canned/near-identical
                # research comment repeatedly across unrelated posts.
                recent_comments = _recent_outbound_comments(db, limit=30)
                duplicate_threshold = float(os.getenv("MOLTBOOK_COMMENT_SIMILARITY_THRESHOLD", "0.72"))
                duplicate = next((c for c in recent_comments
                                  if _comment_similarity(lead.draft_comment, c) >= duplicate_threshold), None)
                if duplicate:
                    lead.status="comment_skipped_duplicate"
                    lead.reason=(lead.reason+" Duplicate/near-duplicate comment suppressed.").strip()
                    db.commit()
                    outputs.append({"post_id":pid,"status":"skipped_duplicate","similarity":round(_comment_similarity(lead.draft_comment, duplicate),4)})
                    continue

                body=post_comment(pid,lead.draft_comment)
                posted_obj=body.get("comment",body) if isinstance(body,dict) else {}
                cid=str(posted_obj.get("id")) if isinstance(posted_obj,dict) and posted_obj.get("id") else None
                interaction=MoltbookInteraction(post_id=pid,comment_id=cid,direction="outbound",content=lead.draft_comment,
                    classification=str(item.get("classification") or "research_question"),status="posted",reason=str(item.get("reason") or ""))
                db.add(interaction)
                lead.status="commented"
                db.commit()
                posted+=1
                outputs.append({"post_id":pid,"status":"posted","comment_id":cid})
            except Exception as exc:
                lead.status="comment_failed"
                lead.reason=(lead.reason+" "+str(exc)).strip()
                db.commit()
                outputs.append({"post_id":pid,"status":"comment_failed","error":str(exc)})
        finally:
            db.close()
    return {"status":"completed","feed_scan":scan,"comments_posted":posted,"outputs":outputs}
