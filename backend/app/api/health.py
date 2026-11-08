from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from backend.app.core import config
from backend.app.database.connection import get_connection
from backend.app.database.meetings_repo import queue_stats
from backend.app.utils.rate_limit import budget_used

router = APIRouter()


@router.api_route("/health", methods=["GET", "HEAD"])
def health() -> dict:
    db_ok = True
    try:
        conn = get_connection()
        conn.execute("SELECT 1")
        conn.close()
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
    }


@router.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus text format: queue health and provider budget usage.
    Aggregate counts only — nothing about individual meetings or users."""
    stats = queue_stats()
    lines = ["# TYPE meetings gauge"]
    for status, n in sorted(stats["by_status"].items()):
        lines.append(f'meetings{{status="{status}"}} {n}')
    lines += [
        "# TYPE queue_oldest_waiting_seconds gauge",
        f"queue_oldest_waiting_seconds {stats['oldest_queued_seconds']:.1f}",
        "# TYPE meetings_completed_last_hour gauge",
        f"meetings_completed_last_hour {stats['completed_last_hour']}",
        "# TYPE processing_seconds_avg_last_hour gauge",
        f"processing_seconds_avg_last_hour {stats['avg_processing_seconds_last_hour']:.1f}",
        "# TYPE provider_tokens_last_minute gauge",
    ]
    for model in sorted({config.SUMMARY_MODEL, config.SECTION_MODEL, config.QA_MODEL}):
        lines.append(f'provider_tokens_last_minute{{model="{model}"}} {budget_used(f"tokens:{model}"):.0f}')
    lines.append(f"provider_tokens_per_minute_limit {config.LLM_TOKENS_PER_MINUTE}")
    lines += [
        "# TYPE audio_seconds_total counter",
        f"audio_seconds_total {stats['audio_seconds_total']:.0f}",
        "# TYPE audio_seconds_skipped_total counter",
        f"audio_seconds_skipped_total {stats['audio_seconds_total'] - stats['audio_seconds_sent_total']:.0f}",
        "# TYPE llm_tokens_total counter",
        f"llm_tokens_total {stats['llm_tokens_total']:.0f}",
        "# TYPE answer_cache_hits_total counter",
        f"answer_cache_hits_total {stats['answer_cache_hits_total']}",
    ]
    return "\n".join(lines) + "\n"
