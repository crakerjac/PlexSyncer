"""
plex_hardlink_sync.py

Syncs Plex content to slot-specific directories as hard links, then writes a
_plezy_meta/manifest.json that the Plezy Android app reads to register files
as offline downloads without requiring a Plex connection on the phone.

── Slot mode (recommended) ───────────────────────────────────────────────────
    python3 plex_hardlink_sync.py --slot tablet
    python3 plex_hardlink_sync.py --all-slots          # CRON

── Legacy playlist mode (testing) ───────────────────────────────────────────
    python3 plex_hardlink_sync.py \
        --host "http://192.168.1.100:32400" --token "TOKEN" \
        --sync-dir "/media/drive/PlexSync/tablet" \
        -p "Kids Car"

── Plex connection ───────────────────────────────────────────────────────────
Shared across all slots.  Stored in configs/plex.json (written by sync_ui.py).
Per-slot plex config is still supported for backward compatibility.

── Requirements ──────────────────────────────────────────────────────────────
    pip install plexapi requests

── Show sync modes ───────────────────────────────────────────────────────────
    {"mode": "all"}                         all episodes
    {"mode": "latest",        "count": N}   N most-recently aired episodes
    {"mode": "next_unwatched","count": N}   N next unwatched episodes
    {"next": N}                             legacy alias for next_unwatched N
"""

from __future__ import annotations

import os, re, json, glob, argparse, requests
from datetime import datetime, timezone
from typing import Optional

import plexapi, plexapi.exceptions
from plexapi.server import PlexServer
from plexapi.video import Show

# ── Constants ─────────────────────────────────────────────────────────────────

SUBTITLE_EXTS   = {'.srt', '.vtt', '.ass', '.ssa', '.sub'}
PROTECTED_NAMES = {'_plezy_meta', '.stfolder', '.stversions', '.stignore'}
CONFIGS_DIR     = os.path.join(os.path.dirname(__file__), 'configs')
PLEX_CONFIG     = os.path.join(CONFIGS_DIR, 'plex.json')


# ══════════════════════════════════════════════════════════════════════════════
# PLEX CONNECTION
# ══════════════════════════════════════════════════════════════════════════════

def _load_global_plex_cfg() -> dict:
    """Load configs/plex.json if it exists, return empty dict otherwise."""
    if os.path.exists(PLEX_CONFIG):
        with open(PLEX_CONFIG, encoding='utf-8') as f:
            return json.load(f)
    return {}


def load_plex_credentials(slot_config: dict) -> tuple[str, str]:
    """
    Return (host, token).  Prefers per-slot config, falls back to
    configs/plex.json (written by the UI), then errors out.
    """
    if 'plex' in slot_config:
        return slot_config['plex']['host'], slot_config['plex']['token']
    if os.path.exists(PLEX_CONFIG):
        with open(PLEX_CONFIG, encoding='utf-8') as f:
            cfg = json.load(f)
        return cfg['host'], cfg['token']
    raise RuntimeError(
        'No Plex credentials found.  Run the UI to configure, or add '
        '"plex": {"host": "...", "token": "..."} to your slot config.'
    )


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


# ══════════════════════════════════════════════════════════════════════════════
# FILENAME / PATH HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def sanitize_filename(name: str) -> str:
    """Mirror Plezy's _sanitizeFileName() from download_storage_service.dart."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'^\.+|\.+$', '', name)
    name = name.replace('.', '_')
    return name.strip()


def build_relative_path(item, source_ext: str,
                        show_year: Optional[int] = None) -> str:
    if item.TYPE == 'episode':
        show_title    = sanitize_filename(item.grandparentTitle)
        year          = show_year or getattr(item, 'grandparentYear', None)
        show_folder   = f"{show_title} ({year})" if year else show_title
        season_num    = item.parentIndex or 0
        episode_num   = item.index or 0
        season_folder = f"Season {season_num:02d}"
        ep_title      = sanitize_filename(item.title)

        if season_num == 0 or episode_num == 0:
            # Plex couldn't match this episode to a season/number.
            # Fall back to air date if available, otherwise use the raw title.
            air_date = getattr(item, 'originallyAvailableAt', None)
            if air_date:
                date_str = air_date.strftime('%Y-%m-%d')
                filename = f"{date_str} - {ep_title}.{source_ext}"
            else:
                filename = f"S{season_num:02d}E{episode_num:02d} - {ep_title}.{source_ext}"
            print(f'  [WARNING] "{item.grandparentTitle} - {item.title}" has no '
                  f'season/episode number in Plex metadata. '
                  f'Using filename: {filename}')
        else:
            filename = f"S{season_num:02d}E{episode_num:02d} - {ep_title}.{source_ext}"

        return f"TV Shows/{show_folder}/{season_folder}/{filename}"
    elif item.TYPE == 'movie':
        year   = getattr(item, 'year', None)
        title  = sanitize_filename(item.title)
        folder = f"{title} ({year})" if year else title
        return f"Movies/{folder}/{folder}.{source_ext}"
    else:
        return f"{sanitize_filename(item.title)}.{source_ext}"


def build_subtitle_dest(video_rel: str, suffix: str) -> str:
    return os.path.splitext(video_rel)[0] + suffix


# ══════════════════════════════════════════════════════════════════════════════
# PLEX ITEM RESOLUTION
# ══════════════════════════════════════════════════════════════════════════════

def pick_best_version(item) -> tuple[Optional[str], Optional[str]]:
    """Lowest bitrate, file size as tiebreaker. Returns (source_path, ext)."""
    try:
        best = min(
            item.media,
            key=lambda m: (
                m.bitrate or 999_999,
                m.parts[0].size if m.parts and m.parts[0].size else float('inf'),
            ),
        )
        path = best.parts[0].file
        return path, os.path.splitext(path)[1].lstrip('.')
    except (IndexError, AttributeError, ValueError):
        return None, None


def find_subtitle_sidecars(source_video_path: str,
                           languages: Optional[list] = None,
                           forced_only: bool = False) -> list[tuple[str, str]]:
    """
    Scan source dir for sidecar subtitle files matching the video base name.
    Returns [(absolute_path, suffix)] where suffix e.g. '.en.srt' or '.srt'.

    languages: list of ISO codes to include, e.g. ['en', 'es'].
               Use ['all'] or None to include everything.
    forced_only: if True, only include files whose name contains 'forced'.

    Sidecar naming conventions handled:
      video.srt          → no language code, always included
      video.en.srt       → language 'en'
      video.en.forced.srt → forced English
    """
    vdir  = os.path.dirname(source_video_path)
    vbase = os.path.splitext(os.path.basename(source_video_path))[0]
    include_all = not languages or 'all' in languages
    out = []
    try:
        for fname in sorted(os.listdir(vdir)):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SUBTITLE_EXTS:
                continue
            if not fname.startswith(vbase):
                continue
            suffix = fname[len(vbase):]           # e.g. '.en.srt' or '.srt'
            lower_suffix = suffix.lower()

            if forced_only and 'forced' not in lower_suffix:
                continue

            if not include_all:
                # Parse language code from suffix like '.en.srt' or '.en.forced.srt'
                parts = lower_suffix.lstrip('.').split('.')
                if len(parts) >= 2:
                    lang_code = parts[0]           # 'en' from 'en.srt'
                    if lang_code not in languages:
                        continue
                # If suffix is just '.srt' (no lang code), always include it

            out.append((os.path.join(vdir, fname), suffix))
    except OSError:
        pass
    return out


def _find_show(plex: PlexServer, title: str) -> Optional[Show]:
    results  = plex.library.search(title=title, libtype='show')
    exact    = [r for r in results if r.title.lower() == title.lower()]
    cands    = exact or results
    if not cands:
        print(f'    [WARNING] Show not found: "{title}"')
        return None
    if len(cands) > 1:
        print(f'    [WARNING] Multiple matches for "{title}", using first.')
    return cands[0]


def collect_playlist_items(plex: PlexServer, name: str) -> dict:
    print(f'  Playlist "{name}"...', end='', flush=True)
    try:
        pl = plex.playlist(name)
    except plexapi.exceptions.NotFound:
        print(' [NOT FOUND -- skipped]')
        return {}
    print(f' [{pl.leafCount} items]')
    out = {}
    for item in pl.items():
        sp, ext = pick_best_version(item)
        if sp is None:
            print(f'    [WARNING] No file: {item.title}')
            continue
        out[str(item.ratingKey)] = (item, sp, ext)
    return out


def collect_movie(plex: PlexServer, title: str) -> dict:
    print(f'  Movie "{title}"...', end='', flush=True)
    try:
        results = plex.library.search(title=title, libtype='movie')
    except Exception as e:
        print(f' [ERROR: {e}]')
        return {}
    exact = [r for r in results if r.title.lower() == title.lower()]
    cands = exact or results
    if not cands:
        print(' [NOT FOUND -- skipped]')
        return {}
    if len(cands) > 1:
        print(f'\n    [WARNING] Multiple matches, using first.')
    item = cands[0]
    sp, ext = pick_best_version(item)
    if sp is None:
        print(' [NO FILE -- skipped]')
        return {}
    print(f' [found: {item.title} ({item.year})]')
    return {str(item.ratingKey): (item, sp, ext)}


def collect_show_episodes(plex: PlexServer, show_title: str,
                           mode_cfg: dict) -> dict:
    """
    Resolve episodes for a show according to mode_cfg:
      {"mode": "all"}
      {"mode": "latest",        "count": N}
      {"mode": "next_unwatched","count": N}
      {"next": N}  -- legacy alias
    """
    # Normalise legacy format
    if 'next' in mode_cfg and 'mode' not in mode_cfg:
        mode_cfg = {'mode': 'next_unwatched', 'count': mode_cfg['next']}

    mode  = mode_cfg.get('mode', 'next_unwatched')
    count = mode_cfg.get('count', 1)

    label = {
        'all':            'all episodes',
        'latest':         f'{count} latest',
        'next_unwatched': f'next {count} unwatched',
    }.get(mode, mode)

    print(f'  Show "{show_title}" ({label})...', end='', flush=True)

    show = _find_show(plex, show_title)
    if show is None:
        print()
        return {}

    show_year: Optional[int] = show.year

    if mode == 'all':
        episodes = show.episodes()
    elif mode == 'latest':
        all_eps  = show.episodes()
        episodes = all_eps[-count:] if len(all_eps) >= count else all_eps
    else:  # next_unwatched
        episodes = show.unwatched()[:count]

    if not episodes:
        print(' [0 episodes]')
        return {}

    print(f' [{len(episodes)} episode(s), year: {show_year}]')
    out = {}
    for ep in episodes:
        sp, ext = pick_best_version(ep)
        if sp is None:
            print(f'    [WARNING] No file: {ep.title}')
            continue
        out[str(ep.ratingKey)] = (ep, sp, ext, show_year)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# MANIFEST
# ══════════════════════════════════════════════════════════════════════════════

def build_manifest_entry(item, rel: str,
                         show_year: Optional[int] = None) -> dict:
    e = {
        'ratingKey':    str(item.ratingKey),
        'type':         item.TYPE,
        'title':        item.title,
        'thumb':        getattr(item, 'thumb', None),
        'summary':      getattr(item, 'summary', '') or '',
        'duration':     getattr(item, 'duration', 0) or 0,
        'addedAt':      datetime.now(timezone.utc).isoformat(),
        'relativePath': rel.replace(os.sep, '/'),
    }
    if item.TYPE == 'episode':
        year = show_year or getattr(item, 'grandparentYear', None)
        e.update({
            'grandparentTitle':     item.grandparentTitle,
            'grandparentYear':      year,
            'grandparentRatingKey': str(item.grandparentRatingKey) if getattr(item, 'grandparentRatingKey', None) else None,
            'grandparentThumb':     getattr(item, 'grandparentThumb', None),
            'parentTitle':          getattr(item, 'parentTitle', None),
            'parentRatingKey':      str(item.parentRatingKey) if item.parentRatingKey is not None else None,
            'seasonNumber':         item.parentIndex,
            'episodeNumber':        item.index,
        })
    elif item.TYPE == 'movie':
        e['year'] = getattr(item, 'year', None)
    return e


def write_manifest(sync_dir: str, server_id: str, server_name: str,
                   entries: list) -> None:
    meta = os.path.join(sync_dir, '_plezy_meta')
    os.makedirs(meta, exist_ok=True)
    manifest = {
        'version':     1,
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'serverId':    server_id,
        'serverName':  server_name,
        'items':       entries,
    }
    path = os.path.join(meta, 'manifest.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f'Manifest written: {len(entries)} item(s) -> {path}')


# ══════════════════════════════════════════════════════════════════════════════
# PRUNING
# ══════════════════════════════════════════════════════════════════════════════

def is_protected(name: str) -> bool:
    return name in PROTECTED_NAMES or name.startswith('.')


def prune(sync_dir: str, expected: set) -> int:
    removed = 0
    for root, dirs, files in os.walk(sync_dir, topdown=False):
        dirs[:] = [d for d in dirs if not is_protected(d)]
        for fname in files:
            if is_protected(fname):
                continue
            full = os.path.join(root, fname)
            rel  = os.path.relpath(full, sync_dir).replace(os.sep, '/')
            if rel not in expected:
                os.remove(full)
                removed += 1
        if root == sync_dir:
            continue
        top = os.path.relpath(root, sync_dir).split(os.sep)[0]
        if is_protected(top):
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass
    return removed


# ══════════════════════════════════════════════════════════════════════════════
# CORE SYNC
# ══════════════════════════════════════════════════════════════════════════════

def link_file(src: str, dst: str) -> bool:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        return True
    try:
        os.link(src, dst)
        return True
    except OSError as e:
        if e.errno == 18:
            print(f'\n  [ERROR] Cross-device link: {os.path.basename(dst)}'
                  f'\n          sync_root and Plex library must be on the same partition.')
        else:
            print(f'\n  [ERROR] {os.path.basename(dst)}: {e}')
        return False


def sync_slot_dir(sync_dir: str, all_items: dict,
                  server_id: str, server_name: str,
                  sub_languages: Optional[list] = None,
                  sub_forced: bool = False) -> None:
    os.makedirs(sync_dir, exist_ok=True)

    exp_video: dict = {}
    exp_subs:  dict = {}

    for rk, tup in all_items.items():
        item, sp, ext = tup[0], tup[1], tup[2]
        show_year     = tup[3] if len(tup) > 3 else None
        rel = build_relative_path(item, ext, show_year=show_year)
        if rel in exp_video:
            print(f'  [WARNING] Collision "{rel}" -- keeping first.')
            continue
        exp_video[rel] = (item, sp, show_year)
        for sub_src, suffix in find_subtitle_sidecars(sp, sub_languages, sub_forced):
            exp_subs[build_subtitle_dest(rel, suffix)] = sub_src

    all_expected = set(exp_video) | set(exp_subs)

    print('Pruning stale files...', end='', flush=True)
    print(f' [removed {prune(sync_dir, all_expected)}]')

    print('Linking video files...')
    new_v = skip_v = err_v = 0
    entries = []
    for rel, (item, sp, show_year) in exp_video.items():
        existed = os.path.exists(os.path.join(sync_dir, rel))
        if link_file(sp, os.path.join(sync_dir, rel)):
            if existed:
                skip_v += 1
            else:
                print(f'  + {rel} ({os.path.getsize(sp)/1024/1024:.1f} MB)')
                new_v += 1
            entries.append(build_manifest_entry(item, rel, show_year))
        else:
            err_v += 1

    new_s = skip_s = err_s = 0
    if exp_subs:
        print('Linking subtitle sidecars...')
    for rel, sp in exp_subs.items():
        existed = os.path.exists(os.path.join(sync_dir, rel))
        if link_file(sp, os.path.join(sync_dir, rel)):
            if existed: skip_s += 1
            else:
                print(f'  + {rel}')
                new_s += 1
        else:
            err_s += 1

    print(f'\nVideos : {new_v} new, {skip_v} existing, {err_v} errors')
    if exp_subs:
        print(f'Subs   : {new_s} new, {skip_s} existing, {err_s} errors')

    write_manifest(sync_dir, server_id, server_name, entries)


# ══════════════════════════════════════════════════════════════════════════════
# SLOT CONFIG
# ══════════════════════════════════════════════════════════════════════════════

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


def run_slot(slot_name: str) -> None:
    config = load_slot_config(slot_name)
    if config is None:
        return

    host, token  = load_plex_credentials(config)
    plex_cfg     = _load_global_plex_cfg()
    sync_root    = plex_cfg.get('sync_root', '')
    if not sync_root:
        print('[ERROR] sync_root not set.  Run the UI to configure it '
              'under Global Settings, or add "sync_root" to configs/plex.json.')
        return

    sub_languages  = plex_cfg.get('subtitle_languages',   ['en'])
    sub_forced     = plex_cfg.get('subtitle_forced_only',  False)
    sync_dir       = os.path.join(sync_root, slot_name)
    selections     = config.get('selections', {})

    print(f'\n{"=" * 60}')
    print(f'  Slot : {slot_name}')
    print(f'  Dir  : {sync_dir}')
    print(f'{"=" * 60}')

    plex = connect(host, token)

    managed_user = plex_cfg.get('managed_user', '').strip()
    if managed_user:
        print(f'Switching to managed user "{managed_user}"...', end='', flush=True)
        try:
            plex = plex.switchUser(managed_user)
            print(' [DONE]')
        except Exception as e:
            print(f' [FAILED: {e}]')
            print('  Continuing as admin account.')

    server_id   = plex.machineIdentifier
    server_name = plex.friendlyName
    all_items: dict = {}

    def merge(d: dict) -> None:
        for k, v in d.items():
            if k not in all_items:
                all_items[k] = v

    print('\nResolving selections...')
    for name in selections.get('playlists', []):
        merge(collect_playlist_items(plex, name))
    for title in selections.get('movies', []):
        merge(collect_movie(plex, title))
    for show_title, mode_cfg in selections.get('shows', {}).items():
        if isinstance(mode_cfg, int):
            mode_cfg = {'mode': 'next_unwatched', 'count': mode_cfg}
        merge(collect_show_episodes(plex, show_title, mode_cfg))

    total = len(all_items)
    print(f'\nTotal unique items: {total}')
    if total == 0:
        print('Nothing to sync.')
        return

    sync_slot_dir(sync_dir, all_items, server_id, server_name,
                  sub_languages=sub_languages, sub_forced=sub_forced)
    print(f'\nSlot "{slot_name}" complete.')


# ══════════════════════════════════════════════════════════════════════════════
# LEGACY PLAYLIST MODE
# ══════════════════════════════════════════════════════════════════════════════

def run_legacy(args) -> None:
    plex        = connect(args.host, args.token)
    server_id   = plex.machineIdentifier
    server_name = plex.friendlyName
    print(f'Server ID: {server_id}')
    all_items: dict = {}
    print('\nResolving playlists...')
    for name in args.playlist:
        for k, v in collect_playlist_items(plex, name).items():
            if k not in all_items:
                all_items[k] = v
    total = len(all_items)
    print(f'\nTotal unique items: {total}')
    if total == 0:
        print('Nothing to sync.')
        return
    sync_slot_dir(args.sync_dir, all_items, server_id, server_name)
    print('\nSync complete.')


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    p = argparse.ArgumentParser(
        description='Sync Plex content as hard links with a Plezy-compatible manifest.')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--slot',      metavar='NAME')
    mode.add_argument('--all-slots', action='store_true')
    p.add_argument('--host',     default='http://127.0.0.1:32400')
    p.add_argument('--token',    default=None)
    p.add_argument('--sync-dir', default=None)
    p.add_argument('-p', '--playlist', action='append', metavar='NAME')
    args = p.parse_args()

    if args.slot:
        run_slot(args.slot)
    elif args.all_slots:
        names = get_all_slot_names()
        if not names:
            print('No slot configs found.')
            return
        print(f'Running {len(names)} slot(s): {", ".join(names)}')
        for name in names:
            run_slot(name)
        print('\nAll slots complete.')
    else:
        missing = [f for f, v in [
            ('--token', args.token), ('--sync-dir', args.sync_dir),
            ('-p', args.playlist)] if not v]
        if missing:
            p.error(f'Legacy mode requires: {", ".join(missing)}')
        run_legacy(args)


if __name__ == '__main__':
    main()
