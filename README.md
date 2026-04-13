# PlexSyncer for Plezy

> Automated offline media sync from Plex to mobile devices via Syncthing\Rclone and Plezy.

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
13. [Open Questions / Future Work](#13-open-questions--future-work)

---

## 1. Vision

PlexSyncer automates the selection, hard-linking, and cleanup of Plex media content into
slot-specific directories that Syncthing mirrors to mobile devices. A forked build of
the Plezy Android app reads a `manifest.json` sidecar to register synced files as
offline-available without requiring a Plex connection on the phone.

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
│    tablet/                ◄────────────────────────┘    │
│      _plezy_meta/                                       │
│        manifest.json                                    │
│      TV Shows/...                                       │
│      Movies/...                                         │
└─────────────────┬───────────────────────────────────────┘
                  │  Syncthing (one-way push, phone is receive-only)
                  ▼
┌─────────────────────────────────────────────────────────┐
│  Android Phone                                          │
│                                                         │
│  /storage/.../PlexSyncer/tablet/                          │
│    _plezy_meta/manifest.json                            │
│    TV Shows/...                                         │
│    Movies/...                                           │
│         │                                               │
│         ▼  (user taps "Scan" in Plezy)                  │
│  Plezy (forked)                                         │
│    - Reads manifest.json                                │
│    - Registers files as completed downloads             │
│    - Plays files offline via MPV                        │
│    - Syncs watch state back to Plex when online         │
└─────────────────────────────────────────────────────────┘
```

Syncthing is configured as **one-way push only** (phone is receive-only). There is no
risk of phone-side changes propagating back to the server.

---

## 3. System Architecture

### Components

| Component | Language | Purpose |
|---|---|---|
| `plex_hardlink_sync.py` | Python | Core sync worker: hard links, manifest, pruning |
| `sync_ui.py` | Python / Streamlit | Web UI for slot configuration |
| Plezy fork | Dart / Flutter | Android app: manifest import, scan button, delete-from-storage |
| Syncthing | — | File transport, phone is receive-only |

### Slot Model

A **slot** represents one target device/profile. Each slot has its own:
- Configuration file (`configs/{slot_name}.json`)
- Sync directory (`sync_root/{slot_name}/`)
- Syncthing folder target

This allows independent configurations for e.g. a tablet (kids content) and a phone
(adult content) from the same Plex server.

---

## 4. File and Path Conventions

Paths **must exactly match** what Plezy's own downloader produces, because the forked
app uses the same path-resolution logic to play files.

### Sanitization Rules

Derived from `download_storage_service.dart → _sanitizeFileName()`:

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

Subtitle files sit alongside the video, named:
```
{Video filename base}.{language_code}.{ext}
```

Example:
```
TV Shows/ALF (1986)/Season 01/S01E01 - A_L_F.en.srt
```

> **Open question:** Does Plezy's offline mode surface subtitle tracks from sidecar
> files in its subtitle picker UI? This needs testing before subtitle support is
> considered complete. MPV (the underlying player) auto-detects sidecars, but Plezy
> may need to register them in its database. See §10.

---

## 5. Manifest Format

The manifest lives at `{slot_dir}/_plezy_meta/manifest.json` and is always fully
regenerated on each sync run. Plezy reads this file on a manual "Scan" to register
all synced files as completed download records.

```json
{
  "version": 1,
  "generatedAt": "1997-08-29T02:14:00Z"
  "serverId": "0123456789012345678901234567890123456789",
  "serverName": "MediaBox",
  "items": [
    {
      "ratingKey": "12345",
      "type": "episode",
      "title": "A.L.F.",
      "thumb": "/library/metadata/12345/thumb",
      "summary": "ALF accidentally reveals himself to a neighbor...",
      "duration": 1560000,
      "addedAt": "1997-08-29T02:14:00Z",
      "relativePath": "TV Shows/ALF (1986)/Season 01/S01E01 - A_L_F.mp4",
      "grandparentTitle": "ALF",
      "grandparentYear": 1986,
      "parentTitle": "Season 1",
      "seasonNumber": 1,
      "episodeNumber": 1
    },
    {
      "ratingKey": "67890",
      "type": "movie",
      "title": "Dune",
      "year": 2021,
      "thumb": "/library/metadata/67890/thumb",
      "summary": "A noble family becomes embroiled in a war...",
      "duration": 9360000,
      "addedAt": "1997-08-29T02:14:00Z",
      "relativePath": "Movies/Dune (2021)/Dune (2021).mp4"
    }
  ]
}
```

### Field Notes

| Field | Source | Notes |
|---|---|---|
| `serverId` | `plex.machineIdentifier` | Must match exactly what Plezy stored at login |
| `thumb` | `item.thumb` | Server-relative path; Plezy fetches lazily when online |
| `relativePath` | Generated by script | Relative to slot dir; always forward slashes |
| `duration` | `item.duration` | Milliseconds, as Plex stores it |
| `type` | `item.TYPE` | `"episode"` or `"movie"` |

---

## 6. Configuration Slots

Each slot is stored as `configs/{slot_name}.json`:

```json
{
  "slot_name": "tablet",
  "plex": {
    "host": "http://192.168.1.100:32400",
    "token": "YOUR_PLEX_TOKEN"
  },
  "sync_root": "/media/PlexDrive/PlexSyncer",
  "selections": {
    "playlists": ["Kids Car", "Kids Bedtime"],
    "movies": ["Moana", "Encanto"],
    "shows": {
      "ALF": { "next": 3 },
      "The Bear": { "next": 2 }
    }
  }
}
```

The slot's sync directory is `{sync_root}/{slot_name}/`.

### Selection Types

| Type | Behavior |
|---|---|
| `playlists` | Sync all items in the named Plex playlist |
| `movies` | Sync a specific movie by title |
| `shows` | Sync the next N **unwatched** episodes of a show |

Version selection across all types: **lowest bitrate** (smallest file) is always chosen
automatically. This is optimal for mobile storage and transfer speed.

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
| `--all-slots` CRON mode | ⬜ TODO |
| Movie support | ✅ Done |
| TV show "Next X unwatched" support | ✅ Done |
| Subtitle sidecar hard-linking | ⬜ TODO |
| Manifest generation | ✅ Done |
| Pruning (files not in active config) | ✅ Done |
| Path collision detection | ✅ Done |

### Invocation

Single slot (manual):
```bash
python3 plex_hardlink_sync.py --slot tablet
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
  --sync-dir "/media/drive/PlexSyncer/tablet" \
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
**Framework:** Streamlit  
**Access:** `http://{server-ip}:8501`

### Status

| Feature | Status |
|---|---|
| Slot selector / creator | ⬜ TODO |
| Plex connection config | ⬜ TODO |
| Playlist browser | ⬜ TODO |
| Movie search & selection | ⬜ TODO |
| TV show "Next X" selector | ⬜ TODO |
| Save config | ⬜ TODO |
| "Save & Sync" trigger button | ⬜ TODO |

> **Sequencing note:** The UI is built last. The worker must be fully validated
> end-to-end first. The UI is purely a config editor and sync trigger — it adds
> no new logic.

---

## 9. Plezy Fork

**Repository:** Fork of [edde746/plezy](https://github.com/edde746/plezy)  
**Target platform:** Android only (initial)  
**Upstream PR potential:** Delete-from-storage (Change 2) may be suitable upstream.

### Change 1 — Manifest Import / Scan Button

A "Scan" button in the Downloads screen triggers a manifest import:

1. Read `_plezy_meta/manifest.json` from the configured SAF folder root
2. For each item, check if `serverId:ratingKey` already exists in the
   `DownloadedMedia` table — skip if so
3. Resolve `relativePath` to a full SAF `content://` URI using the SAF tree root
4. Insert a new `DownloadedMediaItem` row with `status = completed`
5. Items in the database that are no longer in the manifest and were imported via
   scan (flagged `source = syncthing`) are removed from the database

**Database fields populated (from `tables.dart → DownloadedMedia`):**

| Field | Value |
|---|---|
| `serverId` | from manifest |
| `ratingKey` | from manifest |
| `globalKey` | `"{serverId}:{ratingKey}"` |
| `type` | from manifest |
| `status` | `3` (completed) |
| `progress` | `100` |
| `videoFilePath` | resolved SAF `content://` URI |
| `thumbPath` | `null` initially (fetched lazily when online) |
| `downloadedAt` | current timestamp |

### Change 2 — Delete from Storage on Download Delete

When the user deletes a download in Plezy, the underlying file is also deleted from
phone storage via SAF. Currently Plezy only removes the database record.

This change is self-contained and does not depend on the manifest/scan feature.
It is a candidate for upstream PR.

### What Is NOT Changed

- Download logic (Syncthing is the transport, not Plezy)
- Watch state sync (unchanged — works as-is)
- Playback (unchanged — MPV plays SAF `content://` URIs)
- Thumbnail fetching (unchanged — lazy fetch when online)

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

MPV auto-detects sidecar subtitle files by filename convention, so **playback should
work**. However it is not yet confirmed whether:

- Plezy's subtitle picker UI lists sidecar files for offline downloads
- Plezy requires subtitle files to be registered in its database to appear in the UI

**Test required:** Manually place a `.en.srt` sidecar next to a downloaded episode on
the phone and verify the subtitle picker shows it in Plezy before building the
linking logic. If registration is needed, the manifest format and Dart import code
will need subtitle entries.

---

## 11. Watch State & Pruning Lifecycle

### Happy Path (Episode Watched Offline)

```
1. CRON syncs N unwatched episodes to slot dir
2. Syncthing pushes to phone
3. User watches episode offline in Plezy
4. Plezy records progress in local OfflineWatchProgress table
5. Phone comes online
6. Syncthing syncs any manifest/file changes (phone is receive-only — no risk)
7. User opens Plezy → watch state syncs back to Plex server
8. Next CRON run:
   a. Plex now shows episode as watched
   b. Worker drops it from "Next X" queue
   c. Pruning deletes the hard link
   d. Next unwatched episode is added and linked
   e. Manifest is regenerated
9. Syncthing pushes updated folder to phone (file deleted, new file added)
10. User taps "Scan" in Plezy → database updated to match new manifest
```

### Timing Gap Behavior

There is an intentional gap between step 3 (watched offline) and step 7 (synced to
Plex). During this gap, if CRON runs:

- The episode is still marked unwatched in Plex
- The worker keeps it in the queue
- No files are pruned prematurely

The gap only closes when the user opens Plezy while online. This is acceptable
behavior — content is never pruned before Plex knows it was watched.

### What Triggers a Scan in Plezy

The "Scan" button is manual. The user taps it after Syncthing has finished syncing
a new manifest. This is intentional — automatic background scanning would risk
partial-sync states where the manifest is updated but files have not yet arrived.

---

## 12. Deployment

### Requirements

- Python 3.10+
- `pip install plexapi requests streamlit`
- `sync_root` must be on the **same filesystem partition** as the Plex media library
  (required for hard links — cross-device links will fail with `errno 18`)
- Syncthing configured with phone folder as **receive-only**

### CRON

```cron
0 3 * * * /usr/bin/python3 /home/user/GitProjects/PlexSyncer/plex_hardlink_sync.py --all-slots >> /var/log/plexsync.log 2>&1
```

### Syncthing

One Syncthing folder per slot, e.g.:

| Folder ID | Server path | Phone path |
|---|---|---|
| `plexsync-tablet` | `/media/drive/PlexSyncer/tablet` | `/storage/.../PlexSyncer/tablet` |
| `plexsync-phone` | `/media/drive/PlexSyncer/phone` | `/storage/.../PlexSyncer/phone` |

Phone folders set to **Receive Only** — no changes propagate back to server.

---

## 13. Open Questions / Future Work

| # | Question | Priority |
|---|---|---|
| 1 | Do subtitle sidecars appear in Plezy's subtitle picker without DB registration? | High — needed before subtitle work starts |
| 2 | Does Plezy require `ApiCache` entries (metadata JSON) in addition to `DownloadedMedia` rows for full offline display? | High — needed before Dart work starts |
| 3 | Thumbnail strategy: lazy fetch when online is confirmed acceptable. No action needed. | Closed |
| 4 | Should "Scan" auto-run when Plezy detects a manifest change, or always manual? | Low — manual is safe default |
| 5 | Managed account (`-u`) support in worker — currently broken due to PlexAPI `NotFound` on display name vs username | Low — not needed for main account workflow |
| 6 | Streamlit UI — build after worker is fully validated | Deferred |
| 7 | Upstream PR for Change 2 (delete-from-storage) | Deferred until fork is working |
