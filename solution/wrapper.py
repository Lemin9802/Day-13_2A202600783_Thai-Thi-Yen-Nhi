from __future__ import annotations

import copy
import hashlib
import re
import time
import unicodedata
from typing import Any

from telemetry.cost import cost_from_usage
from telemetry.logger import logger, new_correlation_id, set_correlation_id
from telemetry.redact import redact

_NOTE_MARKER = re.compile(r"(?i)\b(ghi\s*ch[uú]|ghi\s*chu|note|notes|order\s*note|customer\s*note)\b\s*[:：-]?")
_RISKY_NOTE = re.compile(r"(?i)(bo\s+qua|bỏ\s+qua|system|developer|policy|price|gia|giá|discount|coupon|tool|total|tong|tổng)")
_SPACE = re.compile(r"\s+")


def _fold_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("Đ", "D").replace("đ", "d")
    text = "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")
    return _SPACE.sub(" ", text).strip()


def _sanitize_question(question: Any) -> str:
    text = "" if question is None else str(question)
    marker = _NOTE_MARKER.search(text)
    if marker and _RISKY_NOTE.search(text[marker.start():]):
        text = text[:marker.start()].strip()
    return _fold_text(text)


def _cache_key(question: str) -> str:
    return hashlib.sha256(question.lower().encode("utf-8")).hexdigest()


def _safe_config(config: dict[str, Any]) -> dict[str, Any]:
    conf = dict(config or {})
    conf.update({
        "temperature": 0.2,
        "max_steps": 6,
        "loop_guard": True,
        "context_size": 5,
        "verbose_system": False,
        "timeout_ms": 25000,
        "max_completion_tokens": 700,
        "normalize_unicode": True,
        "redact_pii": True,
        "context_reset_every": 6,
        "tool_error_rate": min(float(conf.get("tool_error_rate", 0.02) or 0.02), 0.02),
        "planner": True,
        "verify": True,
        "self_consistency": max(1, int(conf.get("self_consistency", 2) or 2)),
        "tool_budget": 4,
    })
    conf["retry"] = {"enabled": True, "max_attempts": 2, "backoff_ms": 250}
    conf["cache"] = {"enabled": True, "max_size": 1000}
    if conf.get("catalog_override"):
        conf["catalog_override"] = {}
    return conf


def _trace_actions(trace: Any) -> list[str]:
    actions: list[str] = []
    if isinstance(trace, list):
        for item in trace:
            if isinstance(item, dict):
                name = item.get("action") or item.get("tool") or item.get("name") or item.get("event")
                if name:
                    actions.append(str(name))
    return actions


def _result_is_good(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("status") != "ok":
        return False
    answer = result.get("answer")
    return isinstance(answer, str) and bool(answer.strip())


def _postprocess(result: dict[str, Any], context: dict[str, Any], cache_hit: bool = False) -> dict[str, Any]:
    result = copy.deepcopy(result) if isinstance(result, dict) else {}
    answer = result.get("answer")
    if isinstance(answer, str):
        result["answer"] = redact(answer)[0]
    meta = dict(result.get("meta") or {})
    meta["cache_hit"] = cache_hit
    meta["session_id"] = context.get("session_id")
    meta["turn_index"] = context.get("turn_index")
    result["meta"] = meta
    result.setdefault("status", "wrapper_error")
    result.setdefault("steps", 0)
    result.setdefault("trace", [])
    return result


def _log_outcome(event: str, result: dict[str, Any], context: dict[str, Any], wall_ms: int, attempt: int, sanitized_question: str) -> None:
    meta = result.get("meta") or {}
    usage = meta.get("usage") or {}
    model = meta.get("model") or "unknown"
    tools = meta.get("tools_used") or []
    actions = _trace_actions(result.get("trace"))
    repeated_actions = sorted({a for a in actions if actions.count(a) > 1})
    logger.log_event(event, {
        "qid": context.get("qid"),
        "session_id": context.get("session_id"),
        "turn_index": context.get("turn_index"),
        "attempt": attempt,
        "status": result.get("status"),
        "question": redact(sanitized_question)[0],
        "wall_ms": wall_ms,
        "latency_ms": meta.get("latency_ms"),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cost_usd": cost_from_usage(str(model), usage),
        "model": model,
        "provider": meta.get("provider"),
        "tools_used": tools,
        "tool_count": len(tools),
        "steps": result.get("steps"),
        "cache_hit": meta.get("cache_hit", False),
        "repeated_actions": repeated_actions,
    })


def mitigate(call_next, question, config, context):
    context = context or {}
    set_correlation_id(str(context.get("qid") or new_correlation_id()))
    conf = _safe_config(config or {})
    sanitized_question = _sanitize_question(question)
    cache = context.get("cache")
    lock = context.get("cache_lock")
    key = _cache_key(sanitized_question)

    if isinstance(cache, dict) and lock is not None:
        with lock:
            cached = cache.get(key)
        if cached:
            result = _postprocess(cached, context, cache_hit=True)
            _log_outcome("agent_cache_hit", result, context, 0, 0, sanitized_question)
            return result

    attempts = int((conf.get("retry") or {}).get("max_attempts", 2))
    backoff_ms = int((conf.get("retry") or {}).get("backoff_ms", 250))
    best: dict[str, Any] | None = None

    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            raw = call_next(sanitized_question, conf)
        except Exception as exc:
            raw = {
                "answer": "Khong the tinh tong cong: loi he thong tam thoi.",
                "status": "wrapper_error",
                "steps": 0,
                "trace": [{"event": "wrapper_exception", "error": type(exc).__name__}],
                "meta": {"latency_ms": int((time.time() - started) * 1000), "usage": {}, "tools_used": []},
            }
        wall_ms = int((time.time() - started) * 1000)
        result = _postprocess(raw, context, cache_hit=False)
        _log_outcome("agent_call", result, context, wall_ms, attempt, sanitized_question)
        best = result
        if _result_is_good(result):
            break
        if attempt < attempts:
            time.sleep(backoff_ms / 1000.0)

    if best is None:
        best = _postprocess({
            "answer": "Khong the tinh tong cong: loi he thong tam thoi.",
            "status": "wrapper_error",
            "steps": 0,
            "trace": [],
            "meta": {"usage": {}, "tools_used": []},
        }, context, cache_hit=False)

    if _result_is_good(best) and isinstance(cache, dict) and lock is not None:
        max_size = int((conf.get("cache") or {}).get("max_size", 1000))
        with lock:
            if len(cache) < max_size:
                cache[key] = copy.deepcopy(best)

    return best
