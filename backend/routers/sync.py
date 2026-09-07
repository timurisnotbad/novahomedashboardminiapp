from datetime import datetime

from fastapi import APIRouter

from .. import rc_sync

router = APIRouter(tags=["sync"])


@router.post("/sync")
def force_sync():
    try:
        count = rc_sync.sync_to_db()
        return {
            "success": True,
            "bookings_synced": count,
            "synced_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
