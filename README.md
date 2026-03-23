

## Famly Photos   
<img width="100" height="100" alt="famly-photos" src="https://github.com/user-attachments/assets/8572b1b9-4437-4845-9ae2-3651b856f603" />  

A self-hosted Docker app that automatically saves your child's photos, videos, and Learning Journey content from [Famly](https://famly.co) to your own storage.

## Why?

[Famly](https://famly.co) is a platform used by nurseries and childcare providers to share photos, observations, and updates with parents. It's great — but all that content lives on their servers, and there's no export or download feature. If your nursery switches platforms, or Famly changes their policies, those precious photos and journey entries could disappear.

**Famly Photos** solves this by automatically downloading everything to a directory you control — a local folder, a NAS, wherever you want. It runs on a schedule, so new content is picked up automatically. You also get a built-in gallery and journey timeline to browse everything locally.


## What it downloads

- **Tagged photos** — photos your nursery tags with your child's name
- **Learning Journey** — observations with photos, videos, and teacher notes

All content is deduplicated across runs, so you can safely restart or re-run without getting duplicates.

> The backend also supports fetching feed items, notes, and messages (`FETCH_FEED`, `FETCH_NOTES`, `FETCH_MESSAGES` env vars), but there is no UI for browsing those yet. They are disabled by default.

<img width="250" height="689" alt="Screenshot 2026-03-21 at 16 37 50" src="https://github.com/user-attachments/assets/8b4dd726-79b4-4d59-a6cb-de4a59d7064e" />
<img width="250" height="688" alt="Screenshot 2026-03-21 at 16 37 39" src="https://github.com/user-attachments/assets/7e3e1f9f-a81c-4ef9-9ecc-e758a304bb66" />  

## Features

- **Auto-login**: logs in with email/password via GraphQL and refreshes the token automatically
- **Scheduled fetching**: checks for new content every N hours (default: 6)
- **Photo gallery**: masonry grid with infinite scroll, lightbox with observation notes alongside each photo
- **Journey timeline**: vertical feed of observations and notes with inline photos and videos
- **Dashboard**: stats, run history, token status, admin controls
- **Manual upload**: upload your own photos via the API
- **Smart date resolution**: cross-references feed dates and EXIF data for accurate photo dates
- **NAS-friendly**: photos directory can be a local folder or network mount — works with Synology Photos, Immich, or any photo indexer
- **Health endpoint**: `GET /health` for container monitoring

## Prerequisites

- Docker and Docker Compose
- A Famly parent account (email + password)
- Your child's UUID (see [Finding your child ID](#finding-your-child-id) below)

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/vincentmakes/famly-photos.git
cd famly-photos

# 2. Configure
cp .env.example .env
# Edit .env with your Famly credentials, child ID, and photo storage path

# 3. Start the container
docker compose up -d --build

# 4. Open the app
# Gallery:    http://localhost:8811
# Journey:    http://localhost:8811/journey
# Dashboard:  http://localhost:8811/dashboard
```

## Tagging your child in Famly

For tagged photos to be downloaded, you need to **tag your child** in photos they upload. This is a standard Famly feature - Tagged photos then show up in your child's profile.

Journey observations, notes, and feed items are downloaded regardless of tagging — they're linked to your child's profile directly.

## Finding your child ID

You need your child's UUID to configure `FAMLY_CHILD_ID`. There are two ways to find it:

### Option 1: From the Famly web app URL

1. Log in to [app.famly.co](https://app.famly.co) in your browser
2. Navigate to your child's profile
3. The URL will look like: `https://app.famly.co/children/f12345-ecc6-4128-a491-a1b2c3d4e5f6`
4. The UUID after `/children/` is your child ID

### Option 2: From the browser network tab

1. Log in to [app.famly.co](https://app.famly.co)
2. Open your browser's Developer Tools (F12) and go to the **Network** tab
3. Browse around your child's profile or photos
4. Look for API requests containing `childId=` in the URL — the value is your child's UUID

## Multiple children

The app supports **one child per instance**. Each container has its own database, token cache, and photo directory, so there's no cross-contamination between children.

To set up multiple children, create a separate `.env` file for each child (same Famly credentials, different `FAMLY_CHILD_ID`), and run them as separate services with their own data volumes and ports:

```bash
# Create per-child env files
cp .env.example .env.child1
cp .env.example .env.child2
# Edit each with the correct FAMLY_CHILD_ID
```

```yaml
# docker-compose.yml
services:
  famly-alice:
    build: .
    env_file: .env.child1
    volumes:
      - ./data/alice:/appdata/data      # separate DB + token per child
      - /photos/alice:/photos           # separate photo storage
    ports:
      - "8811:8811"

  famly-bob:
    build: .
    env_file: .env.child2
    volumes:
      - ./data/bob:/appdata/data
      - /photos/bob:/photos
    ports:
      - "8812:8811"                     # different host port
```

Each instance gets its own gallery and dashboard at its own port (e.g. `:8811` for Alice, `:8812` for Bob). They share the same Famly login credentials but fetch content for different children.

## Pages

| URL | Description |
|---|---|
| `/` | Photo gallery — tagged photos in a masonry grid with lightbox |
| `/journey` | Journey timeline — observations and notes with photos/videos |
| `/dashboard` | Admin dashboard — stats, job history, controls |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `FAMLY_EMAIL` | *(required)* | Famly parent account email |
| `FAMLY_PASSWORD` | *(required)* | Famly parent account password |
| `FAMLY_CHILD_ID` | *(required)* | Child UUID (see [Finding your child ID](#finding-your-child-id)) |
| `FAMLY_ACCESS_TOKEN` | | Static token (skips login — see [2FA accounts](#2fa-accounts)) |
| `HOST_PHOTOS_PATH` | *(required)* | Host path where photos are saved |
| `FETCH_INTERVAL_HOURS` | `6` | Hours between auto-fetches |
| `FETCH_TAGGED` | `true` | Fetch tagged photos |
| `FETCH_JOURNEY` | `true` | Fetch Learning Journey observations |
| `APP_PORT` | `8811` | Server port |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ADMIN_PASSWORD` | | Password for purge-all endpoint (empty = no protection) |

## Storage Layout

```
/photos/                  (your HOST_PHOTOS_PATH)
├── tagged/               (tagged photos + manual uploads)
│   └── *.jpg
└── journey/              (observations + notes: photos + videos)
    ├── *.jpg
    └── *.mp4
```

If you point `HOST_PHOTOS_PATH` at a NAS photo directory (e.g. Synology Photos, Immich watch folder), downloaded photos will be automatically indexed by your photo app.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Photo gallery |
| `GET` | `/journey` | Journey timeline |
| `GET` | `/dashboard` | Admin dashboard |
| `GET` | `/photos/{path}` | Serve photo/video from storage |
| `GET` | `/health` | Health check |
| `GET` | `/api/stats` | JSON stats |
| `GET` | `/api/gallery-page` | Paginated gallery items (offset/limit) |
| `POST` | `/api/fetch-now` | Trigger manual fetch |
| `POST` | `/api/refresh-token` | Force token refresh |
| `POST` | `/api/rescan` | Re-index photos from disk into DB |
| `POST` | `/api/purge-source` | Delete all content + photos for a source |
| `POST` | `/api/purge-all` | Delete everything + trigger fresh fetch |
| `POST` | `/api/cleanup` | Remove DB entries for missing files |
| `POST` | `/api/upload` | Upload photos/videos (max 200MB per file) |

## 2FA accounts

If your Famly account has two-factor authentication enabled, the automatic email/password login won't work. Instead:

1. Log in to Famly in your browser
2. Open Developer Tools (F12) → Network tab
3. Look for any API request and find the `x-famly-accesstoken` header
4. Copy that token value into `FAMLY_ACCESS_TOKEN` in your `.env`

Note: this token will expire eventually and you'll need to repeat the process. The app cannot auto-refresh static tokens.

## Security

**This app has no authentication on its web UI or API.** Anyone who can reach the port can view all your child's photos, trigger downloads, upload files, or delete data. This is by design — it's a personal tool meant to run on your local network.

**Do not expose this app to the internet.** Keep it behind your firewall, on `localhost`, or on a trusted LAN only. If you need remote access, put it behind a reverse proxy with authentication (e.g. Authelia, Authentik, Caddy with basicauth, or a VPN like Tailscale/WireGuard).

Other security notes:

- **Credentials**: your Famly email/password are stored in the `.env` file and the access token is cached in plaintext at `/appdata/data/token.json`. Protect these files with appropriate filesystem permissions.
- **`ADMIN_PASSWORD`**: the purge-all endpoint can optionally require a password, but all other API endpoints (fetch, rescan, upload, cleanup) are unprotected.
- **Container isolation**: the app runs as a single container and only communicates outbound to `app.famly.co`. No inbound connections are needed except the web UI port.

## Notes

- **Privacy**: all data stays on your machine. The container only communicates with `app.famly.co`.
- **Multi-context accounts**: if your Famly account has access to multiple nurseries, the app automatically selects the first one during login.
- **Deduplication**: all content is deduplicated across runs — tagged photos by filename, journey/notes by a hash of content fields, feed/messages by their API IDs.
- **Famly API**: this app uses Famly's internal API (the same one their web app uses). It's not an official public API, so it could change. Tagged photos have been stable for years; the GraphQL endpoints for journey/notes are newer.
