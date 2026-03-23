"""SQLite persistence for photo tracking, content entries, and job history."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("famly.db")


@dataclass(frozen=True)
class JobRun:
    id: int
    started_at: str
    finished_at: str | None
    status: str
    photos_downloaded: int
    photos_skipped: int
    error_message: str | None


@dataclass(frozen=True)
class ContentEntry:
    """An observation, journey entry, note, or message with optional media."""
    id: str
    source: str          # 'tagged' | 'journey' | 'note' | 'message' | 'feed'
    title: str
    body: str
    author: str
    created_at: str
    media_files: list[str]   # filenames in photo_dir
    video_url: str | None


@dataclass(frozen=True)
class GalleryItem:
    """Single item for the gallery view – a photo with optional context."""
    filename: str
    fetched_at: str
    source: str
    title: str
    body: str
    author: str
    content_date: str
    video_url: str = ""


@dataclass(frozen=True)
class Stats:
    total_photos: int
    total_videos: int
    total_entries: int
    entries_by_source: dict[str, int]
    total_runs: int
    successful_runs: int
    failed_runs: int
    last_run: JobRun | None
    last_success: JobRun | None
    disk_usage_mb: float


class Database:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS photos (
                filename    TEXT PRIMARY KEY,
                source_url  TEXT NOT NULL,
                fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
                content_id  TEXT
            );

            CREATE TABLE IF NOT EXISTS content_entries (
                id          TEXT PRIMARY KEY,
                source      TEXT NOT NULL DEFAULT 'tagged',
                title       TEXT NOT NULL DEFAULT '',
                body        TEXT NOT NULL DEFAULT '',
                author      TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT '',
                video_url   TEXT,
                fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS job_runs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at        TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at       TEXT,
                status            TEXT NOT NULL DEFAULT 'running',
                photos_downloaded INTEGER NOT NULL DEFAULT 0,
                photos_skipped    INTEGER NOT NULL DEFAULT 0,
                error_message     TEXT
            );
        """)
        # Add content_id column to photos if upgrading from v1
        try:
            self._conn.execute("SELECT content_id FROM photos LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute("ALTER TABLE photos ADD COLUMN content_id TEXT")
        # Now safe to create indexes on content_id
        self._conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_photos_content ON photos(content_id);
            CREATE INDEX IF NOT EXISTS idx_content_source ON content_entries(source);
            CREATE INDEX IF NOT EXISTS idx_content_date ON content_entries(created_at);
        """)
        self._conn.commit()
        logger.info("Database ready at %s", self._path)

    # ── Photo tracking ───────────────────────────────────────────────────

    def photo_exists(self, filename: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM photos WHERE filename = ?", (filename,)
        ).fetchone()
        return row is not None

    def record_photo(
        self, filename: str, source_url: str, content_id: str | None = None
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO photos (filename, source_url, content_id) "
            "VALUES (?, ?, ?)",
            (filename, source_url, content_id),
        )
        self._conn.commit()

    def link_photo_content(self, filename: str, content_id: str) -> None:
        """Set content_id on a photo row that was missing it."""
        self._conn.execute(
            "UPDATE photos SET content_id = ? WHERE filename = ? AND content_id IS NULL",
            (content_id, filename),
        )
        self._conn.commit()

    # Directories created by Synology Photos / NAS that should not be indexed
    _SKIP_DIRS = {"@eaDir", "#recycle", ".synology", "@tmp", "@Recycle", "_legacy"}

    # Subdirectories managed by the app (only these are scanned/counted)
    _SCAN_DIRS = {"tagged", "journey"}

    def scan_directory(self, photo_dir: str) -> int:
        """Index existing photos on disk that aren't yet in the DB.

        Only scans known subdirectories (tagged/, journey/).
        Skips Synology metadata directories (@eaDir, #recycle, etc.).
        Stores relative paths like 'tagged/IMG_001.jpg'.
        Returns the number of newly indexed files.
        """
        IMAGE_EXTS = {
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff",
            ".mp4", ".mov", ".avi", ".webm", ".m4v", ".heic",
        }
        photo_path = Path(photo_dir)
        if not photo_path.exists():
            return 0

        indexed = 0
        for subdir in self._SCAN_DIRS:
            sub_path = photo_path / subdir
            if not sub_path.exists():
                continue
            for f in sub_path.rglob("*"):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in IMAGE_EXTS:
                    continue

                rel = f.relative_to(photo_path)
                if any(part in self._SKIP_DIRS for part in rel.parts):
                    continue

                rel_name = str(rel)
                if self.photo_exists(rel_name):
                    continue

                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                fetched_str = mtime.strftime("%Y-%m-%d %H:%M:%S")

                self._conn.execute(
                    "INSERT OR IGNORE INTO photos (filename, source_url, fetched_at) "
                    "VALUES (?, ?, ?)",
                    (rel_name, f"file://{f}", fetched_str),
                )
                indexed += 1

        self._conn.commit()
        if indexed:
            logger.info("Indexed %d existing photos from disk", indexed)
        return indexed

    def cleanup_missing_files(self, photo_dir: str) -> int:
        """Remove photo entries whose files no longer exist on disk."""
        rows = self._conn.execute("SELECT filename FROM photos").fetchall()
        photo_path = Path(photo_dir)
        missing = [r["filename"] for r in rows
                   if not (photo_path / r["filename"]).exists()]
        if missing:
            self._conn.executemany(
                "DELETE FROM photos WHERE filename = ?",
                [(f,) for f in missing],
            )
            self._conn.commit()
            logger.info("Cleaned up %d ghost photo entries (files missing from disk)", len(missing))
        return len(missing)

    def photo_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM photos WHERE content_id IS NOT NULL"
        ).fetchone()
        return row[0] if row else 0

    def recent_photos(self, limit: int = 20) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT filename, fetched_at FROM photos ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"filename": r["filename"], "fetched_at": r["fetched_at"]} for r in rows]

    # ── Content entries (observations, journey, notes, messages) ──────

    def content_exists(self, content_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM content_entries WHERE id = ?", (content_id,)
        ).fetchone()
        return row is not None

    def upsert_content(
        self,
        *,
        content_id: str,
        source: str,
        title: str = "",
        body: str = "",
        author: str = "",
        created_at: str = "",
        video_url: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO content_entries (id, source, title, body, author, created_at, video_url)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, body=excluded.body,
                 author=excluded.author, video_url=excluded.video_url,
                 created_at=CASE WHEN excluded.created_at != '' THEN excluded.created_at ELSE content_entries.created_at END""",
            (content_id, source, title, body, author, created_at, video_url),
        )
        self._conn.commit()

    def content_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM content_entries").fetchone()
        return row[0] if row else 0

    def content_count_by_source(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT source, COUNT(*) as cnt FROM content_entries GROUP BY source"
        ).fetchall()
        return {r["source"]: r["cnt"] for r in rows}

    def video_count(self) -> int:
        row = self._conn.execute(
            """SELECT (
                SELECT COUNT(*) FROM content_entries
                WHERE video_url IS NOT NULL AND video_url != ''
            ) + (
                SELECT COUNT(*) FROM photos
                WHERE (filename LIKE '%.mp4' OR filename LIKE '%.mov'
                       OR filename LIKE '%.webm' OR filename LIKE '%.m4v')
                AND content_id NOT IN (
                    SELECT id FROM content_entries
                    WHERE video_url IS NOT NULL AND video_url != ''
                )
            )"""
        ).fetchone()
        return row[0] if row else 0

    def purge_source(self, source: str, photo_dir: str) -> tuple[int, int, int]:
        """Delete all content entries + associated photos for a source.

        Returns (deleted_entries, deleted_photo_rows, deleted_files).
        """
        # Find photo files linked to content entries of this source
        rows = self._conn.execute(
            """SELECT p.filename FROM photos p
               INNER JOIN content_entries c ON p.content_id = c.id
               WHERE c.source = ?""",
            (source,),
        ).fetchall()

        deleted_files = 0
        for r in rows:
            filepath = Path(photo_dir) / r["filename"]
            if filepath.exists():
                filepath.unlink()
                deleted_files += 1

        # Delete photo DB rows linked to this source
        cur = self._conn.execute(
            """DELETE FROM photos WHERE content_id IN (
                 SELECT id FROM content_entries WHERE source = ?
               )""",
            (source,),
        )
        deleted_photos = cur.rowcount

        # Delete content entries
        cur2 = self._conn.execute(
            "DELETE FROM content_entries WHERE source = ?", (source,),
        )
        deleted_entries = cur2.rowcount

        self._conn.commit()
        logger.info(
            "Purged source '%s': %d entries, %d photo rows, %d files",
            source, deleted_entries, deleted_photos, deleted_files,
        )
        return deleted_entries, deleted_photos, deleted_files

    def purge_all(self, photo_dir: str) -> tuple[int, int, int]:
        """Delete ALL content entries, photos rows, and files from disk.

        Legacy root-level photos (from old script) are moved to _legacy/.
        Returns (deleted_entries, deleted_files, moved_legacy).
        """
        photo_path = Path(photo_dir)
        IMAGE_EXTS = {
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff",
            ".mp4", ".mov", ".avi", ".webm", ".m4v", ".heic",
        }

        # Move legacy root-level photos to _legacy/ backup
        # Use shutil.move (not Path.rename) because /photos/ may be an SMB mount
        # and rename fails across filesystem boundaries
        legacy_dir = photo_path / "_legacy"
        moved_legacy = 0
        for f in photo_path.iterdir():
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                legacy_dir.mkdir(exist_ok=True)
                try:
                    shutil.move(str(f), str(legacy_dir / f.name))
                    moved_legacy += 1
                except OSError as exc:
                    logger.warning("Failed to move legacy file %s: %s", f.name, exc)

        # Delete all files in known subdirectories
        deleted_files = 0
        for subdir in ("tagged", "journey", "feed", "messages"):
            sub_path = photo_path / subdir
            if sub_path.exists():
                for f in sub_path.iterdir():
                    if f.is_file():
                        f.unlink()
                        deleted_files += 1

        # Clear DB tables
        self._conn.execute("DELETE FROM photos")
        cur = self._conn.execute("DELETE FROM content_entries")
        deleted_entries = cur.rowcount
        self._conn.commit()

        logger.info(
            "Purged all: %d entries, %d files deleted, %d legacy moved to _legacy/",
            deleted_entries, deleted_files, moved_legacy,
        )
        return deleted_entries, deleted_files, moved_legacy

    # ── Gallery queries ──────────────────────────────────────────────────

    def gallery_items(
        self,
        *,
        source: str | None = None,
        limit: int = 60,
        offset: int = 0,
    ) -> list[GalleryItem]:
        """Photos joined with their content entries, plus video-only entries."""
        where_photo = "WHERE p.content_id IS NOT NULL"
        where_video = "WHERE c.video_url IS NOT NULL AND c.video_url != ''"
        params: list = []
        if source:
            where_photo += " AND c.source = ?"
            where_video += " AND c.source = ?"
            params.append(source)
            params.append(source)

        params.extend([limit, offset])
        rows = self._conn.execute(
            f"""SELECT * FROM (
                    SELECT p.filename, p.fetched_at,
                           c.source as source,
                           COALESCE(c.title, '') as title,
                           COALESCE(c.body, '') as body,
                           COALESCE(c.author, '') as author,
                           COALESCE(c.created_at, p.fetched_at) as content_date,
                           COALESCE(c.video_url, '') as video_url
                    FROM photos p
                    JOIN content_entries c ON p.content_id = c.id
                    {where_photo}

                    UNION ALL

                    SELECT '__video__' || c.id as filename, c.fetched_at,
                           c.source, COALESCE(c.title, '') as title,
                           COALESCE(c.body, '') as body,
                           COALESCE(c.author, '') as author,
                           c.created_at as content_date,
                           c.video_url
                    FROM content_entries c
                    {where_video}
                    AND NOT EXISTS (
                        SELECT 1 FROM photos p WHERE p.content_id = c.id
                        AND (p.filename LIKE '%.mp4' OR p.filename LIKE '%.mov'
                             OR p.filename LIKE '%.webm' OR p.filename LIKE '%.m4v')
                    )
                ) combined
                ORDER BY content_date DESC
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [
            GalleryItem(
                filename=r["filename"],
                fetched_at=r["fetched_at"],
                source=r["source"],
                title=r["title"],
                body=r["body"],
                author=r["author"],
                content_date=r["content_date"],
                video_url=r["video_url"],
            )
            for r in rows
        ]

    def gallery_total(self, source: str | None = None) -> int:
        if source:
            row = self._conn.execute(
                """SELECT (
                    SELECT COUNT(*) FROM photos p
                    JOIN content_entries c ON p.content_id = c.id
                    WHERE c.source = ?
                ) + (
                    SELECT COUNT(*) FROM content_entries c
                    WHERE c.video_url IS NOT NULL AND c.video_url != ''
                    AND c.source = ?
                    AND NOT EXISTS (
                        SELECT 1 FROM photos p WHERE p.content_id = c.id
                        AND (p.filename LIKE '%.mp4' OR p.filename LIKE '%.mov'
                             OR p.filename LIKE '%.webm' OR p.filename LIKE '%.m4v')
                    )
                )""",
                (source, source),
            ).fetchone()
        else:
            row = self._conn.execute(
                """SELECT (SELECT COUNT(*) FROM photos) + (
                    SELECT COUNT(*) FROM content_entries c
                    WHERE c.video_url IS NOT NULL AND c.video_url != ''
                    AND NOT EXISTS (
                        SELECT 1 FROM photos p WHERE p.content_id = c.id
                        AND (p.filename LIKE '%.mp4' OR p.filename LIKE '%.mov'
                             OR p.filename LIKE '%.webm' OR p.filename LIKE '%.m4v')
                    )
                )"""
            ).fetchone()
        return row[0] if row else 0

    def content_entries_with_media(
        self, *, source: str | None = None, limit: int = 30, offset: int = 0
    ) -> list[ContentEntry]:
        """Content entries with their associated media files."""
        where = "WHERE 1=1"
        params: list = []
        if source:
            where += " AND c.source = ?"
            params.append(source)

        params.extend([limit, offset])
        rows = self._conn.execute(
            f"""SELECT c.*, GROUP_CONCAT(p.filename) as files
                FROM content_entries c
                LEFT JOIN photos p ON p.content_id = c.id
                {where}
                GROUP BY c.id
                ORDER BY c.created_at DESC
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [
            ContentEntry(
                id=r["id"],
                source=r["source"],
                title=r["title"],
                body=r["body"],
                author=r["author"],
                created_at=r["created_at"],
                media_files=[f for f in (r["files"] or "").split(",") if f],
                video_url=r["video_url"],
            )
            for r in rows
        ]

    # ── Job runs ─────────────────────────────────────────────────────────

    def fix_stale_runs(self) -> int:
        """Mark any 'running' jobs with no finished_at as interrupted (e.g. after a crash)."""
        cur = self._conn.execute(
            """UPDATE job_runs SET status = 'interrupted',
                      finished_at = started_at
               WHERE status = 'running' AND finished_at IS NULL"""
        )
        self._conn.commit()
        return cur.rowcount

    def start_run(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO job_runs (status) VALUES ('running')"
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def finish_run(
        self,
        run_id: int,
        *,
        downloaded: int,
        skipped: int,
        error: str | None = None,
    ) -> None:
        status = "failed" if error else "success"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            """UPDATE job_runs
               SET finished_at = ?, status = ?,
                   photos_downloaded = ?, photos_skipped = ?,
                   error_message = ?
             WHERE id = ?""",
            (now, status, downloaded, skipped, error, run_id),
        )
        self._conn.commit()

    def _row_to_job(self, row: sqlite3.Row) -> JobRun:
        return JobRun(
            id=row["id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            photos_downloaded=row["photos_downloaded"],
            photos_skipped=row["photos_skipped"],
            error_message=row["error_message"],
        )

    def recent_runs(self, limit: int = 10) -> list[JobRun]:
        rows = self._conn.execute(
            "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def get_stats(self, photo_dir: str) -> Stats:
        total_photos = self.photo_count()
        total_videos = self.video_count()
        total_entries = self.content_count()
        entries_by_source = self.content_count_by_source()
        runs = self._conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as ok, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as fail "
            "FROM job_runs"
        ).fetchone()

        last = self._conn.execute(
            "SELECT * FROM job_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_ok = self._conn.execute(
            "SELECT * FROM job_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()

        disk_mb = 0.0
        photo_path = Path(photo_dir)
        for subdir in self._SCAN_DIRS:
            sub = photo_path / subdir
            if sub.exists():
                disk_mb += sum(
                    f.stat().st_size for f in sub.rglob("*") if f.is_file()
                ) / (1024 * 1024)

        return Stats(
            total_photos=total_photos,
            total_videos=total_videos,
            total_entries=total_entries,
            entries_by_source=entries_by_source,
            total_runs=runs["total"] if runs else 0,
            successful_runs=runs["ok"] or 0 if runs else 0,
            failed_runs=runs["fail"] or 0 if runs else 0,
            last_run=self._row_to_job(last) if last else None,
            last_success=self._row_to_job(last_ok) if last_ok else None,
            disk_usage_mb=round(disk_mb, 1),
        )
