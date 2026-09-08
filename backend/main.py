"""Nova Home Dashboard — FastAPI application entry point."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import auth, config, database, rc_sync, reminders, scheduler
from .routers import (bookings, cleaning, control, dashboard, finance, occupancy, payments,
                      payrecon, payroll, penalties, prices, sync, tasks)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("nova")

FRONTEND_DIR = config.BASE_DIR / "frontend"


async def _initial_sync() -> None:
    """Sync in the background so the server starts instantly. The DB keeps the
    previous data between restarts, so the app is populated immediately while
    fresh data loads in the background."""
    loop = asyncio.get_event_loop()
    try:
        count = await loop.run_in_executor(None, rc_sync.sync_to_db)
        logger.info("Initial sync: %s bookings (demo=%s)", count, config.DEMO_MODE)
        # seed the booking baseline so we don't announce the whole calendar
        await loop.run_in_executor(None, reminders.check_booking_changes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initial sync failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    scheduler.start()
    asyncio.create_task(_initial_sync())  # don't block startup on the network
    yield
    scheduler.shutdown()


app = FastAPI(title="Nova Home Dashboard", version="1.0", lifespan=lifespan)

# Telegram WebApp is served from t.me / opens with an opaque origin ("null").
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=r"https://.*\.telegram\.org|https://t\.me|null",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routers — every data endpoint requires a legitimate dashboard user
# (see auth.access_guard); owner-only routers add their own stricter guard.
for r in (dashboard.router, bookings.router, cleaning.router,
          occupancy.router, payments.router, sync.router, finance.router,
          tasks.router, prices.router, penalties.router, payroll.router,
          payrecon.router, control.router):
    app.include_router(r, prefix=config.API_PREFIX, dependencies=[Depends(auth.access_guard)])


@app.middleware("http")
async def no_cache_frontend(request, call_next):
    """Always revalidate the app shell so updates load without a hard refresh."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get(f"{config.API_PREFIX}/health")
def health():
    return {"status": "ok", "version": config.APP_VERSION, "demo_mode": config.DEMO_MODE,
            "last_sync": database.last_sync()}


@app.get(f"{config.API_PREFIX}/me")
def me(request: Request):
    """Return the caller's role, derived from the Telegram Mini App signature."""
    init = request.headers.get("X-Telegram-Init-Data", "")
    okey = request.headers.get("X-Owner-Key", "")
    role = auth.role_from_init(init, okey)
    return {
        "role": role,
        "is_owner": role == "owner",
        "can_payments": role == "owner" or auth.can_payments(init, okey),
        # False → the app was opened from a bare link, not through the bot
        "has_access": auth.has_access(init, okey),
    }


# Serve the Mini App frontend (single-origin deploy: one URL for ngrok/nginx).
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
