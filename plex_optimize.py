"""
plex_optimize.py

Pre-transcodes Plex content with incompatible codecs to H.264/AAC MP4 files
stored in a shared _optimized/ cache directory.  plex_hardlink_sync.py
automatically uses these optimized versions when linking files to sync slots.

── Usage ─────────────────────────────────────────────────────────────────────
    python3 plex_optimize.py --all-slots           # transcode all slots
    python3 plex_optimize.py --slot OnePlus13r     # single slot only
    python3 plex_optimize.py --all-slots --dry-run # report only, no transcode

── How it works ──────────────────────────────────────────────────────────────
    1. Reads your slot configs to determine which Plex items are in scope.
    2. Checks each item's video codec against a blocklist.
    3. For incompatible items, transcodes to H.264/AAC MP4 using:
         - VAAPI hardware encoding (Intel iGPU) — fast, low CPU
         - libx264 software fallback if VAAPI is unavailable
    4. Output files are cached in {sync_root}/_optimized/{ratingKey}.mp4.
       Already-cached files are skipped on subsequent runs.
    5. plex_hardlink_sync.py detects these files and uses them automatically.

── plex.json settings (all optional) ────────────────────────────────────────
    "transcode_incompatible": true          # master switch (default: false)
    "transcode_device":  "/dev/dri/renderD128"  # VAAPI device node
    "transcode_quality": 23                 # global_quality / CRF (0-51, lower=better)
    "transcode_audio_bitrate": "256k"       # AAC audio bitrate

── Requirements ──────────────────────────────────────────────────────────────
    ffmpeg (with h264_vaapi support)  — already present on MediaBox
    plexapi, requests                 — same as plex_hardlink_sync.py
"""

from __future__ import annotations

import os, re, json, glob, argparse, subprocess, shutil, sys
from datetime import datetime, timezone
from typing import Optional

import plexapi, plexapi.exceptions, requests
from plexapi.server import PlexServer
from plexapi.video import Show

# ── Constants ─────────────────────────────────────────────────────────────────

CONFIGS_DIR  = os.path.join(os.path.dirname(__file__), 'configs')
PLEX_CONFIG  = os.path.join(CONFIGS_DIR, 'plex.json')
OPTIMIZED_DIR_NAME = '_optimized'
LOCK_FILE          = '/tmp/plexsyncer.lock'
SYNC_WORKER        = os.path.join(os.path.dirname(__file__), 'plex_hardlink_sync.py')
LOG_FILE           = '/tmp/plexsyncer_optimize.log'

# Video codecs that Android cannot direct-play reliably.
# H.264 (h264/avc), HEVC (hevc/h265), VP8, VP9, AV1 are generally fine.
# Everything else is suspect.
INCOMPATIBLE_VIDEO_CODECS = {
    'mpeg4',       # MPEG-4 Part 2 — your exact case
    'msmpeg4v3',   # Microsoft MPEG-4 v3 (old DivX/XviD)
    'msmpeg4v2',
    'msmpeg4v1',
    'mpeg2video',  # MPEG-2 (DVD)
    'mpeg1video',
    'wmv3',        # Windows Media Video 9
    'wmv2',
    'wmv1',
    'vc1',         # Windows Media Video VC-1
    'theora',      # Ogg Theora
    'rv40',        # RealVideo 4
    'rv30',
    'flv1',        # Sorenson Spark (old Flash)
    'vp6f',        # Flash VP6
    'indeo5',      # Intel Indeo
    'cinepak',
    'mjpeg',       # Motion JPEG (large files, poor seeking)
    'prores',      # Apple ProRes
    'dnxhd',       # Avid DNxHD
    'huffyuv',
}


# ══════════════════════════════════════════════════════════════════════════════
# LOCK
# ══════════════════════════════════════════════════════════════════════════════

def acquire_lock() -> Optional[int]:
    """Try to acquire the PlexSyncer flock. Returns fd on success, None if busy."""
    try:
        import fcntl
        fd = open(LOCK_FILE, 'w')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (IOError, OSError):
        return None


def is_locked() -> bool:
    """Return True if another process holds the PlexSyncer lock."""
    try:
        import fcntl
        fd = open(LOCK_FILE, 'w')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
        return False
    except (IOError, OSError):
        return True


# ══════════════════════════════════════════════════════════════════════════════
# STATUS FILE
# ══════════════════════════════════════════════════════════════════════════════

_status_path: Optional[str] = None  # set in main() once we know sync_root


def _status_file(sync_root: str) -> str:
    return os.path.join(sync_root, OPTIMIZED_DIR_NAME, '.status.json')


def write_status(sync_root: str, **kwargs) -> None:
    """Write/update the status file. All kwargs are merged into the JSON."""
    path = _status_file(sync_root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = {}
        if os.path.exists(path):
            with open(path) as f:
                existing = json.load(f)
        existing.update(kwargs)
        with open(path, 'w') as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass  # status file is best-effort


def clear_status(sync_root: str) -> None:
    try:
        path = _status_file(sync_root)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def read_status(sync_root: str) -> Optional[dict]:
    path = _status_file(sync_root)
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return None


class Tee:
    """Write to both stdout and a log file simultaneously."""
    def __init__(self, log_path: str):
        self._log = open(log_path, 'w', encoding='utf-8', buffering=1)
        self._stdout = sys.stdout

    def write(self, data):
        self._stdout.write(data)
        self._log.write(data)

    def flush(self):
        self._stdout.flush()
        self._log.flush()

    def close(self):
        self._log.close()


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def _load_global_plex_cfg() -> dict:
    if os.path.exists(PLEX_CONFIG):
        with open(PLEX_CONFIG, encoding='utf-8') as f:
            return json.load(f)
    return {}


def load_plex_credentials(slot_config: dict) -> tuple[str, str]:
    if 'plex' in slot_config:
        return slot_config['plex']['host'], slot_config['plex']['token']
    if os.path.exists(PLEX_CONFIG):
        with open(PLEX_CONFIG, encoding='utf-8') as f:
            cfg = json.load(f)
        return cfg['host'], cfg['token']
    raise RuntimeError('No Plex credentials found.')


def connect(host: str, token: str) -> PlexServer:
    print('Connecting to Plex...', end='', flush=True)
    try:
        plex = PlexServer(host, token)
    except plexapi.exceptions.Unauthorized:
        print(' [FAILED -- bad token]')
        raise
    except requests.exceptions.ConnectionError:
        print(' [FAILED -- could not reach server]')
        raise
    print(f' [DONE -- {plex.friendlyName}]')
    return plex


def load_slot_config(slot_name: str) -> Optional[dict]:
    path = os.path.join(CONFIGS_DIR, f'{slot_name}.json')
    if not os.path.exists(path):
        print(f'[ERROR] Config not found: {path}')
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def get_all_slot_names() -> list:
    paths = glob.glob(os.path.join(CONFIGS_DIR, '*.json'))
    names = [os.path.splitext(os.path.basename(p))[0] for p in sorted(paths)]
    return [n for n in names if n not in ('example', 'plex')]


# ══════════════════════════════════════════════════════════════════════════════
# PLEX ITEM COLLECTION
# ══════════════════════════════════════════════════════════════════════════════

def pick_best_media(item):
    """Return the lowest-bitrate media version object (not just path)."""
    try:
        return min(
            item.media,
            key=lambda m: (
                m.bitrate or 999_999,
                m.parts[0].size if m.parts and m.parts[0].size else float('inf'),
            ),
        )
    except (IndexError, AttributeError, ValueError):
        return None


def _find_show(plex: PlexServer, title: str) -> Optional[Show]:
    results = plex.library.search(title=title, libtype='show')
    exact   = [r for r in results if r.title.lower() == title.lower()]
    cands   = exact or results
    if not cands:
        print(f'    [WARNING] Show not found: "{title}"')
        return None
    return cands[0]


def collect_items_for_slot(plex: PlexServer, config: dict) -> dict:
    """
    Collect all items for a slot config.
    Returns {ratingKey: (item, source_path, video_codec)} for items
    with an incompatible video codec.
    """
    selections = config.get('selections', {})
    all_items  = {}

    def merge(d: dict) -> None:
        for k, v in d.items():
            if k not in all_items:
                all_items[k] = v

    # Playlists
    for name in selections.get('playlists', []):
        try:
            pl = plex.playlist(name)
            for item in pl.items():
                media = pick_best_media(item)
                if media and media.parts:
                    all_items[str(item.ratingKey)] = (
                        item, media.parts[0].file,
                        getattr(media, 'videoCodec', None)
                    )
        except Exception:
            pass

    # Movies
    for title in selections.get('movies', []):
        try:
            results = plex.library.search(title=title, libtype='movie')
            exact   = [r for r in results if r.title.lower() == title.lower()]
            item    = (exact or results)[0] if (exact or results) else None
            if item:
                media = pick_best_media(item)
                if media and media.parts:
                    all_items[str(item.ratingKey)] = (
                        item, media.parts[0].file,
                        getattr(media, 'videoCodec', None)
                    )
        except Exception:
            pass

    # Shows
    for show_title, mode_cfg in selections.get('shows', {}).items():
        if isinstance(mode_cfg, int):
            mode_cfg = {'mode': 'next_unwatched', 'count': mode_cfg}
        if 'next' in mode_cfg and 'mode' not in mode_cfg:
            mode_cfg = {'mode': 'next_unwatched', 'count': mode_cfg['next']}

        mode  = mode_cfg.get('mode', 'next_unwatched')
        count = mode_cfg.get('count', 1)

        show = _find_show(plex, show_title)
        if show is None:
            continue

        if mode == 'all':
            episodes = show.episodes()
        elif mode == 'latest':
            all_eps  = show.episodes()
            episodes = all_eps[-count:] if len(all_eps) >= count else all_eps
        else:
            episodes = show.unwatched()[:count]

        for ep in episodes:
            media = pick_best_media(ep)
            if media and media.parts:
                all_items[str(ep.ratingKey)] = (
                    ep, media.parts[0].file,
                    getattr(media, 'videoCodec', None)
                )

    return all_items


# ══════════════════════════════════════════════════════════════════════════════
# FFMPEG TRANSCODE
# ══════════════════════════════════════════════════════════════════════════════

def _vaapi_available(device: str) -> bool:
    """Quick check: does the VAAPI device node exist and is ffmpeg built with h264_vaapi?"""
    if not os.path.exists(device):
        return False
    result = subprocess.run(
        ['ffmpeg', '-hide_banner', '-encoders'],
        capture_output=True, text=True
    )
    return 'h264_vaapi' in result.stdout


def _opt_filename(item, rk: str) -> str:
    """Build a human-readable filename: 'Show S01E02_293000.mp4'"""
    show   = getattr(item, 'grandparentTitle', None)
    season = getattr(item, 'parentIndex', None)
    ep     = getattr(item, 'index', None)
    title  = getattr(item, 'title', '')
    import re
    def clean(s): return re.sub(r'[<>:"/\\|?*]', '', s).strip()
    if show and season is not None and ep is not None:
        name = f'{clean(show)} S{season:02d}E{ep:02d}'
    elif show:
        name = f'{clean(show)} {clean(title)}'
    else:
        name = clean(title) or rk
    return f'{name}_{rk}.mp4'


def _rk_from_opt_filename(filename: str) -> Optional[str]:
    """Extract ratingKey from filename like 'Show S01E02_293000.mp4'"""
    stem = os.path.splitext(filename)[0]
    parts = stem.rsplit('_', 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[1]
    return None


def _run_ffmpeg(cmd: list, dst: str) -> bool:
    """Run an ffmpeg command, clean up on failure. Returns True on success."""
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        if result.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            size_mb = os.path.getsize(dst) / 1024 / 1024
            print(f'    Done — {size_mb:.1f} MB')
            return True
        # Non-zero exit — always remove output (even partial files) so
        # the next tier starts clean and doesn't find a corrupt file.
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        for line in lines[-5:]:
            print(f'    {line}')
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except Exception:
            pass
        return False
    except FileNotFoundError:
        print('    [ERROR] ffmpeg not found. Install with: sudo apt install ffmpeg')
        return False
    except Exception as e:
        print(f'    [ERROR] {e}')
        if os.path.exists(dst):
            os.remove(dst)
        return False


def transcode_file(
    src: str,
    dst: str,
    vaapi_device: str  = '/dev/dri/renderD128',
    quality: int       = 23,
    audio_bitrate: str = '256k',
) -> bool:
    """
    Transcode src to dst as H.264/AAC MP4.
    Tier 1: VAAPI encode-only (software decode, hardware encode via Intel iGPU).
    Tier 2: libx264 software fallback.
    Old codecs (mpeg4, mpeg2, etc.) always fail hwaccel_output_format vaapi,
    so we skip the full VAAPI pipeline and go straight to encode-only.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    cpu_cores = os.cpu_count() or 4

    common_audio = [
        '-c:a', 'aac', '-b:a', audio_bitrate,
        '-ar', '48000', '-ac', '2',
        '-metadata:s:a:0', 'language=eng',
    ]

    if _vaapi_available(vaapi_device):
        print('    Encoding via VAAPI (encode-only)...', flush=True)
        cmd1 = [
            'ffmpeg', '-y',
            '-i', src,
            '-movflags', '+faststart',
            '-vf', 'format=nv12,hwupload',
            '-vaapi_device', vaapi_device,
            '-c:v', 'h264_vaapi',
            '-global_quality', str(quality),
        ] + common_audio + ['-f', 'mp4', dst]
        if _run_ffmpeg(cmd1, dst):
            return True

    print('    VAAPI failed — falling back to software encoding (libx264)...', flush=True)
    cmd2 = [
        'ffmpeg', '-y',
        '-i', src,
        '-movflags', '+faststart',
        '-c:v', 'libx264',
        '-crf', str(quality),
        '-preset', 'fast',
        '-threads', str(cpu_cores),
    ] + common_audio + ['-f', 'mp4', dst]
    return _run_ffmpeg(cmd2, dst)


# ══════════════════════════════════════════════════════════════════════════════
# CORE OPTIMIZE
# ══════════════════════════════════════════════════════════════════════════════

def optimize_slot(slot_name: str, plex: PlexServer, plex_cfg: dict,
                  dry_run: bool = False,
                  sync_root: str = '') -> dict:
    """
    Scan a slot's items and transcode any with incompatible codecs.
    Returns summary dict with counts.
    """
    sync_root    = plex_cfg.get('sync_root', '')
    optimized_dir = os.path.join(sync_root, OPTIMIZED_DIR_NAME)
    vaapi_device  = plex_cfg.get('transcode_device', '/dev/dri/renderD128')
    quality       = int(plex_cfg.get('transcode_quality', 23))
    audio_bitrate = plex_cfg.get('transcode_audio_bitrate', '256k')

    config = load_slot_config(slot_name)
    if config is None:
        return {'error': f'Config not found for slot {slot_name}'}

    print(f'\nCollecting items for slot "{slot_name}"...')
    all_items = collect_items_for_slot(plex, config)
    print(f'  {len(all_items)} total items')

    compatible   = []
    incompatible = []
    already_done = []
    transcoded   = []
    failed       = []

    for rk, (item, src_path, codec) in all_items.items():
        codec_lower = (codec or '').lower()
        title       = getattr(item, 'title', rk)
        show_title  = getattr(item, 'grandparentTitle', None)
        label       = f'[{show_title}] {title}' if show_title else title

        if codec_lower not in INCOMPATIBLE_VIDEO_CODECS:
            compatible.append({'rk': rk, 'title': label, 'codec': codec})
            continue

        # Incompatible codec — find existing file by ratingKey suffix
        existing = None
        if os.path.isdir(optimized_dir):
            existing = next(
                (f for f in os.listdir(optimized_dir)
                 if f.endswith(f'_{rk}.mp4')
                 and os.path.getsize(os.path.join(optimized_dir, f)) > 0),
                None
            )
        opt_path = os.path.join(optimized_dir,
                                existing or _opt_filename(item, rk))

        if existing:
            already_done.append({'rk': rk, 'title': label, 'codec': codec})
            print(f'  ✓ Already optimized: {label}')
            continue

        incompatible.append({'rk': rk, 'title': label, 'codec': codec,
                              'src': src_path})

        if dry_run:
            print(f'  ⚠ Would transcode: {label}  [{codec}]')
            continue

        # Check for cancel sentinel before starting next item
        if sync_root:
            cancel_path = os.path.join(sync_root, OPTIMIZED_DIR_NAME, '.cancel')
            if os.path.exists(cancel_path):
                print('\n[CANCELLED] Cancel requested — stopping after current item.')
                try:
                    os.remove(cancel_path)
                except Exception:
                    pass
                write_status(sync_root, state='complete',
                             current_item='Cancelled',
                             finished=datetime.now(timezone.utc).isoformat(),
                             final_failed=len(failed))
                break

        # Transcode
        print(f'  ⚡ Transcoding: {label}  [{codec}]')
        src_size = os.path.getsize(src_path) / 1024 / 1024 if os.path.exists(src_path) else 0
        print(f'    Source: {os.path.basename(src_path)}  ({src_size:.1f} MB)')

        if sync_root:
            write_status(sync_root,
                current_item=label,
                current_index=len(transcoded) + len(failed) + 1,
                transcoded=len(transcoded),
                failed=len(failed),
                updated=datetime.now(timezone.utc).isoformat(),
            )

        os.makedirs(optimized_dir, exist_ok=True)
        ok = transcode_file(src_path, opt_path, vaapi_device, quality, audio_bitrate)
        if ok:
            transcoded.append({'rk': rk, 'title': label, 'codec': codec})
        else:
            failed.append({'rk': rk, 'title': label, 'codec': codec})

    return {
        'slot':        slot_name,
        'total':       len(all_items),
        'compatible':  compatible,
        'already':     already_done,
        'transcoded':  transcoded,
        'failed':      failed,
        'would_do':    incompatible if dry_run else [],
    }


def print_summary(results: list, dry_run: bool) -> None:
    print(f'\n{"=" * 60}')
    print('  Optimization Summary')
    print(f'{"=" * 60}')

    total_transcoded = sum(len(r.get('transcoded', [])) for r in results)
    total_already    = sum(len(r.get('already',    [])) for r in results)
    total_failed     = sum(len(r.get('failed',     [])) for r in results)
    total_would      = sum(len(r.get('would_do',   [])) for r in results)
    total_compatible = sum(len(r.get('compatible', [])) for r in results)

    for r in results:
        if 'error' in r:
            print(f'  {r.get("slot", "?")} : ERROR — {r["error"]}')
            continue
        slot = r['slot']
        if dry_run:
            print(f'  {slot} : {r["total"]} items, '
                  f'{len(r["already"])} already optimized, '
                  f'{len(r["would_do"])} would transcode')
        else:
            print(f'  {slot} : {r["total"]} items, '
                  f'{len(r["transcoded"])} transcoded, '
                  f'{len(r["already"])} already done, '
                  f'{len(r["failed"])} failed')

    print()
    if dry_run:
        print(f'  Compatible (no action needed) : {total_compatible}')
        print(f'  Already optimized             : {total_already}')
        print(f'  Would transcode               : {total_would}')
    else:
        print(f'  Compatible (no action needed) : {total_compatible}')
        print(f'  Already optimized             : {total_already}')
        print(f'  Newly transcoded              : {total_transcoded}')
        if total_failed:
            print(f'  Failed                        : {total_failed}  ← check output above')


# ══════════════════════════════════════════════════════════════════════════════
# CACHE CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

def cleanup_cache(sync_root: str, active_keys: set, dry_run: bool = False) -> int:
    """
    Remove optimized files whose ratingKey is no longer in any slot.
    Parses rk from filename suffix: 'Show S01E02_293000.mp4' → '293000'.
    Returns number of files removed (or that would be removed in dry-run).
    """
    optimized_dir = os.path.join(sync_root, OPTIMIZED_DIR_NAME)
    if not os.path.isdir(optimized_dir):
        return 0

    removed = 0
    for fname in os.listdir(optimized_dir):
        if not fname.endswith('.mp4'):
            continue
        # Parse ratingKey from suffix
        stem  = fname[:-4]  # strip .mp4
        parts = stem.rsplit('_', 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue  # unknown format, leave it alone
        rk = parts[1]
        if rk in active_keys:
            continue
        fpath = os.path.join(optimized_dir, fname)
        if dry_run:
            print(f'  🗑 Would delete stale: {fname}')
        else:
            try:
                os.remove(fpath)
                print(f'  🗑 Deleted stale: {fname}')
                removed += 1
            except Exception as e:
                print(f'  [WARNING] Could not delete {fname}: {e}')
    return removed


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    p = argparse.ArgumentParser(
        description='Pre-transcode incompatible Plex content for PlexSyncer.')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--slot',      metavar='NAME', help='Optimize a single slot')
    mode.add_argument('--all-slots', action='store_true', help='Optimize all slots')
    p.add_argument('--dry-run', action='store_true',
                   help='Report what would be transcoded without doing it')
    p.add_argument('--sync', action='store_true',
                   help='Run plex_hardlink_sync.py for the same slot(s) after optimizing')
    args = p.parse_args()

    if not args.slot and not args.all_slots:
        p.error('Specify --slot NAME or --all-slots')

    lock_fd = None  # lock managed externally by flock(1) in webhook/cron

    plex_cfg  = _load_global_plex_cfg()
    sync_root = plex_cfg.get('sync_root', '')

    if not sync_root:
        print('[ERROR] sync_root not set in configs/plex.json')
        sys.exit(1)


    # Gate real transcoding behind the config flag.
    # dry-run always works so users can see what would happen before enabling.
    if not args.dry_run and not plex_cfg.get('transcode_incompatible', False):
        print('[INFO] Transcoding is not enabled.')
        print('       Go to Settings → Sync Settings and enable "Codec Optimization",')
        print('       then click Save Settings before running Optimize & Sync.\n')
        sys.exit(0)

    if not shutil.which('ffmpeg'):
        print('[ERROR] ffmpeg not found. Install with: sudo apt install ffmpeg')
        sys.exit(1)

    host, token = load_plex_credentials({})
    plex        = connect(host, token)

    managed_user = plex_cfg.get('managed_user', '').strip()
    if managed_user:
        print(f'Switching to managed user "{managed_user}"...', end='', flush=True)
        try:
            plex = plex.switchUser(managed_user)
            print(' [DONE]')
        except Exception as e:
            print(f' [FAILED: {e}] — continuing as admin')

    slot_names = [args.slot] if args.slot else get_all_slot_names()
    if not slot_names:
        print('No slot configs found.')
        sys.exit(0)

    if args.dry_run:
        print('\n[DRY RUN — no files will be transcoded]\n')
    else:
        # Clear any leftover cancel sentinel from a previous run
        cancel_path = os.path.join(sync_root, OPTIMIZED_DIR_NAME, '.cancel')
        if os.path.exists(cancel_path):
            try:
                os.remove(cancel_path)
                print('[INFO] Cleared leftover cancel sentinel from previous run.')
            except Exception:
                pass
        # Tee stdout to log file so UI can tail it after a page refresh
        sys.stdout = Tee(LOG_FILE)
        # Write initial status
        write_status(sync_root,
            state='running',
            slot=', '.join(slot_names),
            pid=os.getpid(),
            started=datetime.now(timezone.utc).isoformat(),
            current_item='Starting...',
            current_index=0,
            total=0,  # updated per slot
            transcoded=0,
            failed=0,
            log_file=LOG_FILE,
            with_sync=args.sync,
        )

    results     = []
    active_keys = set()  # all ratingKeys seen across ALL slots (for cleanup)

    # When running a single slot, still collect items from all other slots
    # so cleanup doesn't incorrectly mark their optimized files as stale.
    all_slot_names = get_all_slot_names()
    if set(slot_names) != set(all_slot_names):
        for other_slot in all_slot_names:
            if other_slot in slot_names:
                continue
            other_cfg = load_slot_config(other_slot)
            if other_cfg:
                other_items = collect_items_for_slot(plex, other_cfg)
                active_keys.update(other_items.keys())

    for slot_name in slot_names:
        print(f'\n{"=" * 60}')
        print(f'  Slot : {slot_name}')
        print(f'{"=" * 60}')
        r = optimize_slot(slot_name, plex, plex_cfg, dry_run=args.dry_run,
                          sync_root=sync_root)
        results.append(r)
        # Accumulate all rks seen (compatible + incompatible + already done)
        for key in ('compatible', 'already', 'transcoded', 'failed', 'would_do'):
            for item in r.get(key, []):
                active_keys.add(item['rk'])

    print_summary(results, dry_run=args.dry_run)

    # Cleanup stale cache entries
    print(f'\n{"=" * 60}')
    print('  Cache Cleanup')
    print(f'{"=" * 60}')
    n_removed = cleanup_cache(sync_root, active_keys, dry_run=args.dry_run)
    if n_removed == 0 and not args.dry_run:
        print('  No stale files.')
    elif args.dry_run:
        stale_count = sum(
            1 for f in os.listdir(os.path.join(sync_root, OPTIMIZED_DIR_NAME))
            if f.endswith('.mp4') and f[:-4].rsplit('_', 1)[-1].isdigit()
            and f[:-4].rsplit('_', 1)[-1] not in active_keys
        ) if os.path.isdir(os.path.join(sync_root, OPTIMIZED_DIR_NAME)) else 0
        if stale_count == 0:
            print('  No stale files.')

    print(f'\nOptimized files cache: {os.path.join(sync_root, OPTIMIZED_DIR_NAME)}/')

    if not args.dry_run:
        total_failed = sum(len(r.get('failed', [])) for r in results)
        write_status(sync_root,
            state='complete',
            finished=datetime.now(timezone.utc).isoformat(),
            current_item='Done',
            final_failed=total_failed,
        )
        cancel_path = os.path.join(sync_root, OPTIMIZED_DIR_NAME, '.cancel')
        if os.path.exists(cancel_path):
            try:
                os.remove(cancel_path)
            except Exception:
                pass

    # Chain into sync if requested (lock is still held, sync won't double-run).
    if args.sync and not args.dry_run:
        print(f'\n{"=" * 60}')
        print('  Chaining into plex_hardlink_sync.py...')
        print(f'{"=" * 60}')
        sync_cmd = [sys.executable, SYNC_WORKER]
        if args.slot:
            sync_cmd += ['--slot', args.slot]
        else:
            sync_cmd += ['--all-slots']
        # Pass the open lock fd so sync inherits it — keeps the lock held.
        subprocess.run(sync_cmd, cwd=os.path.dirname(__file__))

    # Restore stdout from Tee
    if not args.dry_run and isinstance(sys.stdout, Tee):
        sys.stdout.close()
        sys.stdout = sys.__stdout__



if __name__ == '__main__':
    main()
