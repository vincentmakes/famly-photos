# CLAUDE.md

## Project overview

Famly Photos is a self-hosted Docker app that automatically downloads tagged photos, Learning Journey observations (with videos), and other content from the [Famly](https://famly.co) childcare platform. It provides a web gallery, journey timeline, and dashboard UI. Photos are stored in any host directory mounted into the container.

## Architecture

```
Docker container (port 8811)
├── FastAPI app (main.py)
│   ├── Gallery     GET /            (masonry grid + lightbox, tagged photos)
│   ├── Journey     GET /journey     (vertical feed of observations)
│   ├── Dashboard   GET /dashboard   (stats, job history, controls)
│   ├── Photo serve GET /photos/*    (serves from mounted photo dir)
│   ├── Health      GET /health
│   └── API         POST /api/*      (fetch-now, refresh-token, rescan, purge-source,
│                                      purge-all, cleanup, upload)
│                   GET  /api/*      (stats, gallery-page)
├── APScheduler → _do_fetch() every N hours
├── FamlyAuth (auth.py) → GraphQL login + token cache
├── Fetcher (fetcher.py) → downloads from Famly APIs
└── SQLite DB (db.py) → tracks photos, content entries, job runs

Volumes:
  /photos → host directory via HOST_PHOTOS_PATH env var (tagged/ and journey/ subdirectories)
  /appdata/data → Docker volume (famly-photos.db + token.json + device_id)
```

## Key files

| File | Purpose |
|---|---|
| `src/config.py` | Pydantic Settings for env vars / `.env`, plus `DB_PATH` constant (hardcoded, not configurable) |
| `src/auth.py` | Famly auth via GraphQL `Authenticate` mutation at `/graphql`. Handles `AuthenticationSucceeded`, `AuthenticationChallenged` (multi-context), and `AuthenticationFailed`. Caches token to `/appdata/data/token.json`. Persists a stable `device_id` file in the same directory. Also exposes `graphql()` helper for journey/notes queries |
| `src/fetcher.py` | Multi-source fetcher. Tagged photos via REST `/api/v2/images/tagged`. Journey + notes via GraphQL. Feed via REST `/api/feed/feed/feed`. Messages via REST `/api/v2/conversations`. Downloads into subdirectories: `tagged/`, `journey/`. Extracts EXIF dates via Pillow. Builds a feed date map for tagging dates onto tagged photos |
| `src/db.py` | SQLite with 3 tables: `photos` (filename, source_url, content_id), `content_entries` (id, source, title, body, author, created_at, video_url), `job_runs`. Auto-migrates from v1 schema. Scan method indexes existing files on disk. Includes cleanup/purge methods |
| `src/main.py` | FastAPI app, routes, scheduler setup, lifespan (startup cleanup + scan + scheduler). File upload endpoint with EXIF date extraction |
| `src/templates/gallery.html` | Masonry photo grid with infinite scroll, lightbox with notes sidebar, video play overlay |
| `src/templates/dashboard.html` | Stats, job history, controls (Fetch Now, Refresh Token, Rescan Disk) |
| `src/templates/timeline.html` | Vertical card feed of observations/notes with inline photos/videos |
| `pyproject.toml` | Project metadata, dependencies, Ruff linter config |
| `docker-compose.yml` | Single service, health check, volume mounts |
| `Dockerfile` | Python 3.12-slim, pip install, runs `src/main.py` |

## Routes

| Method | Path | Description |
|---|---|---|
| GET | `/` | Photo gallery (tagged photos, masonry grid with infinite scroll) |
| GET | `/journey` | Journey timeline (observations with photos/videos) |
| GET | `/timeline` | Legacy redirect → `/journey` (301) |
| GET | `/dashboard` | Admin dashboard with stats, job history, controls |
| GET | `/photos/{path}` | Serves photos/videos from the mounted directory |
| GET | `/health` | Health check (token status) |
| GET | `/api/stats` | JSON stats |
| GET | `/api/gallery-page` | Infinite scroll pagination (offset/limit params) |
| POST | `/api/fetch-now` | Trigger manual fetch |
| POST | `/api/refresh-token` | Force token refresh |
| POST | `/api/rescan` | Re-index existing photos from disk into DB |
| POST | `/api/purge-source` | Delete all content + photos for a source |
| POST | `/api/purge-all` | Delete everything + trigger fresh fetch (requires admin_password if set) |
| POST | `/api/cleanup` | Remove DB entries for files no longer on disk |
| POST | `/api/upload` | Upload photos/videos (EXIF date extraction, 200MB limit) |

## Famly API details

**Authentication**: GraphQL mutation `Authenticate` at `POST /graphql?Authenticate`. Sends email, password, deviceId. Returns `AuthenticationSucceeded` with `accessToken`, or `AuthenticationChallenged` requiring a follow-up `ChooseContext` mutation. No REST login endpoint exists.

**Tagged photos**: `GET /api/v2/images/tagged?childId=...` — REST, returns list with `prefix`, `key`, `width`, `height` fields. URL pattern: `{prefix}/{width}x{height}/{key}`.

**Journey/Observations**: GraphQL `LearningJourneyQuery` — returns `results[]` with `remark.body`, `createdBy.name.fullName`, `status.createdAt`, `images[].secret` (prefix/key/path/expires), `videos[].videoUrl`. No stable observation ID is exposed as a queryable field — deduplication uses a SHA-256 hash of `(source, createdAt, author, firstImageId, bodyPrefix)`.

**Notes**: GraphQL `GetChildNotes` — similar structure, same hashing approach for dedup.

**Feed**: `GET /api/feed/feed/feed` — REST, paginated via `cursor` param, returns `feedItems[]`. Also used to build a date map for tagged photos (cross-referencing filenames to get `takenAt` dates).

**Messages**: `GET /api/v2/conversations` then `GET /api/v2/conversations/{id}` — REST.

**Image URLs from GraphQL**: Use the `secret` block: `{prefix}/{key}/{path}?expires={expires}`. Different from tagged images which use `{prefix}/{key}`.

## Storage layout

```
/photos/                  (host-mounted directory)
├── _legacy/              (old root-level photos moved here by purge-all)
├── tagged/               (tagged photos + manual uploads)
│   └── *.jpg
└── journey/              (observations + notes: photos + videos)
    ├── *.jpg
    └── *.mp4
```

DB stores relative paths including subfolder: `tagged/abc.jpg`, `journey/vid.mp4`. The `/photos/{filename:path}` route serves files using these relative paths.

## Content deduplication

- **Tagged photos**: deduped by `_stable_id("tagged", filename)` — deterministic hash from the filename derived from the download URL
- **Journey/Notes**: deduped by deterministic hash via `_stable_id()` in fetcher.py — `sha256(source + createdAt + author + firstImageId + bodyPrefix)`. This prevents duplicate content entries across runs since the GraphQL API doesn't expose a stable observation ID
- **Feed/Messages**: deduped by `feedItemId` / `messageId` from the REST API
- **Manual uploads**: deduped by `sha256("manual|" + relative_path)`

## Date resolution for tagged photos

Tagged photos have no reliable date from the REST API. The fetcher resolves dates in priority order:
1. API response fields (`takenAt`, `createdAt`, `createdDate`, `date`)
2. Feed date map — cross-references filenames against the full feed to find `takenAt`
3. EXIF `DateTimeOriginal` / `DateTimeDigitized` extracted via Pillow

## Running locally

```bash
cp .env.example .env  # fill in FAMLY_EMAIL, FAMLY_PASSWORD, FAMLY_CHILD_ID, HOST_PHOTOS_PATH
docker compose up -d --build
# Gallery:    http://localhost:8811
# Journey:    http://localhost:8811/journey
# Dashboard:  http://localhost:8811/dashboard
```

## Common operations

```bash
# Purge a content source (deletes DB entries + files from disk)
curl -X POST "http://localhost:8811/api/purge-source?source=journey"

# Purge everything and re-fetch (moves legacy photos to _legacy/)
curl -X POST "http://localhost:8811/api/purge-all?password=YOUR_ADMIN_PASSWORD"

# Force re-index existing files on disk into DB
curl -X POST "http://localhost:8811/api/rescan"

# Clean up DB entries for missing files
curl -X POST "http://localhost:8811/api/cleanup"

# Manual fetch trigger
curl -X POST "http://localhost:8811/api/fetch-now"

# Force token refresh
curl -X POST "http://localhost:8811/api/refresh-token"

# Upload photos manually
curl -X POST "http://localhost:8811/api/upload" -F "files=@photo.jpg"
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `FAMLY_EMAIL` | (required) | Parent account email |
| `FAMLY_PASSWORD` | (required) | Parent account password |
| `FAMLY_CHILD_ID` | (required) | Child UUID |
| `FAMLY_ACCESS_TOKEN` | | Static token (skips email/password login, for 2FA accounts) |
| `FAMLY_INSTALLATION_ID` | *(auto-generated)* | Installation UUID (usually stable) |
| `FAMLY_BASE_URL` | `https://app.famly.co` | Backend base URL. Override for Famly-backed portals like Bright Horizons (`https://familyapp.brighthorizons.co.uk`). GraphQL endpoint is derived as `{base}/graphql` |
| `HOST_PHOTOS_PATH` | | Host path for docker-compose volume mount |
| `PHOTO_DIR` | `/photos` | Container path for photos |
| `FETCH_INTERVAL_HOURS` | `6` | Hours between auto-fetches |
| `FETCH_TAGGED` | `true` | Fetch tagged photos |
| `FETCH_JOURNEY` | `true` | Fetch journey observations |
| `FETCH_FEED` | `false` | Fetch feed items (no UI yet) |
| `FETCH_NOTES` | `false` | Fetch child notes (no UI yet) |
| `FETCH_MESSAGES` | `false` | Fetch conversation messages (no UI yet) |
| `APP_PORT` | `8811` | Server port |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ADMIN_PASSWORD` | | Password for purge-all endpoint (empty = no protection) |

## Deployment

Runs as a Docker container on any Linux/macOS/Windows host. The photos volume (`HOST_PHOTOS_PATH` in `.env`) can point to any local directory, network share (SMB/NFS), or NAS mount. The SQLite DB path is hardcoded as a constant in `src/config.py` (`DB_PATH`). If using a NAS like Synology, its photo indexer can auto-index the `tagged/` and `journey/` subdirectories.

## Tech stack

- Python 3.12, FastAPI, Jinja2, APScheduler, requests, pydantic-settings
- Pillow for EXIF date extraction, python-multipart for file uploads
- SQLite (single file, no migrations framework — schema auto-created in `db._migrate()`)
- Ruff for linting (configured in `pyproject.toml`: E, F, I, N, W, UP, B, C4, SIM rules)
- No JS framework — vanilla JS in templates, CSS-only styling (dark theme)

## Linting

```bash
ruff check src/     # lint
ruff format src/    # format
```

Ruff is configured in `pyproject.toml` targeting Python 3.12 with 88-char line length.

## Gotchas

- Famly's GraphQL schema does not expose `observationId` as a queryable field on `Observation` results — requesting it causes a 400 error
- Video `<video>` tags need `preload="metadata"` and explicit dimensions/background or they render as invisible 0-height elements
- Gallery passes `GalleryItem` dataclasses to Jinja — must convert to dicts via `asdict()` before `tojson` filter
- The `secret` image URL format has an `expires` parameter — URLs are time-limited. Downloaded files are permanent but re-fetching the same observation later may yield different URLs for the same image
- Famly sessions expire; the auth module auto-refreshes on 401/403, but if using `FAMLY_ACCESS_TOKEN` (static token for 2FA accounts), manual rotation is needed when it expires
- `Path.rename()` fails across filesystem boundaries (e.g. network mounts) — use `shutil.move()` instead
- The `_legacy/` and `_SKIP_DIRS` directories are excluded from scanning to avoid indexing NAS metadata or backup files (e.g. Synology's `@eaDir`, `#recycle`)
- Journey and notes both download media to the `journey/` subfolder (not separate directories)
- The fetcher always attempts media downloads even if the content entry already exists, to retry previously failed downloads
