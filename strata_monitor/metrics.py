"""Pure telemetry parsing and adaptive TTS/TSR/TTFT calculations."""
from __future__ import annotations

from datetime import datetime
import math

WINDOWS_MINUTES = (10, 20, 30)
SUCCESS_FINISHES = frozenset(("stop", "length"))
FAILURE_FINISHES = frozenset(("error", "timeout", "nonfinite", "failed", "cancel", "cancelled",
                              "disconnect", "aborted"))


def responsive_scale(width, height):
    """Keep typography usable from the minimum widget size through large windows."""
    return max(0.86, min(1.12, min(width / 200, height / 190)))


def _timestamp(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            return None
    return None


def _task(row):
    """Normalize Strata server /metrics rows and host /api/monitor rows."""
    if not isinstance(row, dict):
        return None
    duration = row.get("duration_s")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration):
        ms = row.get("durationMs")
        duration = ms / 1000 if isinstance(ms, (int, float)) and math.isfinite(ms) else None
    if duration is None or duration < 0:
        return None
    start = _timestamp(row.get("time", row.get("at")))
    completed = _timestamp(row.get("completedAt"))
    if completed is None and start is not None:
        completed = start + duration
    if completed is None:
        return None

    finish = row.get("finish", row.get("finishReason"))
    state = str(row.get("state", "")).lower()
    if finish in SUCCESS_FINISHES:
        outcome = "success"
    elif finish in FAILURE_FINISHES:
        outcome = "failure"
    elif finish:
        # A present but unrecognized finish reason is not verified task outcome.
        outcome = None
    elif state in ("done", "success", "completed"):
        outcome = "success"
    elif state in ("error", "failed"):
        outcome = "failure"
    else:
        outcome = None
    ttft = row.get("ttftMs", row.get("ttft_ms"))
    if not isinstance(ttft, (int, float)) or not math.isfinite(ttft) or ttft < 0:
        ttft = None
    return {"completed": completed, "duration": float(duration), "outcome": outcome,
            "ttft_ms": float(ttft) if ttft is not None else None}


def adaptive_metrics(requests, now=None, ttft_requests=None):
    """Choose 10/20/30 minutes by verified outcomes and summarize that same window.

    TTS is the mean duration of successful, completed tasks. TSR counts only tasks
    with a known success or failure outcome. TTFT is the mean reported time to first
    token for requests in the selected window. The service's bounded history is used
    as-is; no persistent or historical data is read or modified.
    """
    now = datetime.now().timestamp() if now is None else float(now)
    tasks = [task for row in (requests or []) if (task := _task(row)) is not None
             and task["completed"] <= now]

    evaluated_by_window = {}
    for minutes in WINDOWS_MINUTES:
        cutoff = now - minutes * 60
        evaluated_by_window[minutes] = [task for task in tasks
                                        if task["completed"] >= cutoff and task["outcome"] is not None]

    if len(evaluated_by_window[10]) >= 10:
        minutes = 10
    elif len(evaluated_by_window[20]) >= 10:
        minutes = 20
    else:
        minutes = 30

    evaluated = evaluated_by_window[minutes]
    successes = [task for task in evaluated if task["outcome"] == "success"]
    durations = [task["duration"] for task in successes]
    ttft_cutoff = now - minutes * 60
    ttft_tasks = [task for row in (ttft_requests if ttft_requests is not None else requests)
                  if (task := _task(row)) is not None and ttft_cutoff <= task["completed"] <= now
                  and task["ttft_ms"] is not None]
    return {
        "window_minutes": minutes,
        "insufficient_data": len(evaluated) < 10,
        "tts_seconds": sum(durations) / len(durations) if durations else None,
        "tts_samples": len(durations),
        "tsr_percent": 100 * len(successes) / len(evaluated) if evaluated else None,
        "tsr_samples": len(evaluated),
        "ttft_ms": sum(task["ttft_ms"] for task in ttft_tasks) / len(ttft_tasks) if ttft_tasks else None,
        "ttft_samples": len(ttft_tasks),
    }


def activity_label(metrics, agent_status=None):
    """Map only explicitly reported engine states; never guess tool/debug activity."""
    metrics = metrics if isinstance(metrics, dict) else {}
    live = metrics.get("live") if isinstance(metrics.get("live"), dict) else {}
    state = str(live.get("state", metrics.get("phase", ""))).lower()
    if not state:
        return "Unknown"
    engine = metrics.get("engine") if isinstance(metrics.get("engine"), dict) else {}
    if state == "generating":
        return "Generating"
    if state == "reading":
        return "Reading"
    if state in ("thinking", "processing", "loading"):
        return "Processing"
    agent_status = agent_status if isinstance(agent_status, dict) else {}
    try:
        if int(agent_status.get("activeTurns", 0)) > 0:
            return "Active"
    except (TypeError, ValueError):
        pass
    if state in ("idle", "unloaded", "") and engine.get("status") not in ("starting", "loading"):
        return "Idle"
    if state in ("idle", "unloaded") and engine.get("status") in ("starting", "loading"):
        return "Processing"
    return "Unknown"


def tokens_per_second(metrics):
    """Return a reported live generation or prompt-reading rate, never a stale rate."""
    metrics = metrics if isinstance(metrics, dict) else {}
    telemetry = metrics.get("telemetry") if isinstance(metrics.get("telemetry"), dict) else {}
    if telemetry.get("throughput") == "demo":
        return None
    live = metrics.get("live") if isinstance(metrics.get("live"), dict) else {}
    state = str(live.get("state", metrics.get("phase", ""))).lower()
    if state == "generating":
        # Show the mean for this generation, not the engine's moving-window rate.
        # Do not substitute an instantaneous rate when the mean is unavailable.
        value = live.get("tok_s_mean")
    elif state == "reading":
        value = live.get("prefill_tok_s_mean", live.get("prompt_tps", metrics.get("promptTps")))
        if value is None:
            value = metrics.get("promptTps")
    else:
        return 0.0 if state in ("idle", "unloaded") else None
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def model_name(metrics):
    metrics = metrics if isinstance(metrics, dict) else {}
    engine = metrics.get("engine") if isinstance(metrics.get("engine"), dict) else {}
    model = engine.get("model") or engine.get("modelId")
    if model:
        return model
    if engine.get("running"):
        return metrics.get("variantId") or "N/A"
    return "N/A"
