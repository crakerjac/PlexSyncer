# PlexSyncer (Return of the Sync)

> Automated offline media sync from Plex to mobile devices via rclone/Syncthing and the Plezy Android app.

---

## Table of Contents

1. [Vision](#1-vision)
2. [How It Works](#2-how-it-works)
3. [System Architecture](#3-system-architecture)
4. [File and Path Conventions](#4-file-and-path-conventions)
5. [Manifest Format](#5-manifest-format)
6. [Configuration Slots](#6-configuration-slots)
7. [Python Worker — plex_hardlink_sync.py](#7-python-worker)
8. [Management UI — sync_ui.py](#8-management-ui)
9. [Plezy Fork — Android Changes](#9-plezy-fork)
10. [Subtitle Handling](#10-subtitle-handling)
11. [Watch State & Pruning Lifecycle](#11-watch-state--pruning-lifecycle)
12. [Deployment](#12-deployment)
13. [Mobile Sync Options](#13-mobile-sync-options)
14. [Platform Support](#14-platform-support)
15. [Open Questions / Future Work](#15-open-questions--future-work)

---

## 1. Vision

PlexSyncer automates the selection, hard-linking, and cleanup of Plex media content into
slot-specific directories that are transferred to mobile devices via rclone, Syncthing, or
any file sync tool of your choice. A forked build of the Plezy Android app reads a
`manifest.json` sidecar to register synced files as offline-available without requiring a
Plex connection on the phone.

The goal: curated offline content on a mobile device that stays fresh automatically,
with zero manual file management.

---

## 2. How It Works

```
┌─────────────────────────────────────────────────────────┐
│  Plex Media Server (Linux)                              │
│                                                         │
│  Smart Playlists / Show configs                         │
│         │                                               │
│         ▼                                               │
│  plex_hardlink_sync.py  (CRON or manual)                │
│    - Reads slot configs                                 │
│    - Resolves items (playlists, movies, Next X shows)   │
│    - Picks lowest-bitrate version of each item          │
│    - Creates hard links  ──────────────────────────┐    │
│    - Hard-links subtitle sidecars                  │    │
│    - Writes _plezy_meta/manifest.json              │    │
│    - Prunes removed/watched items                  │    │
│                                                    │    │
│  /sync_root/                                       │    │
│    MyPhone/               ◄────────────────────────┘    │
│      _plezy_meta/                                       │
│        manifest.json                                    │
│      TV Shows/...                                       │
│      Movies/...                                         │
└─────────────────┬───────────────────────────────────────┘
                  │  rclone sync → phone's PlexSyncer folder
                  ▼
┌─────────────────────────────────────────────────────────┐
│  Android Phone                                          │
│                                                         │
│  /storage/.../Plezy/PlexSyncer/                         │
│    _plezy_meta/manifest.json                            │
│    TV Shows/...                                         │
│    Movies/...                                           │
│         │                                               │
│         ▼  (user taps "Scan" in Plezy fork)             │
│  Plezy (forked)                                         │
│    - Reads manifest.json                                │
│    - Registers files as completed downloads             │
│    - Fetches artwork from Plex server (when online)     │
│    - Plays files offline via MPV                        │
│    - Syncs watch state back to Plex when online         │
│    - Prunes DB records for files removed by rclone      │
└─────────────────────────────────────────────────────────┘
```

The sync tool runs **one-way** (phone is destination only). There is no risk of
phone-side changes propagating back to the server. See §13 for sync tool options.

### Physical Separation

PlexSyncer files live in a dedicated `PlexSyncer` subfolder inside the Plezy SAF download
root. Plezy's own native downloads go to internal app storage. This means rclone can
sync freely without touching anything Plezy downloaded natively, and Plezy's scan button
only operates on the `PlexSyncer` folder — it never affects native downloads.

---

## 3. System Architecture

### Components

| Component | Language | Purpose |
|---|---|---|
| `plex_hardlink_sync.py` | Python | Core sync worker: hard links, manifest, pruning |
| `sync_ui.py` | Python / Streamlit | Web UI for slot configuration and sync triggering |
| Plezy fork | Dart / Flutter | Android app: manifest import, scan button, artwork fetch |
| rclone / Round Sync | — | File transport to phone (one-way, user-configured) |

### Slot Model

A **slot** represents one target device/profile. Each slot has its own:
- Configuration file (`configs/{slot_name}.json`)
- Sync directory on server (`{sync_root}/{slot_name}/`)
- rclone sync target pointing to `{Plezy SAF root}/PlexSyncer/` on the phone

This allows independent configurations for e.g. a tablet (kids content) and a phone
(adult content) from the same Plex server.

### Two-Repo Structure

| Repo | Contents |
|---|---|
| `PlexSyncer` (this repo) | Python worker, Streamlit UI, configs, install scripts |
| [`crakerjac/plezy`](https://github.com/crakerjac/plezy) | Forked Plezy Android app with PlexSyncer integration |

---

## 4. File and Path Conventions

Paths **must exactly match** what Plezy's own downloader produces. The sanitization rules
and directory structure are derived from `download_storage_service.dart`.

### Sanitization Rules

1. Remove characters: `< > : " / \ | ? *`
2. Remove leading and trailing dots
3. Replace all remaining dots with underscores
4. Trim whitespace

Examples:
- `A.L.F.` → `A_L_F`
- `Keepin' the Faith` → `Keepin' the Faith` *(apostrophe is preserved)*
- `2001: A Space Odyssey` → `2001 A Space Odyssey`

### Episode Path

```
TV Shows/{Show Title} ({Year})/Season {XX}/{SxxExx} - {Episode Title}.{ext}
```

Example:
```
TV Shows/ALF (1986)/Season 01/S01E01 - A_L_F.mp4
```

### Movie Path

```
Movies/{Movie Title} ({Year})/{Movie Title} ({Year}).{ext}
```

Example:
```
Movies/Dune (2021)/Dune (2021).mp4
```

### Subtitle Sidecar Path

```
{Video filename base}.{language_code}.{ext}
```

Example:
```
TV Shows/ALF (1986)/Season 01/S01E01 - A_L_F.en.srt
```

> **Open question:** Does Plezy's offline mode surface subtitle tracks from sidecar
> files in its subtitle picker UI? MPV auto-detects them for playback, but Plezy
> may need DB registration for them to appear in the subtitle picker. See §10.

---

## 5. Manifest Format

The manifest lives at `{slot_dir}/_plezy_meta/manifest.json` and is fully regenerated
on each sync run. The Plezy fork reads this file when the user taps "Scan" to register
all synced files as completed download records, and uses the manifest to prune DB records
for files that have since been removed by rclone.

```json
{
  "version": 1,
  "generatedAt": "1997-08-29T02:14:00Z",
  "serverId": "0123456789012345678901234567890123456789",
  "serverName": "PlexServer",
  "items": [
    {
      "ratingKey": "12345",
      "type": "episode",
      "title": "A.L.F.",
      "thumb": "/library/metadata/12345/thumb",
      "art": "/library/metadata/12345/art",
      "summary": "ALF accidentally reveals himself to a neighbor...",
      "duration": 1560000,
      "relativePath": "TV Shows/ALF (1986)/Season 01/S01E01 - A_L_F.mp4",
      "grandparentTitle": "ALF",
      "grandparentYear": 1986,
      "grandparentRatingKey": "111",
      "grandparentThumb": "/library/metadata/111/thumb",
      "grandparentArt": "/library/metadata/111/art",
      "parentTitle": "Season 1",
      "parentRatingKey": "222",
      "parentThumb": "/library/metadata/222/thumb",
      "seasonNumber": 1,
      "episodeNumber": 1
    },
    {
      "ratingKey": "67890",
      "type": "movie",
      "title": "Dune",
      "year": 2021,
      "thumb": "/library/metadata/67890/thumb",
      "art": "/library/metadata/67890/art",
      "summary": "A noble family becomes embroiled in a war...",
      "duration": 9360000,
      "relativePath": "Movies/Dune (2021)/Dune (2021).mp4"
    }
  ]
}
```

### Field Reference

| Field | Types | Purpose |
|---|---|---|
| `ratingKey` | all | Plex item ID; forms `serverId:ratingKey` global key |
| `type` | all | `"episode"` or `"movie"` |
| `title` | all | Display title |
| `thumb` | all | Server-relative poster path; fetched lazily by Plezy |
| `art` | all | Server-relative background/banner path; fetched lazily |
| `summary` | all | Overview text shown on detail screen |
| `duration` | all | Milliseconds; shown in duration badge |
| `relativePath` | all | Path relative to slot dir; used to resolve SAF URI |
| `year` | movie | Release year badge |
| `grandparentRatingKey` | episode | Show's Plex ID; used for DB queries and artwork |
| `grandparentTitle` | episode | Show title |
| `grandparentThumb` | episode | Show poster path |
| `grandparentArt` | episode | Show background/banner path |
| `grandparentYear` | episode | Show year (for show stub metadata) |
| `parentRatingKey` | episode | Season's Plex ID |
| `parentTitle` | episode | Season title |
| `parentThumb` | episode | Season poster path |
| `seasonNumber` | episode | Season number (parentIndex) |
| `episodeNumber` | episode | Episode number within season |

---

## 6. Configuration Slots

The Plex connection and sync root live in `configs/plex.json`:

```json
{
  "host": "http://localhost:32400",
  "token": "YOUR_PLEX_TOKEN",
  "managed_user": "",
  "sync_root": "/media/drive/PlexSyncer",
  "subtitle_languages": ["en"],
  "subtitle_forced_only": false,
  "hidden_libraries": []
}
```

Each slot is stored as `configs/{slot_name}.json`:

```json
{
  "slot_name": "MyPhone",
  "selections": {
    "playlists": ["Kids Car", "Kids Bedtime"],
    "movies": ["Moana", "Encanto"],
    "shows": {
      "ALF": { "mode": "next_unwatched", "count": 3 },
      "The Bear": { "mode": "next_unwatched", "count": 2 }
    }
  }
}
```

The slot's sync directory is `{sync_root}/{slot_name}/`.

### Show Sync Modes

| Config | Behavior |
|---|---|
| `{"mode": "all"}` | All episodes |
| `{"mode": "latest", "count": N}` | N most-recently aired episodes |
| `{"mode": "next_unwatched", "count": N}` | Next N unwatched episodes |

Version selection: **lowest bitrate** (smallest file) is always chosen automatically.

---

## 7. Python Worker

**File:** `plex_hardlink_sync.py`

### Status

| Feature | Status |
|---|---|
| Plex connection + server ID | ✅ Done |
| Multi-playlist sync | ✅ Done |
| Correct filename sanitization (mirrors Plezy) | ✅ Done |
| Lowest-bitrate version picker | ✅ Done |
| Slot config file support | ✅ Done |
| `--all-slots` CRON mode | ✅ Done |
| Movie support | ✅ Done |
| TV show "Next X unwatched" support | ✅ Done |
| Subtitle sidecar hard-linking | ✅ Done |
| Manifest generation (all artwork fields) | ✅ Done |
| Pruning (files not in active config) | ✅ Done |
| Path collision detection | ✅ Done |

### Invocation

Single slot (manual):
```bash
python3 plex_hardlink_sync.py --slot MyPhone
```

All slots (CRON):
```bash
python3 plex_hardlink_sync.py --all-slots
```

Legacy playlist mode (still supported for testing):
```bash
python3 plex_hardlink_sync.py \
  --host "http://192.168.1.100:32400" \
  --token "YOUR_TOKEN" \
  --sync-dir "/media/drive/PlexSyncer/MyPhone" \
  -p "Kids Car" -p "Kids Bedtime"
```

### Version Picker Logic

```python
best_media = min(
    item.media,
    key=lambda m: (m.bitrate or 999999, m.parts[0].size or float('inf'))
)
```

Bitrate is the primary sort key; file size is the tiebreaker.

---

## 8. Management UI

**File:** `sync_ui.py`
**Framework:** Streamlit ≥ 1.34.0
**Access:** `http://{server-ip}:8501`

### Quick Start (manual)

```bash
cd /path/to/PlexSyncer
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
streamlit run sync_ui.py
```

### Install as System Service

```bash
bash install_service.sh
```

This creates the venv, installs dependencies, and registers a systemd service
(`plexsyncer.service`) that starts automatically on boot.

```bash
bash uninstall_service.sh   # remove service (configs and files untouched)
```

Manage with:
```bash
sudo systemctl status plexsyncer
sudo systemctl restart plexsyncer
sudo journalctl -u plexsyncer -f
```

### Layout

The UI uses a two-column cart + search layout:

- **Left — Sync Queue:** Everything queued for the active slot. Shows item count, current
  on-disk size of the slot directory, inline sync mode for each show, per-item ✕ remove
  buttons, and a Clear All button.
- **Right — Browser:** One tab per Plex library plus a Playlists tab. All items load at
  once with a live filter box — no pagination. Check/uncheck items to add or remove them
  from the queue.

Slot tabs run across the top of the page. The UI connects to Plex automatically on load
using the stored token — no manual Connect step required.

### Features

| Feature | Status |
|---|---|
| Slot tabs across top (segmented control) | ✅ Done |
| Auto-connect to Plex on page load | ✅ Done |
| Plex connection + managed user | ✅ Done |
| Library browser — one tab per library | ✅ Done |
| Hide libraries from browser (UI-only, worker unaffected) | ✅ Done |
| TV show sync mode selector (next N, latest N, all) | ✅ Done |
| Unwatched episode count badge | ✅ Done |
| All items loaded at once — no pagination | ✅ Done |
| Cart panel (left column) with live queue | ✅ Done |
| Per-item ✕ remove + Clear All | ✅ Done |
| Item count + on-disk size display | ✅ Done |
| Unsaved changes indicator | ✅ Done |
| Save config + Save & Sync | ✅ Done |
| Live sync output log | ✅ Done |
| Settings dialog (connection, sync, libraries, slots) | ✅ Done |

### Requirements

Streamlit ≥ 1.34.0 is required for `@st.dialog`.
See `requirements.txt`.

---

## 9. Plezy Fork

**Repository:** [`crakerjac/plezy`](https://github.com/crakerjac/plezy) — fork of [`edde746/plezy`](https://github.com/edde746/plezy)
**Current release:** `1.33.1+PlexSyncer`
**Target platform:** Android
**Status:** Complete. Build from source or download the latest release from the fork repository.

All PlexSyncer-specific changes are marked with `// PlexSyncer` comments for easy
identification during upstream rebases.

### Changed Files

| File | Type | Change |
|---|---|---|
| `lib/services/manifest_import_service.dart` | **New** | SAF reader, JSON parser, URI resolver |
| `lib/services/download_manager_service.dart` | Modified | `registerSyncedDownload()`, `registerSyncedParentStub()` |
| `lib/providers/download_provider.dart` | Modified | `importFromManifest()`, `ImportSummary`, prune loop |
| `lib/screens/downloads/downloads_screen.dart` | Modified | Scan button in Downloads app bar |
| `pubspec.yaml` | Modified | `+PlexSyncer` version suffix |

### How It Works

**Scan button** appears in the Downloads screen app bar whenever a SAF download folder
is configured in Plezy settings. Tapping it:

1. Reads `{SAF root}/PlexSyncer/_plezy_meta/manifest.json`
2. For each item in the manifest, resolves the `relativePath` to a SAF `content://` URI
   by walking the SAF tree from the `PlexSyncer` folder root
3. Skips items already registered in the database (idempotent)
4. Registers new items as completed downloads with full metadata
5. Fetches artwork from the Plex server immediately (if online), including show poster
   and background banner — deduplicates show artwork to one fetch per show
6. Prunes DB records for files that were in the PlexSyncer folder on a previous scan
   but are no longer in the manifest (removed by rclone / watched and rotated out)

**Pruning without a source column:** PlexSyncer items are identified by their SAF URI —
all files registered by the scan have a `content://` URI whose document ID starts with
the `PlexSyncer` folder's document ID. This uniquely identifies them without any DB
schema changes, preserving full compatibility with upstream Plezy.

### Setup

1. In Plezy → Settings → Downloads, set a custom download folder pointing to your
   desired SAF root (e.g. `/Plezy/`)
2. Configure rclone to sync the PlexSyncer slot directory to
   `{SAF root}/PlexSyncer/` on the phone
3. After rclone completes, tap the scan button (↻) in the Downloads tab

### Upstream Compatibility

No database schema changes. No `build_runner` required. The changes are additive and
isolated — rebasing onto a new upstream Plezy version requires merging only the
four modified files listed above.

---

## 10. Subtitle Handling

The worker detects subtitle sidecar files on the server and hard-links them alongside
the video using the naming convention: `{VideoBase}.{lang}.{ext}`.

### Detection Logic

```python
for stream in part.subtitleStreams():
    if stream.key:  # key is set only for sidecar files, not embedded streams
        lang = stream.languageCode or 'und'
        ext = os.path.splitext(stream.key)[1].lstrip('.')
        subtitle_source = stream.key  # server-relative path
```

### Open Question — Plezy Subtitle Registration

MPV auto-detects sidecar subtitle files by filename convention, so playback should work.
However it is not yet confirmed whether Plezy's subtitle picker UI lists sidecar files
for offline downloads, or whether they need to be registered in the database.

**Test required:** Manually place a `.en.srt` sidecar next to a downloaded episode on
the phone and check the subtitle picker in Plezy before investing further.

---

## 11. Watch State & Pruning Lifecycle

```
1.  CRON syncs N unwatched episodes to slot dir
2.  rclone transfers to phone (PlexSyncer subfolder)
3.  User taps Scan in Plezy → files registered as completed downloads
4.  User watches episode offline in Plezy
5.  Plezy records progress locally (OfflineWatchProgress table)
6.  Phone comes online
7.  User opens Plezy → watch state syncs back to Plex server
8.  Next CRON run:
    a. Plex now shows episode as watched
    b. Worker drops it from "Next X" queue
    c. Pruning deletes the server-side hard link
    d. Next unwatched episode is linked
    e. Manifest is regenerated
9.  rclone transfers updated folder to phone (old file gone, new file added)
10. User taps Scan → new items registered, watched item pruned from DB
```

### Timing Safety

The CRON only prunes based on Plex watch state. If the user watched something offline
but hasn't synced back to Plex yet, Plex still shows it as unwatched → it stays in the
queue → it is NOT pruned prematurely. Content is never removed before Plex knows it
was watched.

### Scan Prune Safety

The Plezy scan only removes DB records for items whose `videoFilePath` is a SAF URI
under the `PlexSyncer` folder. Native Plezy downloads (internal storage or other SAF
locations) are never touched, regardless of what's in the manifest.

---

## 12. Deployment

### Requirements

- Python 3.10+
- `pip install -r requirements.txt` (or run `bash install_service.sh`)
- Streamlit ≥ 1.34.0
- `sync_root` must be on the **same filesystem partition** as the Plex media library
  (required for hard links — cross-device links fail with `errno 18`)

### CRON

```cron
0 3 * * * /path/to/PlexSyncer/venv/bin/python /path/to/PlexSyncer/plex_hardlink_sync.py --all-slots >> /var/log/plexsyncer.log 2>&1
```

### Sync Tool

PlexSyncer generates slot directories on the server. Getting them to the phone is
handled separately — see §13 for options.

Server path per slot: `{sync_root}/{slot_name}/`
Phone destination: `{Plezy SAF root}/PlexSyncer/` (configure in your sync tool)

---

## 13. Mobile Sync Options

PlexSyncer is not opinionated about how files get to your phone.

### rclone + Round Sync (Recommended)

[rclone](https://rclone.org/) on the server with [Round Sync](https://github.com/roundsync/roundsync)
on Android. Significantly faster than Syncthing for large media files.

```bash
# Example: sync slot to phone's PlexSyncer folder
rclone sync /media/drive/PlexSyncer/MyPhone remote:/Plezy/PlexSyncer --progress
```

Round Sync on Android can pull from an rclone remote on a schedule.

### Syncthing

[Syncthing](https://syncthing.net/) works but is noticeably slower for large files.
Configure the phone folder as **Receive Only**. The worker protects `.stfolder` and
other Syncthing internals from pruning.

### SMB / NFS / USB

Mount the slot directory as a network share and copy manually or with a scheduled task.

---

## 14. Platform Support

| Platform | Worker | UI | Notes |
|---|---|---|---|
| **Linux** | ✅ | ✅ | Primary target, fully tested |
| **macOS** | ✅ | ✅ | `os.link()` native; not tested but expected to work |
| **Windows** | ⚠️ | ✅ | Hard links require NTFS + admin or Developer Mode; not tested |

Hard links require `sync_root` and the Plex media library to be on the same filesystem
partition on all platforms.

---

## 15. Open Questions / Future Work

| # | Item | Priority |
|---|---|---|
| 1 | Subtitle sidecar visibility in Plezy's offline subtitle picker | High |
| 2 | Automatic Scan after Round Sync completes (broadcast intent?) | Low — manual is safe for now |
| 3 | macOS testing | Low |
| 4 | Windows hard link support (privilege check + fallback) | Low |
| 5 | Upstream PR to Plezy for scan / manifest import | Not planned — upstream has shipped its own sync rules feature (`edde746/plezy` commit 3607416), which takes a different device-pull approach. The PlexSyncer fork remains a separate, maintained build. |
