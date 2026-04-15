"""Famly Photos – FastAPI app with scheduled fetcher, dashboard, and gallery."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import FamlyAuth
from config import settings, DB_PATH
from db import Database
from fetcher import run_fetch, _exif_date

# ── Logging ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s  %(name)-18s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("famly.main")

# ── Shared state ─────────────────────────────────────────────────────────

db = Database(DB_PATH)
auth = FamlyAuth(
    email=settings.famly_email,
    password=settings.famly_password,
    installation_id=settings.famly_installation_id,
    token_path=Path(DB_PATH).parent / "token.json",
    static_token=settings.famly_access_token,
)
scheduler = BackgroundScheduler()

_fetch_running = False


def _do_fetch() -> None:
    global _fetch_running
    if _fetch_running:
        logger.warning("Fetch already running – skipping")
        return

    _fetch_running = True
    run_id = db.start_run()
    try:
        if not settings.famly_child_id:
            raise ValueError("FAMLY_CHILD_ID is not set")
        downloaded, skipped = run_fetch(
            auth=auth,
            db=db,
            child_id=settings.famly_child_id,
            photo_dir=settings.photo_dir,
            fetch_tagged=settings.fetch_tagged,
            fetch_feed=settings.fetch_feed,
            fetch_journey=settings.fetch_journey,
            fetch_notes=settings.fetch_notes,
            fetch_messages=settings.fetch_messages,
        )
        db.finish_run(run_id, downloaded=downloaded, skipped=skipped)
    except Exception as exc:
        logger.error("Fetch failed: %s", exc)
        db.finish_run(run_id, downloaded=0, skipped=0, error=str(exc))
    finally:
        _fetch_running = False


# ── App lifecycle ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Fix any jobs left as "running" from a previous crash
    stale = db.fix_stale_runs()
    if stale:
        logger.info("Marked %d stale running jobs as interrupted", stale)

    # Clean up ghost DB entries (files in DB but missing from disk)
    cleaned = db.cleanup_missing_files(settings.photo_dir)
    if cleaned:
        logger.info("Startup cleanup: removed %d ghost entries", cleaned)

    # Index any existing photos already on disk (from old script / Synology)
    indexed = db.scan_directory(settings.photo_dir)
    if indexed:
        logger.info("Startup scan: indexed %d existing photos", indexed)

    scheduler.add_job(
        _do_fetch,
        "interval",
        hours=settings.fetch_interval_hours,
        id="photo_fetch",
    )
    scheduler.start()
    logger.info("Scheduler started – fetching every %dh", settings.fetch_interval_hours)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Famly Photos",
    description="Famly photo/video downloader with gallery and monitoring",
    lifespan=lifespan,
)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ── Favicon / logo ────────────────────────────────────────────────────────

_LOGO_PNG = Path(__file__).parent.parent / "famly-photos.png"


@app.get("/favicon.png")
@app.get("/apple-touch-icon.png")
@app.get("/logo.png")
async def logo_png() -> FileResponse:
    return FileResponse(_LOGO_PNG, media_type="image/png")


# ── Serve photos from the mounted directory ──────────────────────────────

@app.get("/photos/{filename:path}")
async def serve_photo(filename: str) -> FileResponse:
    """Serve a photo/video from the photo directory."""
    base = Path(settings.photo_dir).resolve()
    path = (base / filename).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    mime, _ = mimetypes.guess_type(str(path))
    return FileResponse(path, media_type=mime or "application/octet-stream")


# ── Gallery / Photos (home page) ───────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def gallery(request: Request) -> HTMLResponse:
    """Photos gallery – tagged photos only."""
    batch_size = 40
    items = db.gallery_items(source="tagged", limit=batch_size, offset=0)
    total = db.gallery_total(source="tagged")

    items_dicts = [asdict(it) for it in items]

    return templates.TemplateResponse(
        request,
        "gallery.html",
        {
            "items": items_dicts,
            "total": total,
            "has_more": total > batch_size,
        },
    )


@app.get("/api/gallery-page")
async def gallery_page(
    offset: int = Query(0, ge=0),
    limit: int = Query(40, ge=1, le=100),
) -> JSONResponse:
    """JSON endpoint for infinite scroll – tagged photos only."""
    items = db.gallery_items(source="tagged", limit=limit, offset=offset)
    total = db.gallery_total(source="tagged")
    items_dicts = [asdict(it) for it in items]
    return JSONResponse({
        "items": items_dicts,
        "total": total,
        "has_more": offset + limit < total,
    })


# ── Journey (observations with photos/videos) ────────────────────────────

@app.get("/journey", response_class=HTMLResponse)
async def journey(
    request: Request,
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    per_page = 20
    offset = (page - 1) * per_page
    entries = db.content_entries_with_media(source="journey", limit=per_page, offset=offset)

    return templates.TemplateResponse(
        request,
        "timeline.html",
        {
            "entries": entries,
            "page": page,
        },
    )


@app.get("/timeline")
async def timeline_redirect() -> RedirectResponse:
    """Legacy redirect: /timeline → /journey."""
    return RedirectResponse(url="/journey", status_code=301)


# ── Dashboard ────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    stats = db.get_stats(settings.photo_dir)
    recent_runs = db.recent_runs(limit=15)
    recent_photos = db.recent_photos(limit=20)
    next_run = scheduler.get_job("photo_fetch")
    next_run_time = (
        next_run.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z")
        if next_run and next_run.next_run_time
        else "N/A"
    )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "recent_runs": recent_runs,
            "recent_photos": recent_photos,
            "next_run_time": next_run_time,
            "fetch_interval": settings.fetch_interval_hours,
            "is_running": _fetch_running,
            "token_age_hours": round(auth.token_age_hours, 1),
            "child_id": settings.famly_child_id[:8] + "..." if settings.famly_child_id else "NOT SET",
        },
    )


# ── API endpoints ────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats() -> JSONResponse:
    stats = db.get_stats(settings.photo_dir)
    return JSONResponse({
        "total_photos": stats.total_photos,
        "total_videos": stats.total_videos,
        "total_entries": stats.total_entries,
        "entries_by_source": stats.entries_by_source,
        "total_runs": stats.total_runs,
        "successful_runs": stats.successful_runs,
        "failed_runs": stats.failed_runs,
        "disk_usage_mb": stats.disk_usage_mb,
        "last_run": {
            "started_at": stats.last_run.started_at,
            "status": stats.last_run.status,
            "photos_downloaded": stats.last_run.photos_downloaded,
        } if stats.last_run else None,
    })


@app.post("/api/fetch-now")
async def trigger_fetch() -> JSONResponse:
    if _fetch_running:
        return JSONResponse({"status": "already_running"}, status_code=409)
    scheduler.add_job(_do_fetch, id="manual_fetch", replace_existing=True)
    return JSONResponse({"status": "triggered"})


@app.post("/api/refresh-token")
async def refresh_token() -> JSONResponse:
    try:
        auth.refresh()
        return JSONResponse({"status": "ok", "token_age_hours": 0.0})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@app.post("/api/rescan")
async def rescan_directory() -> JSONResponse:
    """Re-index existing photos from the photo directory into the DB."""
    indexed = db.scan_directory(settings.photo_dir)
    return JSONResponse({"status": "ok", "indexed": indexed})


@app.post("/api/purge-source")
async def purge_source(source: str = Query(..., description="Source to purge: feed, note, message, journey")) -> JSONResponse:
    """Delete all content entries and associated photos for a given source."""
    deleted_entries, deleted_photos, deleted_files = db.purge_source(source, settings.photo_dir)
    return JSONResponse({
        "status": "ok",
        "source": source,
        "deleted_entries": deleted_entries,
        "deleted_photos": deleted_photos,
        "deleted_files": deleted_files,
    })


@app.post("/api/cleanup")
async def cleanup_missing() -> JSONResponse:
    """Remove photo DB entries whose files no longer exist on disk."""
    removed = db.cleanup_missing_files(settings.photo_dir)
    return JSONResponse({"status": "ok", "removed": removed})


@app.post("/api/purge-all")
async def purge_all(password: str = Query("", description="Admin password")) -> JSONResponse:
    """Delete ALL content entries, photo rows, and files. Then trigger a fresh fetch."""
    if settings.admin_password and password != settings.admin_password:
        return JSONResponse({"status": "error", "detail": "Wrong password"}, status_code=403)
    if _fetch_running:
        return JSONResponse({"status": "fetch_running"}, status_code=409)
    deleted_entries, deleted_files, moved_legacy = db.purge_all(settings.photo_dir)
    scheduler.add_job(_do_fetch, id="manual_fetch", replace_existing=True)
    return JSONResponse({
        "status": "ok",
        "deleted_entries": deleted_entries,
        "deleted_files": deleted_files,
        "moved_legacy": moved_legacy,
        "message": "Purged all data. Fresh fetch triggered.",
    })


_UPLOAD_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
    ".mp4", ".mov", ".webm", ".m4v",
}
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB


@app.post("/api/upload")
async def upload_files(files: list[UploadFile] = File(...)) -> JSONResponse:
    """Upload one or more photos/videos. Date is extracted from EXIF."""
    photo_path = Path(settings.photo_dir)
    tagged_dir = photo_path / "tagged"
    tagged_dir.mkdir(parents=True, exist_ok=True)

    uploaded = 0
    errors: list[str] = []

    for f in files:
        name = Path(f.filename or "upload.jpg").name  # strip any directory components
        ext = Path(name).suffix.lower()
        if ext not in _UPLOAD_EXTS:
            errors.append(f"{name}: unsupported format")
            continue

        # Read file content (check size)
        data = await f.read()
        if len(data) > _MAX_UPLOAD_BYTES:
            errors.append(f"{name}: exceeds 200 MB limit")
            continue

        # Deduplicate filename
        dest = tagged_dir / name
        if dest.exists():
            stem = Path(name).stem
            i = 1
            while dest.exists():
                dest = tagged_dir / f"{stem}_{i}{ext}"
                i += 1

        dest.write_bytes(data)

        rel_name = f"tagged/{dest.name}"

        # Extract date: EXIF first, then fall back to empty
        created_at = _exif_date(dest)

        # Generate content ID and record in DB
        cid = hashlib.sha256(f"manual|{rel_name}".encode()).hexdigest()[:24]
        db.record_photo(rel_name, "manual:upload", content_id=cid)
        db.upsert_content(
            content_id=cid, source="tagged",
            created_at=created_at,
        )
        uploaded += 1

    return JSONResponse({
        "status": "ok",
        "uploaded": uploaded,
        "errors": errors,
    })


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "healthy",
        "token_valid": auth.access_token != "",
        "token_age_hours": round(auth.token_age_hours, 1),
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.app_host, port=settings.app_port, log_level=settings.log_level.lower())
