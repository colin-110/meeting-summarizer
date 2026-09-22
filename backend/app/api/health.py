from fastapi import APIRouter

from backend.app.database.connection import get_connection

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
