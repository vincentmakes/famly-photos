"""Famly photo/video/content fetcher — tagged + journey with subdirectory storage."""

from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path, PurePosixPath

import requests

try:
    from PIL import Image
    from PIL.ExifTags import Base as ExifBase
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

from auth import FamlyAuth
from config import settings
from db import Database

logger = logging.getLogger("famly.fetcher")

BASE = settings.famly_base_url.rstrip("/")


def _exif_date(filepath: Path) -> str:
    """Extract EXIF DateTimeOriginal from a JPEG file.

    Returns ISO-ish string like '2024-03-15T10:30:00' or '' if unavailable.
    """
    if not _HAS_PIL:
        return ""
    try:
        with Image.open(filepath) as img:
            exif = img.getexif()
            # DateTimeOriginal (tag 36867) or DateTimeDigitized (36868)
            raw = exif.get(ExifBase.DateTimeOriginal) or exif.get(ExifBase.DateTimeDigitized) or ""
            if raw:
                # EXIF format is "YYYY:MM:DD HH:MM:SS" → convert to ISO
                return raw.replace(":", "-", 2).replace(" ", "T", 1)
    except Exception:
        pass
    return ""


def _stable_id(*parts: str) -> str:
    """Build a deterministic content ID from observation fields."""
    raw = "|".join(p or "" for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

# ── GraphQL queries ──────────────────────────────────────────────────────

JOURNEY_QUERY = """
query LearningJourneyQuery($childId: ChildId!, $variants: [ObservationVariant!], $next: ObservationCursor, $first: Int!) {
  childDevelopment {
    observations(
      first: $first
      childIds: [$childId]
      statuses: [SENT]
      variants: $variants
      after: $next
    ) {
      results {
        children { name }
        createdBy { name { fullName } }
        status { createdAt }
        variant
        remark { body }
        images {
          height width id
          secret { crop expires key path prefix }
        }
        videos {
          ... on TranscodingVideo { id }
          ... on TranscodedVideo { duration height id thumbnailUrl videoUrl width }
        }
      }
      next
    }
  }
}
"""

NOTES_QUERY = """
query GetChildNotes($childId: ChildId!, $cursor: ChildNoteCursor, $limit: Int!, $parentVisible: Boolean, $safeguardingConcern: Boolean, $sensitive: Boolean, $noteTypes: [ChildNoteType!]) {
  childNotes(
    childIds: [$childId]
    cursor: $cursor
    limit: $limit
    parentVisible: $parentVisible
    safeguardingConcern: $safeguardingConcern
    sensitive: $sensitive
    noteTypes: $noteTypes
  ) {
    next
    result {
      noteType
      createdBy { name { fullName } }
      text
      createdAt
      publishedAt
      images {
        height id width
        secret { crop expires key path prefix }
      }
    }
  }
}
"""


# ── URL helpers ──────────────────────────────────────────────────────────

def _secret_image_url(img: dict) -> str | None:
    """Build URL from a GraphQL 'secret' image dict."""
    s = img.get("secret")
    if not s:
        return None
    prefix = s.get("prefix", "")
    key = s.get("key", "")
    path = s.get("path", "")
    expires = s.get("expires", "")
    if prefix and key and path:
        return f"{prefix}/{key}/{path}?expires={expires}"
    if prefix and key:
        return f"{prefix}/{key}"
    return None


def _tagged_image_urls(item: dict) -> list[str]:
    """Candidate URLs for a tagged-images item (REST endpoint)."""
    urls: list[str] = []
    if {"prefix", "key", "width", "height"} <= item.keys():
        urls.append(f"{item['prefix']}/{item['width']}x{item['height']}/{item['key']}")
    if {"prefix", "key"} <= item.keys():
        urls.append(f"{item['prefix']}/{item['key']}")
    for k in ("downloadUrl", "url_big", "urlOriginal"):
        if k in item and isinstance(item[k], str):
            urls.append(item[k])
    if "big" in item and isinstance(item["big"], dict) and "url" in item["big"]:
        urls.append(item["big"]["url"])
    if "url" in item and isinstance(item["url"], str):
        urls.append(item["url"])
    return urls


def _filename_from_url(url: str, fallback_ext: str = ".jpg") -> str:
    """Extract filename from URL, with a fallback for ugly URLs."""
    name = PurePosixPath(url.split("?", 1)[0]).name
    if not name or len(name) < 3 or "." not in name:
        # URL has no usable filename — generate one
        name = f"{uuid.uuid4().hex[:12]}{fallback_ext}"
    return name


def _download_file(
    sess: requests.Session,
    url: str,
    dest_dir: Path,
    db: Database,
    *,
    subfolder: str = "",
    content_id: str | None = None,
    fallback_ext: str = ".jpg",
) -> tuple[bool, bool]:
    """Download a file into dest_dir/subfolder.

    Returns (success, was_new). success=True if file exists or was downloaded.
    was_new=True if it was actually downloaded this run.
    """
    raw_name = _filename_from_url(url, fallback_ext)
    # DB stores relative path including subfolder: "journey/abc.jpg"
    rel_name = f"{subfolder}/{raw_name}" if subfolder else raw_name
    full_path = dest_dir / rel_name

    if full_path.exists() or db.photo_exists(rel_name):
        # Link existing photo to content entry if not already linked
        if content_id:
            db.link_photo_content(rel_name, content_id)
        return True, False

    full_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sess.get(url, stream=True, timeout=60) as r:
            if r.status_code == 404:
                return False, False
            r.raise_for_status()

            # Check content-type for video detection
            ct = r.headers.get("content-type", "")
            if fallback_ext == ".jpg" and "video" in ct:
                # Rename to .mp4 if server says it's a video
                raw_name = raw_name.rsplit(".", 1)[0] + ".mp4"
                rel_name = f"{subfolder}/{raw_name}" if subfolder else raw_name
                full_path = dest_dir / rel_name
                if full_path.exists() or db.photo_exists(rel_name):
                    return True, False

            with full_path.open("wb") as fp:
                for chunk in r.iter_content(8192):
                    fp.write(chunk)

        db.record_photo(rel_name, url, content_id)
        logger.info("Downloaded %s (%.1f KB)", rel_name, full_path.stat().st_size / 1024)
        return True, True
    except requests.RequestException as exc:
        logger.warning("Failed %s: %s", rel_name, exc)
        if full_path.exists():
            full_path.unlink()
        return False, False


# ── Date parsing ─────────────────────────────────────────────────────────

def _parse_api_date(item: dict) -> str:
    """Extract date from an API item.

    Handles both plain string fields and nested objects like:
    {"date": "2026-03-18 11:35:55.000000", "timezone_type": 3, "timezone": "UTC"}
    """
    for key in ("takenAt", "createdAt", "createdDate", "created", "date"):
        val = item.get(key)
        if not val:
            continue
        if isinstance(val, str):
            return val
        if isinstance(val, dict) and "date" in val:
            # "2026-03-18 11:35:55.000000" → "2026-03-18T11:35:55"
            raw = val["date"]
            return raw.split(".")[0].replace(" ", "T")
    return ""


# ── Feed date map (for tagging dates onto tagged photos) ─────────────────

def _build_feed_date_map(
    sess: requests.Session,
) -> dict[str, str]:
    """Fetch all feed pages and build filename → date map.

    Uses takenAt (preferred) or createdDate from feed items.
    """
    date_map: dict[str, str] = {}
    cursor: str | None = None

    while True:
        params: dict = {"first": "50"}
        if cursor:
            params["cursor"] = cursor

        logger.info("[feed-date-map] fetching, cursor=%s", cursor)
        resp = sess.get(f"{BASE}/api/feed/feed/feed", params=params, timeout=30)
        if resp.status_code in (404, 400):
            break
        resp.raise_for_status()
        data = resp.json()

        feed_items = data.get("feedItems") or []
        if not feed_items:
            break
        new_cursor = feed_items[-1].get("feedItemId")
        if new_cursor == cursor:
            break  # no progress, end of feed
        cursor = new_cursor

        for itm in feed_items:
            best_date = _parse_api_date(itm)
            if not best_date:
                continue
            for img in itm.get("images", []):
                for u in _tagged_image_urls(img):
                    fname = _filename_from_url(u)
                    if fname not in date_map:
                        date_map[fname] = best_date

        if not cursor:
            break

    logger.info("[feed-date-map] built map with %d entries", len(date_map))
    return date_map


# ── Tagged photos (REST) ─────────────────────────────────────────────────

def _fetch_tagged(
    sess: requests.Session, db: Database, child_id: str, photo_dir: Path,
    feed_date_map: dict[str, str] | None = None,
) -> tuple[int, int]:
    url: str | None = f"{BASE}/api/v2/images/tagged?childId={child_id}"
    downloaded = skipped = 0

    while url:
        logger.info("[tagged] %s", url)
        resp = sess.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        items = data if isinstance(data, list) else data.get("images") or data.get("items") or []
        url = data.get("paging", {}).get("next") if isinstance(data, dict) else None

        for itm in items:
            # Try API response fields first for date
            created = _parse_api_date(itm)

            # Use first candidate URL as part of stable ID
            urls = _tagged_image_urls(itm)
            first_url = urls[0] if urls else ""
            raw_name = _filename_from_url(first_url) if first_url else ""
            cid = _stable_id("tagged", raw_name)

            found = False
            for cand in urls:
                ok, is_new = _download_file(
                    sess, cand, photo_dir, db,
                    subfolder="tagged", content_id=cid,
                )
                if ok:
                    # If API had no date, try feed date map, then EXIF
                    if not created and feed_date_map:
                        fname = _filename_from_url(cand)
                        created = feed_date_map.get(fname, "")
                    if not created:
                        rel_name = f"tagged/{_filename_from_url(cand)}"
                        exif_dt = _exif_date(photo_dir / rel_name)
                        if exif_dt:
                            created = exif_dt

                    if is_new:
                        downloaded += 1
                    else:
                        skipped += 1
                    found = True
                    break
            if not found:
                skipped += 1

            # Create/update content entry with best available date
            db.upsert_content(
                content_id=cid, source="tagged", title="",
                body="", author="", created_at=created,
            )

    return downloaded, skipped


# ── Journey (GraphQL) ────────────────────────────────────────────────────

def _fetch_journey(
    auth: FamlyAuth, db: Database, child_id: str, photo_dir: Path
) -> tuple[int, int]:
    downloaded = skipped = 0
    cursor: str | None = None
    sess = auth.get_session()

    while True:
        logger.info("[journey] fetching page, cursor=%s", cursor)
        try:
            data = auth.graphql(
                "LearningJourneyQuery",
                JOURNEY_QUERY,
                {
                    "childId": child_id,
                    "variants": ["REGULAR_OBSERVATION", "PARENT_OBSERVATION"],
                    "first": 50,
                    "next": cursor,
                },
            )
        except Exception as exc:
            logger.warning("[journey] GraphQL failed: %s", exc)
            break

        obs = data.get("childDevelopment", {}).get("observations", {})
        results = obs.get("results") or []
        cursor = obs.get("next")

        if not results:
            break

        for item in results:
            body = ""
            if item.get("remark"):
                body = item["remark"].get("body") or ""
            author = ""
            if item.get("createdBy"):
                author = item["createdBy"].get("name", {}).get("fullName", "")
            created = ""
            if item.get("status"):
                created = item["status"].get("createdAt", "")

            # Build stable ID from first image ID + timestamp + author
            first_img_id = ""
            if item.get("images"):
                first_img_id = item["images"][0].get("id", "")
            cid = _stable_id("journey", created, author, first_img_id, body[:100])

            # Collect video URLs
            video_url = None
            for vid in item.get("videos", []):
                vu = vid.get("videoUrl")
                if vu:
                    video_url = vu
                    break

            # Store/update content entry
            if not db.content_exists(cid):
                db.upsert_content(
                    content_id=cid, source="journey", title="",
                    body=body, author=author, created_at=created,
                    video_url=video_url,
                )

            # Always attempt media downloads — _download_file deduplicates.
            # Previously this was skipped when content existed, so failed
            # video/image downloads were never retried on subsequent runs.

            # Download images
            for img in item.get("images", []):
                url = _secret_image_url(img)
                if url:
                    ok, is_new = _download_file(
                        sess, url, photo_dir, db,
                        subfolder="journey", content_id=cid,
                    )
                    if is_new:
                        downloaded += 1
                    elif ok:
                        skipped += 1

            # Download videos
            if video_url:
                logger.info("[journey] downloading video: %s", video_url[:80])
                ok, is_new = _download_file(
                    sess, video_url, photo_dir, db,
                    subfolder="journey", content_id=cid,
                    fallback_ext=".mp4",
                )
                if is_new:
                    downloaded += 1
                elif ok:
                    skipped += 1

        if not cursor:
            break

    return downloaded, skipped


# ── Notes (GraphQL) ──────────────────────────────────────────────────────

def _fetch_notes(
    auth: FamlyAuth, db: Database, child_id: str, photo_dir: Path
) -> tuple[int, int]:
    downloaded = skipped = 0
    cursor: str | None = None
    sess = auth.get_session()

    while True:
        logger.info("[notes] fetching page, cursor=%s", cursor)
        try:
            data = auth.graphql(
                "GetChildNotes",
                NOTES_QUERY,
                {
                    "childId": child_id,
                    "noteTypes": ["Classic"],
                    "parentVisible": True,
                    "safeguardingConcern": False,
                    "sensitive": False,
                    "limit": 50,
                    "cursor": cursor,
                },
            )
        except Exception as exc:
            logger.warning("[notes] GraphQL failed: %s", exc)
            break

        notes_data = data.get("childNotes", {})
        results = notes_data.get("result") or []
        cursor = notes_data.get("next")

        if not results:
            break

        for note in results:
            body = note.get("text") or ""
            author = ""
            if note.get("createdBy"):
                author = note["createdBy"].get("name", {}).get("fullName", "")
            created = note.get("createdAt") or note.get("publishedAt") or ""

            first_img_id = ""
            if note.get("images"):
                first_img_id = note["images"][0].get("id", "")
            cid = _stable_id("note", created, author, first_img_id, body[:100])

            if not db.content_exists(cid):
                db.upsert_content(
                    content_id=cid, source="note", title="",
                    body=body, author=author, created_at=created,
                )

            # Always attempt media downloads (retries previously failed ones)
            for img in note.get("images", []):
                url = _secret_image_url(img)
                if url:
                    ok, is_new = _download_file(
                        sess, url, photo_dir, db,
                        subfolder="journey", content_id=cid,
                    )
                    if is_new:
                        downloaded += 1
                    elif ok:
                        skipped += 1

        if not cursor:
            break

    return downloaded, skipped


# ── Feed (REST) ──────────────────────────────────────────────────────────

def _fetch_feed(
    sess: requests.Session, db: Database, child_id: str, photo_dir: Path
) -> tuple[int, int]:
    downloaded = skipped = 0
    cursor: str | None = None

    while True:
        params: dict = {"first": "50"}
        if cursor:
            params["cursor"] = cursor

        logger.info("[feed] fetching, cursor=%s", cursor)
        resp = sess.get(f"{BASE}/api/feed/feed/feed", params=params, timeout=30)
        if resp.status_code in (404, 400):
            break
        resp.raise_for_status()
        data = resp.json()

        feed_items = data.get("feedItems") or []
        if not feed_items:
            break
        cursor = feed_items[-1].get("feedItemId")

        for itm in feed_items:
            cid = itm.get("feedItemId") or str(uuid.uuid4())
            if db.content_exists(cid):
                skipped += 1
                continue

            db.upsert_content(
                content_id=cid, source="feed", title="",
                body=itm.get("body") or "", author="",
                created_at=itm.get("createdDate") or "",
            )

            for img in itm.get("images", []):
                for u in _tagged_image_urls(img):
                    ok, is_new = _download_file(
                        sess, u, photo_dir, db,
                        subfolder="feed", content_id=cid,
                    )
                    if ok:
                        if is_new:
                            downloaded += 1
                        break

        if not cursor:
            break

    return downloaded, skipped


# ── Messages (REST) ──────────────────────────────────────────────────────

def _fetch_messages(
    sess: requests.Session, db: Database, child_id: str, photo_dir: Path
) -> tuple[int, int]:
    downloaded = skipped = 0

    resp = sess.get(f"{BASE}/api/v2/conversations", timeout=30)
    if resp.status_code in (404, 400):
        return 0, 0
    resp.raise_for_status()
    conversations = resp.json()
    if not isinstance(conversations, list):
        conversations = conversations.get("conversations") or []

    for conv in conversations:
        conv_id = conv.get("conversationId")
        if not conv_id:
            continue
        resp2 = sess.get(f"{BASE}/api/v2/conversations/{conv_id}", timeout=30)
        if resp2.status_code != 200:
            continue

        for msg in resp2.json().get("messages", []):
            cid = msg.get("messageId") or str(uuid.uuid4())
            if db.content_exists(cid):
                skipped += 1
                continue
            db.upsert_content(
                content_id=cid, source="message", title="",
                body=msg.get("body") or "", author="",
                created_at=msg.get("createdDate") or "",
            )
            for img in msg.get("images", []):
                for u in _tagged_image_urls(img):
                    ok, is_new = _download_file(
                        sess, u, photo_dir, db,
                        subfolder="messages", content_id=cid,
                    )
                    if ok:
                        if is_new:
                            downloaded += 1
                        break

    return downloaded, skipped


# ── Main entry point ─────────────────────────────────────────────────────

def run_fetch(
    auth: FamlyAuth,
    db: Database,
    child_id: str,
    photo_dir: str,
    *,
    fetch_tagged: bool = True,
    fetch_feed: bool = False,
    fetch_journey: bool = True,
    fetch_notes: bool = False,
    fetch_messages: bool = False,
) -> tuple[int, int]:
    photo_path = Path(photo_dir)
    photo_path.mkdir(parents=True, exist_ok=True)

    sess = auth.get_session()
    total_dl = total_skip = 0
    retried = False

    # Build feed date map first so tagged photos can use feed dates
    feed_date_map: dict[str, str] = {}
    if fetch_tagged:
        try:
            feed_date_map = _build_feed_date_map(sess)
        except Exception as exc:
            logger.warning("Failed to build feed date map: %s", exc)

    sources: list[tuple[str, bool, object]] = [
        ("tagged", fetch_tagged, lambda: _fetch_tagged(sess, db, child_id, photo_path, feed_date_map)),
        ("journey", fetch_journey, lambda: _fetch_journey(auth, db, child_id, photo_path)),
        ("feed", fetch_feed, lambda: _fetch_feed(sess, db, child_id, photo_path)),
        ("notes", fetch_notes, lambda: _fetch_notes(auth, db, child_id, photo_path)),
        ("messages", fetch_messages, lambda: _fetch_messages(sess, db, child_id, photo_path)),
    ]

    for name, enabled, fetcher in sources:
        if not enabled:
            continue
        try:
            dl, skip = fetcher()
            total_dl += dl
            total_skip += skip
            logger.info("[%s] %d downloaded, %d skipped", name, dl, skip)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (401, 403) and not retried:
                logger.warning("Auth error on %s – refreshing", name)
                auth.refresh()
                sess = auth.get_session()
                retried = True
                try:
                    dl, skip = fetcher()
                    total_dl += dl
                    total_skip += skip
                except Exception as inner:
                    logger.error("[%s] failed after retry: %s", name, inner)
            else:
                logger.error("[%s] failed: %s", name, exc)
        except Exception as exc:
            logger.error("[%s] failed: %s", name, exc)

    logger.info("Total: %d downloaded, %d skipped", total_dl, total_skip)
    return total_dl, total_skip
