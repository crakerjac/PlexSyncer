"""
sync_ui.py  —  PlexSyncer Management UI  (cart + search layout)
Drop-in replacement. Same config files, same worker invocation.

pip install "streamlit>=1.34" plexapi
streamlit run sync_ui.py
"""

import os, json, glob, subprocess, sys
from typing import Optional
import streamlit as st

VERSION     = 'v1.2.0'
APP_ICON    = '📼'
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIGS_DIR = os.path.join(SCRIPT_DIR, 'configs')
PLEX_CONFIG = os.path.join(CONFIGS_DIR, 'plex.json')
WORKER      = os.path.join(SCRIPT_DIR, 'plex_hardlink_sync.py')
OPTIMIZER   = os.path.join(SCRIPT_DIR, 'plex_optimize.py')
LOCK_FILE   = '/tmp/plexsyncer.lock'
LOG_FILE    = '/tmp/plexsyncer_optimize.log'
os.makedirs(CONFIGS_DIR, exist_ok=True)

SYNC_MODE_LABELS = [
    "All episodes",
    "Latest episode",
    "3 latest episodes",
    "5 latest episodes",
    "Next unwatched",
    "Next 3 unwatched",
    "Next 5 unwatched",
    "Next 10 unwatched",
    "Next 15 unwatched",
    "Next 20 unwatched",
]
SYNC_MODE_CONFIGS = [
    {"mode": "all"},
    {"mode": "latest",         "count": 1},
    {"mode": "latest",         "count": 3},
    {"mode": "latest",         "count": 5},
    {"mode": "next_unwatched", "count": 1},
    {"mode": "next_unwatched", "count": 3},
    {"mode": "next_unwatched", "count": 5},
    {"mode": "next_unwatched", "count": 10},
    {"mode": "next_unwatched", "count": 15},
    {"mode": "next_unwatched", "count": 20},
]

def mode_cfg_to_label(cfg) -> str:
    if not cfg:
        return "Next unwatched"
    if isinstance(cfg, dict) and 'next' in cfg and 'mode' not in cfg:
        cfg = {'mode': 'next_unwatched', 'count': cfg['next']}
    for label, c in zip(SYNC_MODE_LABELS, SYNC_MODE_CONFIGS):
        if c == cfg:
            return label
    return "Next unwatched"

def label_to_mode_cfg(label: str) -> dict:
    idx = SYNC_MODE_LABELS.index(label) if label in SYNC_MODE_LABELS else 4
    return SYNC_MODE_CONFIGS[idx]


# ══════════════════════════════════════════════════════════════════════════════
# LOCK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _read_optimize_status(sync_root: str) -> Optional[dict]:
    """Read the optimizer status file if it exists."""
    if not sync_root:
        return None
    path = os.path.join(sync_root, '_optimized', '.status.json')
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _tail_log(n: int = 100) -> str:
    """Return last n lines of the optimizer log file."""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            return ''.join(lines[-n:])
    except Exception:
        pass
    return ''


def _is_locked() -> bool:
    """Return True if the PlexSyncer flock is held by another process."""
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
# CONFIG I/O  —  unchanged format, backward compatible
# ══════════════════════════════════════════════════════════════════════════════

def load_plex_config() -> dict:
    """Read plex.json from disk. Use get_plex_config() during rendering."""
    defaults = {
        'host':                   'http://localhost:32400',
        'token':                  '',
        'managed_user':           '',
        'sync_root':              '',
        'subtitle_languages':     ['en'],
        'subtitle_forced_only':   False,
        'hidden_libraries':       [],
        'transcode_incompatible': False,
        'transcode_device':       '/dev/dri/renderD128',
        'transcode_quality':      23,
        'transcode_audio_bitrate': '256k',
    }
    if os.path.exists(PLEX_CONFIG):
        with open(PLEX_CONFIG, encoding='utf-8') as f:
            defaults.update(json.load(f))
    return defaults

def get_plex_config() -> dict:
    """
    Session-cached config. Avoids repeated disk reads during a single render.
    Invalidated by _invalidate_config_cache() after any settings save.
    """
    if '_plex_cfg_cache' not in st.session_state:
        st.session_state['_plex_cfg_cache'] = load_plex_config()
    return st.session_state['_plex_cfg_cache']

def _invalidate_config_cache() -> None:
    st.session_state.pop('_plex_cfg_cache', None)
    # Dir-size walk results depend on sync_root, so invalidate those too
    for key in list(st.session_state.keys()):
        if key.startswith('_dir_size_'):
            del st.session_state[key]

def save_plex_config(cfg: dict) -> None:
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    with open(PLEX_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)

def list_slots() -> list:
    paths = glob.glob(os.path.join(CONFIGS_DIR, '*.json'))
    names = [os.path.splitext(os.path.basename(p))[0] for p in sorted(paths)]
    return [n for n in names if n not in ('plex', 'example')]

def load_slot_config(slot: str) -> dict:
    path = os.path.join(CONFIGS_DIR, f'{slot}.json')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {'slot_name': slot, 'selections': {'playlists': [], 'movies': [], 'shows': {}}}

def save_slot_config(slot: str, selections: dict) -> None:
    cfg = {'slot_name': slot, 'selections': selections}
    with open(os.path.join(CONFIGS_DIR, f'{slot}.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
# PLEX CONNECTION
# plex_admin  — admin token, used only for home-user list + switchUser()
# plex_browse — used for all library/playlist browsing (may be managed user)
# ══════════════════════════════════════════════════════════════════════════════

def get_browse_plex():
    return st.session_state.get('plex_browse')

def try_connect(host: str, token: str) -> tuple:
    try:
        from plexapi.server import PlexServer
        admin = PlexServer(host.strip(), token.strip())
        st.session_state['plex_admin'] = admin
        _apply_managed_user()
        _invalidate_library_cache()
        st.session_state.pop('home_users', None)
        return True, admin.friendlyName
    except Exception as e:
        st.session_state.pop('plex_admin',  None)
        st.session_state.pop('plex_browse', None)
        return False, str(e)

def _apply_managed_user() -> None:
    admin = st.session_state.get('plex_admin')
    if admin is None:
        return
    managed = get_plex_config().get('managed_user', '').strip()
    if managed:
        try:
            st.session_state['plex_browse'] = admin.switchUser(managed)
            return
        except Exception:
            pass
    st.session_state['plex_browse'] = admin

def auto_connect() -> None:
    """Run once per session. Connects silently using stored config."""
    if 'autoconnect_done' in st.session_state:
        return
    st.session_state['autoconnect_done'] = True
    # Read directly from disk — session cache doesn't exist yet at startup
    cfg = load_plex_config()
    if not cfg.get('token'):
        return
    ok, msg = try_connect(cfg['host'], cfg['token'])
    if ok:
        st.session_state['_startup_toast'] = ('✅', f'Connected to {msg}')
    # Silently ignore connection failures on startup — user can fix in settings

def get_home_users() -> list:
    if 'home_users' in st.session_state:
        return st.session_state['home_users']
    admin = st.session_state.get('plex_admin')
    if admin is None:
        return []
    try:
        users = [u.title for u in admin.myPlexAccount().users() if u.home]
        st.session_state['home_users'] = users
        return users
    except Exception:
        st.session_state['home_users'] = []
        return []


# ══════════════════════════════════════════════════════════════════════════════
# LIBRARY CACHE
# ══════════════════════════════════════════════════════════════════════════════

def get_sections() -> list:
    if 'plex_sections' in st.session_state:
        return st.session_state['plex_sections']
    plex = get_browse_plex()
    if plex is None:
        return []
    sections = [
        {'key': s.key, 'title': s.title, 'type': s.type}
        for s in plex.library.sections()
        if s.type in ('movie', 'show')
    ]
    st.session_state['plex_sections'] = sections
    return sections

def get_visible_sections() -> list:
    hidden = set(get_plex_config().get('hidden_libraries', []))
    return [s for s in get_sections() if s['title'] not in hidden]

def get_section_items(section_key, letter: str = '') -> list:
    """Fetch items for a section, optionally filtered by first letter.
    Results are cached per section+letter combination.
    letter='' returns nothing (placeholder state).
    letter='#' returns titles starting with a digit.
    """
    if not letter:
        return []
    cache_key = f'section_items_{section_key}_{letter}'
    if cache_key in st.session_state:
        return st.session_state[cache_key]
    plex = get_browse_plex()
    if plex is None:
        return []
    section = plex.library.sectionByID(section_key)
    if section.type == 'show':
        raw = section.searchShows()
        all_items = sorted(
            [{'title':          i.title,
              'year':           getattr(i, 'year', None),
              'ratingKey':      str(i.ratingKey),
              'unwatchedCount': getattr(i, 'unwatchedLeafCount', None)}
             for i in raw],
            key=lambda x: x['title'].lower()
        )
    else:
        raw = section.all()
        all_items = sorted(
            [{'title':     i.title,
              'year':      getattr(i, 'year', None),
              'ratingKey': str(i.ratingKey)}
             for i in raw],
            key=lambda x: x['title'].lower()
        )
    # Filter by letter
    if letter == '#':
        items = [i for i in all_items if i['title'] and i['title'][0].isdigit()]
    else:
        # Strip common leading articles for sorting (The, A, An)
        def sort_title(t):
            for prefix in ('the ', 'a ', 'an '):
                if t.lower().startswith(prefix):
                    return t[len(prefix):]
            return t
        items = [i for i in all_items
                 if sort_title(i['title'])[:1].upper() == letter.upper()]
    st.session_state[cache_key] = items
    # Invalidate rk_index so it gets rebuilt with the new items
    st.session_state.pop('_rk_index', None)
    return items

def get_playlists() -> list:
    if 'plex_playlists' in st.session_state:
        return st.session_state['plex_playlists']
    plex = get_browse_plex()
    if plex is None:
        return []
    pls = sorted(
        [{'title': p.title, 'leafCount': p.leafCount, 'ratingKey': str(p.ratingKey)}
         for p in plex.playlists(playlistType='video') if p.leafCount > 0],
        key=lambda x: x['title'].lower()
    )
    st.session_state['plex_playlists'] = pls
    return pls

def _invalidate_library_cache() -> None:
    for key in list(st.session_state.keys()):
        if key.startswith('section_items_') or key in (
                'plex_sections', 'plex_playlists', '_rk_index'):
            del st.session_state[key]


# ══════════════════════════════════════════════════════════════════════════════
# RATING KEY INDEX
# Lazy O(1) title→ratingKey lookup. Built once from cached section items,
# rebuilt automatically after library cache invalidation.
# ══════════════════════════════════════════════════════════════════════════════

def _get_rk_index() -> dict:
    """Returns {(title, item_type): ratingKey} for all cached section items.
    Scans all letter-suffixed caches (alpha-index) as well as the plain key.
    Always rebuilt fresh — not cached — so removals are reflected immediately.
    """
    idx = {}
    for section in get_sections():
        sec_type = section['type']
        sec_key  = section['key']
        # Plain key (legacy / search ALL)
        for item in st.session_state.get(f'section_items_{sec_key}', []):
            idx[(item['title'], sec_type)] = item['ratingKey']
        # Letter-suffixed keys from alpha-index browser
        prefix = f'section_items_{sec_key}_'
        for key, items in st.session_state.items():
            if isinstance(key, str) and key.startswith(prefix) and isinstance(items, list):
                for item in items:
                    idx[(item['title'], sec_type)] = item['ratingKey']
    return idx


# ══════════════════════════════════════════════════════════════════════════════
# SELECTION STATE
#
# _saved_movies    : set of titles  — canonical, survives widget key deletion
# _saved_shows     : dict title→mode_cfg
# _saved_playlists : set of titles
# _dirty           : bool — True when unsaved changes exist
#
# KEY INVARIANT: on_change callbacks update _saved_* IMMEDIATELY whenever any
# checkbox or selectbox changes. This means even if Streamlit re-renders
# a widget (e.g. when switching tabs), _saved_* already has the correct value.
# build_selections_from_widgets() can safely fall back to _saved_* for any
# item whose widget key is absent.
# ══════════════════════════════════════════════════════════════════════════════

def _get_saved() -> tuple:
    return (
        st.session_state.get('_saved_movies',    set()),
        st.session_state.get('_saved_shows',     {}),
        st.session_state.get('_saved_playlists', set()),
    )

def _on_movie_change(rk: str, title: str, slot: str) -> None:
    checked      = st.session_state.get(f'chk_mov_{slot}_{rk}', False)
    saved_movies = st.session_state.get('_saved_movies', set())
    if checked:
        saved_movies.add(title)
    else:
        saved_movies.discard(title)
    st.session_state['_saved_movies'] = saved_movies
    st.session_state['_dirty']        = True

def _on_show_change(rk: str, title: str, slot: str) -> None:
    checked     = st.session_state.get(f'chk_show_{slot}_{rk}', False)
    saved_shows = st.session_state.get('_saved_shows', {})
    if checked:
        label = st.session_state.get(f'mode_show_{slot}_{rk}', 'Next unwatched')
        saved_shows[title] = label_to_mode_cfg(label)
    else:
        saved_shows.pop(title, None)
    st.session_state['_saved_shows'] = saved_shows
    st.session_state['_dirty']       = True

def _on_mode_change(rk: str, title: str, slot: str) -> None:
    if not st.session_state.get(f'chk_show_{slot}_{rk}', False):
        return
    label       = st.session_state.get(f'mode_show_{slot}_{rk}', 'Next unwatched')
    saved_shows = st.session_state.get('_saved_shows', {})
    saved_shows[title] = label_to_mode_cfg(label)
    st.session_state['_saved_shows'] = saved_shows
    st.session_state['_dirty']       = True

def _on_playlist_change(rk: str, title: str, slot: str) -> None:
    checked         = st.session_state.get(f'chk_pl_{slot}_{rk}', False)
    saved_playlists = st.session_state.get('_saved_playlists', set())
    if checked:
        saved_playlists.add(title)
    else:
        saved_playlists.discard(title)
    st.session_state['_saved_playlists'] = saved_playlists
    st.session_state['_dirty']           = True

def switch_slot(slot: str) -> None:
    """
    Reload _saved_* from disk config. Widget keys are namespaced by slot
    to prevent ghost callbacks, so deleting keys here is no longer needed.
    """
    cfg = load_slot_config(slot)
    sel = cfg.get('selections', {})
    st.session_state['_saved_movies']    = set(sel.get('movies', []))
    st.session_state['_saved_playlists'] = set(sel.get('playlists', []))
    st.session_state['_saved_shows']     = sel.get('shows', {})
    st.session_state['_loaded_slot']     = slot
    st.session_state['_dirty']           = False

def build_selections_from_widgets(slot: str) -> dict:
    """
    Build selections dict for saving.
    For each item: use widget key if present, else fall back to _saved_*.
    _saved_* is always current because on_change callbacks update it
    immediately on every interaction — so the fallback is always correct
    even for items on other tabs whose widget keys may not exist yet.
    """
    s_movies, s_shows, s_playlists = _get_saved()
    movies, shows, playlists       = {}, {}, {}
    for section in get_sections():
        items = st.session_state.get(f'section_items_{section["key"]}', [])
        for item in items:
            rk, title = item['ratingKey'], item['title']
            if section['type'] == 'movie':
                chk = f'chk_mov_{slot}_{rk}'
                movies[title] = st.session_state[chk] if chk in st.session_state \
                                else (title in s_movies)
            elif section['type'] == 'show':
                chk  = f'chk_show_{slot}_{rk}'
                mode = f'mode_show_{slot}_{rk}'
                if chk in st.session_state:
                    if st.session_state[chk]:
                        shows[title] = label_to_mode_cfg(
                            st.session_state.get(mode, 'Next unwatched'))
                elif title in s_shows:
                    shows[title] = s_shows[title]
    for pl in st.session_state.get('plex_playlists', []):
        rk, title = pl['ratingKey'], pl['title']
        chk = f'chk_pl_{slot}_{rk}'
        playlists[title] = st.session_state[chk] if chk in st.session_state \
                           else (title in s_playlists)
    return {
        'movies':    sorted(t for t, v in movies.items()    if v),
        'shows':     shows,
        'playlists': sorted(t for t, v in playlists.items() if v),
    }

# ── Cart removal helpers ──────────────────────────────────────────────────────

def _find_rk(title: str, item_type: str) -> Optional[str]:
    """O(1) ratingKey lookup via the cached index."""
    return _get_rk_index().get((title, item_type))

def remove_from_cart(title: str, item_type: str, slot: str) -> None:
    if item_type == 'movie':
        saved = st.session_state.get('_saved_movies', set())
        saved.discard(title)
        st.session_state['_saved_movies'] = saved
        rk = _find_rk(title, 'movie')
        if rk:
            st.session_state[f'chk_mov_{slot}_{rk}'] = False
    elif item_type == 'show':
        saved = st.session_state.get('_saved_shows', {})
        saved.pop(title, None)
        st.session_state['_saved_shows'] = saved
        rk = _find_rk(title, 'show')
        if rk:
            st.session_state[f'chk_show_{slot}_{rk}'] = False
    elif item_type == 'playlist':
        saved = st.session_state.get('_saved_playlists', set())
        saved.discard(title)
        st.session_state['_saved_playlists'] = saved
        for pl in st.session_state.get('plex_playlists', []):
            if pl['title'] == title:
                st.session_state[f'chk_pl_{slot}_{pl["ratingKey"]}'] = False
    st.session_state['_dirty'] = True


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS DIALOG
# Requires Streamlit >= 1.34.  All settings in one place, tabbed.
# ══════════════════════════════════════════════════════════════════════════════

@st.dialog(f"{APP_ICON}  Settings", width="large")
def show_settings() -> None:
    plex_cfg = get_plex_config()

    tab_conn, tab_sync, tab_libs, tab_slots = st.tabs(
        ["Plex Connection", "Sync Settings", "Libraries", "Slots"]
    )

    # ── Plex Connection ───────────────────────────────────────────────────────
    with tab_conn:
        st.text_input(
            "Host", value=plex_cfg.get('host', 'http://localhost:32400'),
            key='sdlg_host',
            help="Usually doesn't need changing — PlexSyncer must run on the same machine as Plex"
        )
        st.text_input(
            "Plex Token", value='', type='password',
            key='sdlg_token',
            placeholder='Leave blank to keep existing token' if plex_cfg.get('token') else 'Enter your Plex token',
            help="Plex Web → Settings → Troubleshooting → Show secret token"
        )
        if plex_cfg.get('token'):
            st.caption("Token is already configured.")

        home_users    = get_home_users()
        managed_opts  = ['(main account)'] + home_users
        saved_managed = plex_cfg.get('managed_user', '')
        managed_idx   = managed_opts.index(saved_managed) if saved_managed in managed_opts else 0
        st.selectbox("Browse as user", managed_opts, index=managed_idx, key='sdlg_managed')

        c1, c2 = st.columns([1, 3])
        if c1.button("Test Connection", key='sdlg_test', use_container_width=True):
            test_token = st.session_state.get('sdlg_token', '').strip() or plex_cfg.get('token', '')
            with st.spinner("Testing…"):
                ok, msg = try_connect(st.session_state['sdlg_host'], test_token)
            if ok:
                c2.success(f"✓ Connected to {msg}")
            else:
                c2.error(f"✗ {msg}")
        elif get_browse_plex():
            admin = st.session_state.get('plex_admin')
            c2.caption(f"Currently connected: **{admin.friendlyName}**")

    # ── Sync Settings ─────────────────────────────────────────────────────────
    with tab_sync:
        st.text_input(
            "Sync root directory",
            value=plex_cfg.get('sync_root', ''),
            key='sdlg_sync_root',
            placeholder='/media/drive/PlexSyncer',
            help="Must be on the same filesystem partition as your Plex media library (required for hard links)"
        )
        st.text_input(
            "Subtitle languages",
            value=', '.join(plex_cfg.get('subtitle_languages', ['en'])),
            key='sdlg_sub_langs',
            placeholder='en, es  — or  all'
        )
        st.checkbox(
            "Forced subtitles only",
            value=plex_cfg.get('subtitle_forced_only', False),
            key='sdlg_sub_forced'
        )

        st.divider()
        st.markdown('**Codec Optimization**')
        st.caption(
            'Pre-transcode files with incompatible codecs using plex_optimize.py. '
            'Click ⚡ in the main toolbar to run optimization for a slot.'
        )
        st.checkbox(
            'Enable optimization (use optimized versions when available)',
            value=plex_cfg.get('transcode_incompatible', False),
            key='sdlg_transcode_enabled'
        )
        st.text_input(
            'VAAPI device',
            value=plex_cfg.get('transcode_device', '/dev/dri/renderD128'),
            key='sdlg_transcode_device',
            help='Hardware accelerator device node for VAAPI encoding'
        )
        col_q, col_a = st.columns(2)
        col_q.number_input(
            'Quality (CRF/global_quality)',
            min_value=0, max_value=51,
            value=int(plex_cfg.get('transcode_quality', 23)),
            key='sdlg_transcode_quality',
            help='Lower = better quality, larger file. 23 is a good default.'
        )
        col_a.text_input(
            'Audio bitrate',
            value=plex_cfg.get('transcode_audio_bitrate', '256k'),
            key='sdlg_transcode_audio',
            help='AAC audio bitrate e.g. 192k, 256k, 320k'
        )

    # ── Libraries ─────────────────────────────────────────────────────────────
    with tab_libs:
        st.caption("Hidden libraries won't appear as tabs or in search results. Does not affect the worker.")
        sections = get_sections()
        if not sections:
            st.info("Connect to Plex first to see your libraries.")
        else:
            hidden = set(plex_cfg.get('hidden_libraries', []))
            for section in sections:
                st.toggle(
                    f"{section['title']}  —  {section['type']}",
                    value=section['title'] not in hidden,
                    key=f'sdlg_libvis_{section["key"]}'
                )

    # ── Slots ─────────────────────────────────────────────────────────────────
    with tab_slots:
        slots = list_slots()
        if slots:
            st.caption(f"{len(slots)} slot{'s' if len(slots) != 1 else ''} configured")
            for s in slots:
                c1, c2 = st.columns([6, 1])
                c1.write(s)
                if c2.button("🗑", key=f'sdlg_del_{s}', help=f'Delete slot "{s}"'):
                    os.remove(os.path.join(CONFIGS_DIR, f'{s}.json'))
                    # Clear _loaded_slot so the deleted slot isn't re-loaded
                    if st.session_state.get('_loaded_slot') == s:
                        st.session_state.pop('_loaded_slot', None)
                    st.session_state['_toast_msg'] = ('🗑', f'Deleted slot "{s}"')
                    st.rerun()
        else:
            st.info("No slots yet.")

        st.divider()
        new_name = st.text_input("New slot name", placeholder="e.g. OnePlus13r",
                                 key='sdlg_new_slot')
        if st.button("Create slot", use_container_width=True, key='sdlg_create'):
            name = new_name.strip()
            if not name:
                st.warning("Enter a name.")
            elif name in slots:            # reuse list already fetched above
                st.warning(f'"{name}" already exists.')
            else:
                save_slot_config(name, {'playlists': [], 'movies': [], 'shows': {}})
                st.session_state['_toast_msg'] = ('✅', f'Created slot "{name}"')
                st.rerun()

    # ── Save ──────────────────────────────────────────────────────────────────
    st.divider()
    if st.button("💾  Save Settings", type="primary", use_container_width=True):
        new_token      = st.session_state.get('sdlg_token', '').strip()
        new_host       = st.session_state.get('sdlg_host',  plex_cfg.get('host', ''))
        new_sync_root  = st.session_state.get('sdlg_sync_root', '')
        new_managed    = st.session_state.get('sdlg_managed', '(main account)')
        new_sub_langs  = st.session_state.get('sdlg_sub_langs', 'en')
        new_sub_forced = st.session_state.get('sdlg_sub_forced', False)

        langs      = [l.strip() for l in new_sub_langs.split(',') if l.strip()] or ['en']
        new_hidden = [
            s['title'] for s in get_sections()
            if not st.session_state.get(f'sdlg_libvis_{s["key"]}', True)
        ]

        save_plex_config({
            'host':                   new_host.strip(),
            'token':                  new_token or plex_cfg.get('token', ''),
            'managed_user':           '' if new_managed == '(main account)' else new_managed,
            'sync_root':              new_sync_root.strip(),
            'subtitle_languages':     langs,
            'subtitle_forced_only':   new_sub_forced,
            'hidden_libraries':       new_hidden,
            'transcode_incompatible': st.session_state.get('sdlg_transcode_enabled', False),
            'transcode_device':       st.session_state.get('sdlg_transcode_device', '/dev/dri/renderD128').strip(),
            'transcode_quality':      int(st.session_state.get('sdlg_transcode_quality', 23)),
            'transcode_audio_bitrate': st.session_state.get('sdlg_transcode_audio', '256k').strip(),
        })
        _invalidate_config_cache()
        _apply_managed_user()
        _invalidate_library_cache()
        st.toast('Settings saved ✓', icon='💾')
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SYNC DIRECTORY SIZE
# Walks the slot's sync directory on disk — fast, accurate, no Plex calls.
# Result cached in session state; invalidated after sync or config change.
# ══════════════════════════════════════════════════════════════════════════════

def get_slot_dir_size(slot: str) -> Optional[float]:
    """Returns current on-disk size of the slot directory in GB, or None."""
    cache_key = f'_dir_size_{slot}'
    if cache_key in st.session_state:
        return st.session_state[cache_key]
    sync_root = get_plex_config().get('sync_root', '').strip()
    if not sync_root or not slot:
        return None
    slot_dir = os.path.join(sync_root, slot)
    if not os.path.isdir(slot_dir):
        return None
    total = 0
    for dirpath, _, filenames in os.walk(slot_dir):
        for fname in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                pass
    gb = total / (1024 ** 3)
    st.session_state[cache_key] = gb
    return gb


def _clear_section(item_type: str, slot: str) -> None:
    """Remove all items of a given type from the cart."""
    if item_type == 'movie':
        for section in get_sections():
            if section['type'] != 'movie':
                continue
            for item in st.session_state.get(f'section_items_{section["key"]}', []):
                st.session_state[f'chk_mov_{slot}_{item["ratingKey"]}'] = False
        st.session_state['_saved_movies'] = set()
    elif item_type == 'show':
        for section in get_sections():
            if section['type'] != 'show':
                continue
            for item in st.session_state.get(f'section_items_{section["key"]}', []):
                st.session_state[f'chk_show_{slot}_{item["ratingKey"]}'] = False
        st.session_state['_saved_shows'] = {}
    elif item_type == 'playlist':
        for pl in st.session_state.get('plex_playlists', []):
            st.session_state[f'chk_pl_{slot}_{pl["ratingKey"]}'] = False
        st.session_state['_saved_playlists'] = set()
    st.session_state['_dirty'] = True


# ══════════════════════════════════════════════════════════════════════════════
# CART PANEL  —  left column
# ══════════════════════════════════════════════════════════════════════════════

def render_cart(slot: str) -> None:
    s_movies, s_shows, s_playlists = _get_saved()
    total = len(s_movies) + len(s_shows) + len(s_playlists)

    # ── Header row: title + total count ───────────────────────────────────────
    h1, h2 = st.columns([3, 2])
    h1.markdown("##### Sync Queue")
    if total:
        h2.caption(f"{total} item{'s' if total != 1 else ''} selected")

    if total == 0:
        st.caption("Nothing selected. Browse and check items on the right →")
        return

    # ── Disk size + clear all ─────────────────────────────────────────────────
    gb = get_slot_dir_size(slot)
    sz1, sz2 = st.columns([3, 2])
    if gb is not None:
        sz1.caption(f"📁 {gb:.1f} GB on disk")
    elif get_plex_config().get('sync_root'):
        sz1.caption("📁 Sync directory not found yet")
    if sz2.button("✕ Clear All", key=f'clr_all_{slot}', help="Remove everything from queue"):
        _clear_section('movie',    slot)
        _clear_section('show',     slot)
        _clear_section('playlist', slot)
        st.rerun()

    st.divider()

    # ── Movies ────────────────────────────────────────────────────────────────
    if s_movies:
        st.caption(f"**Movies** — {len(s_movies)}")
        for title in sorted(s_movies):
            c1, c2 = st.columns([10, 1])
            c1.caption(title)
            if c2.button("✕", key=f'rm_mov_{slot}_{title}', help="Remove"):
                remove_from_cart(title, 'movie', slot)
                st.rerun()

    # ── Shows ─────────────────────────────────────────────────────────────────
    if s_shows:
        st.caption(f"**Shows** — {len(s_shows)}")
        for title, mode_cfg in sorted(s_shows.items()):
            c1, c2, c3 = st.columns([5, 4, 1])
            c1.caption(title)
            c2.caption(f"_{mode_cfg_to_label(mode_cfg)}_")
            if c3.button("✕", key=f'rm_show_{slot}_{title}', help="Remove"):
                remove_from_cart(title, 'show', slot)
                st.rerun()

    # ── Playlists ─────────────────────────────────────────────────────────────
    if s_playlists:
        st.caption(f"**Playlists** — {len(s_playlists)}")
        for title in sorted(s_playlists):
            c1, c2 = st.columns([10, 1])
            c1.caption(title)
            if c2.button("✕", key=f'rm_pl_{slot}_{title}', help="Remove"):
                remove_from_cart(title, 'playlist', slot)
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# BROWSER PANEL  —  right column
# One tab per visible Plex library + one Playlists tab.
# All items rendered at once — page scrolls naturally, no height constraint.
# ══════════════════════════════════════════════════════════════════════════════

def render_browser(slot: str) -> None:
    plex     = get_browse_plex()
    sections = get_visible_sections()
    pls      = get_playlists()

    if not plex:
        st.info("⚙ Open Settings to configure your Plex connection.")
        return

    if not sections and not pls:
        st.info("No visible libraries. Check Settings → Libraries.")
        return

    tab_labels = [s['title'] for s in sections]
    if pls:
        tab_labels.append("Playlists")

    tabs = st.tabs(tab_labels)

    for i, section in enumerate(sections):
        with tabs[i]:
            _render_section(section, slot)

    if pls:
        with tabs[-1]:
            _render_playlists(slot)


def _render_section(section: dict, slot: str) -> None:
    s_movies, s_shows, _ = _get_saved()
    sec_key  = section['key']
    sec_type = section['type']

    # ── Alpha index ───────────────────────────────────────────────────────
    letters  = ['#'] + list('ABCDEFGHIJKLMNOPQRSTUVWXYZ')
    state_key = f'alpha_{sec_key}'
    if state_key not in st.session_state:
        st.session_state[state_key] = ''

    # Render letter buttons in rows of 9
    selected = st.session_state[state_key]
    row_size = 9
    for row_start in range(0, len(letters), row_size):
        cols = st.columns(row_size)
        for col_idx, letter in enumerate(letters[row_start:row_start + row_size]):
            btn_type = 'primary' if letter == selected else 'secondary'
            if cols[col_idx].button(letter, key=f'alpha_{sec_key}_{letter}',
                                    type=btn_type, use_container_width=True):
                # Toggle off if already selected, switch otherwise
                st.session_state[state_key] = '' if letter == selected else letter
                st.rerun()

    # ── Search bar ────────────────────────────────────────────────────────
    search = st.text_input(
        'Search', placeholder='Search titles…',
        key=f'search_{sec_key}', label_visibility='collapsed'
    )

    if search:
        # Search across entire library (use cached full list if available)
        full_cache_key = f'section_items_{sec_key}_ALL'
        if full_cache_key not in st.session_state:
            with st.spinner('Searching…'):
                plex_s   = get_browse_plex()
                section_s = plex_s.library.sectionByID(sec_key)
                if section_s.type == 'show':
                    raw_s = section_s.searchShows()
                    st.session_state[full_cache_key] = [
                        {'title': i.title, 'year': getattr(i, 'year', None),
                         'ratingKey': str(i.ratingKey),
                         'unwatchedCount': getattr(i, 'unwatchedLeafCount', None)}
                        for i in raw_s]
                else:
                    raw_s = section_s.all()
                    st.session_state[full_cache_key] = [
                        {'title': i.title, 'year': getattr(i, 'year', None),
                         'ratingKey': str(i.ratingKey)}
                        for i in raw_s]
        all_items = st.session_state[full_cache_key]
        items = [i for i in all_items if search.lower() in i['title'].lower()]
        st.caption(f'{len(items)} result(s) for "{search}"')
    elif not selected:
        st.caption('Select a letter to browse or search above.')
        return
    else:
        # ── Load items for selected letter ────────────────────────────────
        with st.spinner(f'Loading {selected}…'):
            items = get_section_items(sec_key, selected)
        if not items:
            st.caption(f'No titles under {selected}.')
            return
        st.caption(f'{len(items)} title(s) under {selected}')

    if sec_type == 'movie':
        cols = st.columns(3)
        for idx, item in enumerate(items):
            rk, title = item['ratingKey'], item['title']
            label     = f"{title} ({item['year']})" if item['year'] else title
            cols[idx % 3].checkbox(
                label,
                key=f'chk_mov_{slot}_{rk}',
                value=title in s_movies,
                on_change=_on_movie_change, args=(rk, title, slot)
            )

    elif sec_type == 'show':
        h1, h2 = st.columns([3, 2])
        h1.caption('**Show**')
        h2.caption('**Sync mode**')
        for item in items:
            rk, title    = item['ratingKey'], item['title']
            unwatched    = item.get('unwatchedCount')
            base_label   = f"{title} ({item['year']})" if item['year'] else title
            badge_label  = f"{base_label}  ·  {unwatched} unwatched" \
                           if unwatched else base_label

            c1, c2 = st.columns([3, 2])
            checked = c1.checkbox(
                badge_label,
                key=f'chk_show_{slot}_{rk}',
                value=title in s_shows,
                on_change=_on_show_change, args=(rk, title, slot)
            )
            if checked:
                c2.selectbox(
                    '##', SYNC_MODE_LABELS,
                    key=f'mode_show_{slot}_{rk}',
                    index=SYNC_MODE_LABELS.index(
                        mode_cfg_to_label(s_shows.get(title, {}))
                    ),
                    label_visibility='collapsed',
                    on_change=_on_mode_change, args=(rk, title, slot)
                )
            else:
                c2.caption('—')


def _render_playlists(slot: str) -> None:
    playlists    = get_playlists()
    _, _, s_pls  = _get_saved()

    search   = st.text_input(
        "Filter", placeholder="Filter playlists…",
        key='search_pl', label_visibility='collapsed'
    )
    filtered = [p for p in playlists
                if not search or search.lower() in p['title'].lower()]
    st.caption(f"{len(filtered)} of {len(playlists)}")

    for pl in filtered:
        rk, title = pl['ratingKey'], pl['title']
        st.checkbox(
            f"{title}  ({pl['leafCount']} items)",
            key=f'chk_pl_{slot}_{rk}',
            value=title in s_pls,
            on_change=_on_playlist_change, args=(rk, title, slot)
        )


# ══════════════════════════════════════════════════════════════════════════════
# SYNC OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def run_sync_live(slot_name: str) -> None:
    # Phase 2: stored output — display with st.status
    if '_sync_output' in st.session_state:
        rc    = st.session_state.get('_sync_rc', 0)
        lines = st.session_state['_sync_output']
        state = 'complete' if rc == 0 else 'error'
        label = '✅ Sync complete' if rc == 0 else f'❌ Sync failed (exit {rc})'
        with st.status(label, state=state, expanded=True):
            st.code('\n'.join(lines), language=None)
        if st.button('← Back to configuration', key='btn_back'):
            st.session_state.pop('_sync_output', None)
            st.session_state.pop('_sync_rc',     None)
            st.session_state['_show_sync'] = False
            st.session_state.pop('_loaded_slot', None)
            st.rerun()
        return

    # Phase 1: stream output — bail if lock is held
    if _check_lock_and_warn():
        st.session_state['_show_sync'] = False
        st.rerun()
        return

    with st.status(f'⏳ Syncing {slot_name}…', expanded=True) as status:
        lines:  list = []
        output = st.empty()
        proc   = subprocess.Popen(
            [sys.executable, WORKER, '--slot', slot_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=SCRIPT_DIR,
        )
        for raw in proc.stdout:
            lines.append(raw.rstrip())
            output.code('\n'.join(lines), language=None)
        proc.wait()
        if proc.returncode == 0:
            status.update(label='✅ Sync complete',
                          state='complete', expanded=True)
        else:
            status.update(label=f'❌ Sync failed (exit {proc.returncode})',
                          state='error', expanded=True)

    # Invalidate dir-size cache so the cart shows updated size after next sync
    st.session_state.pop(f'_dir_size_{slot_name}', None)
    st.session_state['_sync_output'] = lines
    st.session_state['_sync_rc']     = proc.returncode
    st.rerun()


def _render_status_banner(plex_cfg: dict) -> None:
    """Render a persistent top-of-page banner when optimization is running."""
    sync_root = plex_cfg.get('sync_root', '')
    status    = _read_optimize_status(sync_root)
    if not status:
        return
    state = status.get('state', '')
    if state == 'running':
        slot    = status.get('slot', '?')
        item    = status.get('current_item', '...')
        done    = status.get('transcoded', 0)
        failed  = status.get('failed', 0)
        started = status.get('started', '')
        st.warning(
            f'⚡ **Optimizing {slot}** — {item}  '
            f'({done} done, {failed} failed)  '
            f'[started {started[:16].replace("T", " ")} UTC]',
            icon='⏳'
        )
        c1, c2 = st.columns([1, 5])
        if c1.button('View progress', key='btn_banner_view'):
            st.session_state['_show_optimize'] = True
            st.session_state.pop('_opt_dry_output', None)
            st.session_state.pop('_opt_output', None)
            st.rerun()
        c2.empty()
    elif state == 'complete':
        failed = status.get('final_failed', 0)
        icon   = '✅' if not failed else '⚠️'
        msg    = f'{icon} Optimization complete'
        if failed:
            msg += f' — {failed} item(s) failed (see log)'
        c1, c2, c3 = st.columns([2, 1, 3])
        c1.success(msg)
        if c2.button('View log', key='btn_banner_log'):
            st.session_state['_show_optimize'] = True
            st.session_state['_show_complete_log'] = True
            st.rerun()
        if c3.button('Dismiss', key='btn_banner_dismiss'):
            path = os.path.join(plex_cfg.get('sync_root', ''),
                                '_optimized', '.status.json')
            try:
                os.remove(path)
            except Exception:
                pass
            st.rerun()


def _check_lock_and_warn() -> bool:
    """Show a warning and return True if the lock is held (block launch)."""
    if _is_locked():
        st.warning(
            '⏳ A PlexSyncer process (cron or another UI tab) is already running. '
            'Wait for it to finish before starting a new run.',
            icon='🔒'
        )
        return True
    return False


def run_optimize_live(slot_name: str, with_sync: bool = False) -> None:
    """Run plex_optimize.py for the current slot and stream output.
    Phase 1 (⚡):    dry-run — shows what would be transcoded.
    Phase 2 confirm: real transcode + optional sync (⚡▶).
    """
    # Always clear a stale 'complete' status at entry so it never blocks
    # a fresh ⚡ click. The banner Dismiss button is optional — this is
    # the guaranteed cleanup path.
    _plex_cfg_top = load_plex_config()
    _sr_top = _plex_cfg_top.get('sync_root', '')
    if _sr_top and not st.session_state.get('_show_complete_log'):
        _st_top = _read_optimize_status(_sr_top)
        if _st_top and _st_top.get('state') == 'complete':
            try:
                os.remove(os.path.join(_sr_top, '_optimized', '.status.json'))
            except Exception:
                pass

    # ── Status view: only when coming from banner or no in-session work yet ──
    # Fires when: banner "View progress" clicked (_show_complete_log),
    # OR there is an active status file but no in-session dry/real output yet
    # (i.e. user navigated here after a page refresh mid-transcode).
    no_session_output = (
        '_opt_output' not in st.session_state
        and '_opt_dry_output' not in st.session_state
        and st.session_state.get('_pending_optimize_real') != slot_name
    )
    if st.session_state.get('_show_complete_log') or no_session_output:
        plex_cfg  = load_plex_config()
        sync_root = plex_cfg.get('sync_root', '')
        status    = _read_optimize_status(sync_root)
        # Only intercept if there is actually a status file with something to show
        if status and status.get('state') in ('running', 'complete'):
            state   = status.get('state', '')
            slot_s  = status.get('slot', '?')
            if state == 'running':
                st.subheader(f'⚡ Optimizing {slot_s}…')
                item    = status.get('current_item', '...')
                done    = status.get('transcoded', 0)
                failed  = status.get('failed', 0)
                st.info(f'Current: **{item}**  |  {done} done, {failed} failed')
                st.code(_tail_log(80), language=None)
                cancel_path = os.path.join(sync_root, '_optimized', '.cancel')
                if os.path.exists(cancel_path):
                    st.warning('⏹ Cancel requested — waiting for current item to finish…')
                c1, c2, c3 = st.columns([1, 1, 4])
                if c1.button('⏹ Stop', key='btn_status_cancel', type='secondary',
                             help='Stop after current file finishes'):
                    try:
                        os.makedirs(os.path.dirname(cancel_path), exist_ok=True)
                        open(cancel_path, 'w').close()
                    except Exception:
                        pass
                    st.rerun()
                if c2.button('← Main', key='btn_status_main',
                             help='Return to main screen (transcode continues)'):
                    st.session_state['_show_optimize'] = False
                    st.session_state.pop('_show_complete_log', None)
                    st.rerun()
                c3.caption('Auto-refreshing every 5 seconds…')
                import time; time.sleep(5)
                st.rerun()
                return
            elif state == 'complete':
                st.subheader('✅ Optimization complete')
                st.code(_tail_log(200), language=None)
                if st.button('← Back', key='btn_status_back'):
                    st.session_state['_show_optimize'] = False
                    st.session_state.pop('_show_complete_log', None)
                    st.rerun()
                return
        # No status file — fall through to dry-run (phase 1)

    # ── Phase 3: real optimize (or optimize+sync) output ──────────────────
    if '_opt_output' in st.session_state:
        rc    = st.session_state.get('_opt_rc', 0)
        lines = st.session_state['_opt_output']
        label = ('✅ Optimize & Sync complete' if st.session_state.get('_opt_with_sync')
                 else '✅ Optimize complete') if rc == 0 \
                else f'❌ Failed (exit {rc})'
        state = 'complete' if rc == 0 else 'error'
        with st.status(label, state=state, expanded=True):
            st.code('\n'.join(lines), language=None)
        if st.button('← Back to configuration', key='btn_opt_back'):
            for k in ('_opt_output', '_opt_rc', '_opt_with_sync',
                      '_opt_dry_output', '_opt_dry_rc'):
                st.session_state.pop(k, None)
            st.session_state['_show_optimize'] = False
            st.session_state.pop('_loaded_slot', None)
            st.rerun()
        return

    # ── Phase 2: dry-run done — show confirm buttons ───────────────────────
    if '_opt_dry_output' in st.session_state:
        rc    = st.session_state.get('_opt_dry_rc', 0)
        lines = st.session_state['_opt_dry_output']
        state = 'complete' if rc == 0 else 'error'
        with st.status('🔍 Dry-run complete — review and confirm', state=state,
                       expanded=True):
            st.code('\n'.join(lines), language=None)

        st.caption('Choose an action:')
        c1, c2, c3 = st.columns(3)

        if c1.button('⚡ Optimize only', use_container_width=True,
                     key='btn_opt_confirm'):
            if not _check_lock_and_warn():
                st.session_state.pop('_opt_dry_output', None)
                st.session_state.pop('_opt_dry_rc', None)
                st.session_state['_opt_with_sync'] = False
                st.session_state['_pending_optimize_real'] = slot_name
                st.rerun()

        if c2.button('⚡▶ Optimize & Sync', use_container_width=True,
                     type='primary', key='btn_opt_sync_confirm'):
            if not _check_lock_and_warn():
                st.session_state.pop('_opt_dry_output', None)
                st.session_state.pop('_opt_dry_rc', None)
                st.session_state['_opt_with_sync'] = True
                st.session_state['_pending_optimize_real'] = slot_name
                st.rerun()

        if c3.button('← Cancel', use_container_width=True, key='btn_opt_cancel'):
            for k in ('_opt_dry_output', '_opt_dry_rc', '_opt_with_sync'):
                st.session_state.pop(k, None)
            st.session_state['_show_optimize'] = False
            st.rerun()
        return

    # ── Phase 2b: run real optimize (after confirm) ────────────────────────
    if st.session_state.get('_pending_optimize_real') == slot_name:
        del st.session_state['_pending_optimize_real']
        # Clear dry-run output so confirm buttons don't render underneath
        for k in ('_opt_dry_output', '_opt_dry_rc'):
            st.session_state.pop(k, None)
        do_sync = st.session_state.get('_opt_with_sync', False)
        label   = f'⚡▶ Optimizing & syncing {slot_name}…' if do_sync \
                  else f'⚡ Optimizing {slot_name}…'
        # Wrap with flock so UI runs can't overlap with cron/webhook
        cmd = ['/usr/bin/flock', '-n', LOCK_FILE,
               sys.executable, OPTIMIZER, '--slot', slot_name]
        if do_sync:
            cmd += ['--sync']

        # Controls visible during streaming
        plex_cfg_run  = load_plex_config()
        sync_root_run = plex_cfg_run.get('sync_root', '')
        cancel_path   = os.path.join(sync_root_run, '_optimized', '.cancel')
        ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 4])
        stop_clicked = ctrl1.button('⏹ Stop', key='btn_run_stop',
                                    help='Stop after current file finishes')
        back_clicked = ctrl2.button('← Main', key='btn_run_back',
                                    help='Return to main (transcode continues in background)')
        if stop_clicked:
            try:
                os.makedirs(os.path.dirname(cancel_path), exist_ok=True)
                open(cancel_path, 'w').close()
            except Exception:
                pass
        if back_clicked:
            st.session_state['_show_optimize'] = False
            st.rerun()

        with st.status(label, expanded=True) as status:
            lines:  list = []
            output = st.empty()
            proc   = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=SCRIPT_DIR,
            )
            for raw in proc.stdout:
                lines.append(raw.rstrip())
                output.code('\n'.join(lines), language=None)
            proc.wait()
            ok_label = '✅ Optimize & Sync complete' if do_sync \
                       else '✅ Optimize complete'
            if proc.returncode == 0:
                status.update(label=ok_label, state='complete', expanded=True)
            else:
                status.update(label=f'❌ Failed (exit {proc.returncode})',
                              state='error', expanded=True)
        if do_sync:
            st.session_state.pop(f'_dir_size_{slot_name}', None)
        st.session_state['_opt_output'] = lines
        st.session_state['_opt_rc']     = proc.returncode
        st.rerun()
        return

    # ── Phase 1: run dry-run ───────────────────────────────────────────────
    with st.status(f'🔍 Scanning {slot_name} for incompatible codecs…',
                   expanded=True) as status:
        lines:  list = []
        output = st.empty()
        proc   = subprocess.Popen(
            [sys.executable, OPTIMIZER, '--slot', slot_name, '--dry-run'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=SCRIPT_DIR,
        )
        for raw in proc.stdout:
            lines.append(raw.rstrip())
            output.code('\n'.join(lines), language=None)
        proc.wait()
        status.update(label='🔍 Scan complete — review below',
                      state='complete', expanded=True)

    st.session_state['_opt_dry_output'] = lines
    st.session_state['_opt_dry_rc']     = proc.returncode
    st.session_state['_show_optimize']  = False  # routing now driven by _opt_dry_output
    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def _do_save(slot: str) -> int:
    """Save current widget state to disk. Returns item count."""
    plex_cfg = get_plex_config()
    if not plex_cfg.get('sync_root', '').strip():
        st.error('Set the sync root directory in Settings before saving.')
        st.stop()
    s_movies, s_shows, s_playlists = _get_saved()
    selections = {
        'movies':    sorted(s_movies),
        'shows':     s_shows,
        'playlists': sorted(s_playlists),
    }
    save_slot_config(slot, selections)
    st.session_state['_dirty']       = False
    st.session_state['_loaded_slot'] = slot  # prevent switch_slot on post-save rerun
    return len(s_movies) + len(s_shows) + len(s_playlists)


def main():
    st.set_page_config(
        page_title=f'PlexSyncer {VERSION}',
        page_icon=APP_ICON,
        layout='wide',
        initial_sidebar_state='collapsed',
    )

    # Auto-connect once per session
    auto_connect()

    # Pending toasts
    for key in ('_toast_msg', '_startup_toast'):
        if key in st.session_state:
            icon, msg = st.session_state.pop(key)
            st.toast(msg, icon=icon)

    slots = list_slots()

    # ── Header ─────────────────────────────────────────────────────────────────
    c_logo, c_slots, c_actions = st.columns([2, 7, 2])

    with c_logo:
        plex  = get_browse_plex()
        admin = st.session_state.get('plex_admin')
        st.markdown(f"#### {APP_ICON} PlexSyncer")
        if plex:
            st.caption(f"🟢 {admin.friendlyName}")
        else:
            st.caption("🔴 Not connected")
        _opt_st = _read_optimize_status(load_plex_config().get('sync_root', ''))
        if _opt_st and _opt_st.get('state') == 'running':
            _done = _opt_st.get('transcoded', 0)
            _item = _opt_st.get('current_item', '…')
            st.caption(f'⚡ {_item} ({_done} done)')

    with c_slots:
        if not slots:
            st.caption("No slots yet — open ⚙ Settings to create one.")
            current_slot = None
        else:
            # Use segmented_control (1.40+) for the cleanest tab look,
            # fall back to horizontal radio on older versions.
            if hasattr(st, 'segmented_control'):
                current_slot = st.segmented_control(
                    "Active slot", slots,
                    default=slots[0],
                    key='slot_ctrl',
                    label_visibility='collapsed',
                )
                if current_slot is None:
                    current_slot = slots[0]
            else:
                current_slot = st.radio(
                    "Active slot", slots,
                    horizontal=True,
                    key='slot_radio',
                    label_visibility='collapsed',
                )

    with c_actions:
        dirty = st.session_state.get('_dirty', False)
        b1, b2, b3, b4 = st.columns(4)

        if b1.button("⚙", help="Settings", use_container_width=True):
            show_settings()

        if current_slot:
            save_icon = "💾●" if dirty else "💾"
            if b2.button(save_icon, help="Save", use_container_width=True):
                n = _do_save(current_slot)
                st.toast(f'Saved — {n} items', icon='💾')
                st.rerun()

            if b3.button("⚡", help="Optimize (transcode incompatible codecs)",
                         use_container_width=True):
                st.session_state['_pending_optimize'] = current_slot
                st.rerun()

            if b4.button("▶", help="Save & Sync", type="primary",
                         use_container_width=True):
                if not _check_lock_and_warn():
                    _do_save(current_slot)
                    st.session_state['_pending_sync'] = current_slot
                    st.rerun()

    st.divider()

    if not current_slot:
        return

    # Slot switch — only reload from disk when actually switching slots,
    # never when dirty (would wipe unsaved changes on every rerun).
    if st.session_state.get('_loaded_slot') != current_slot:
        if st.session_state.get('_dirty') and \
                st.session_state.get('_loaded_slot') is not None:
            # Slot changed while dirty — save first, then switch
            _do_save(st.session_state['_loaded_slot'])
        switch_slot(current_slot)

    # Optimize view
    if st.session_state.get('_pending_optimize') == current_slot:
        del st.session_state['_pending_optimize']
        st.session_state['_show_optimize'] = True

    if (st.session_state.get('_show_optimize')
            or '_opt_output' in st.session_state
            or '_opt_dry_output' in st.session_state
            or st.session_state.get('_pending_optimize_real') == current_slot):
        run_optimize_live(current_slot)
        st.caption(f'PlexSyncer {VERSION}')
        return

    # Sync view
    if st.session_state.get('_pending_sync') == current_slot:
        del st.session_state['_pending_sync']
        st.session_state['_show_sync'] = True

    if st.session_state.get('_show_sync') or '_sync_output' in st.session_state:
        run_sync_live(current_slot)
        st.caption(f'PlexSyncer {VERSION}')
        return

    # ── Main two-column layout ─────────────────────────────────────────────────
    col_cart, col_browser = st.columns([1, 2.5])

    with col_cart:
        render_cart(current_slot)

    with col_browser:
        render_browser(current_slot)

    st.divider()
    st.caption(f'PlexSyncer {VERSION}')


if __name__ == '__main__':
    main()
