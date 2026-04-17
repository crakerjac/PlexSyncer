"""
sync_ui.py  —  PlexSyncer Management UI  (cart + search layout)
Drop-in replacement. Same config files, same worker invocation.

pip install "streamlit>=1.34" plexapi
streamlit run sync_ui.py
"""

import os, json, glob, subprocess, sys
from typing import Optional
import streamlit as st

VERSION     = 'v1.0.0'
APP_ICON    = '📼'
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIGS_DIR = os.path.join(SCRIPT_DIR, 'configs')
PLEX_CONFIG = os.path.join(CONFIGS_DIR, 'plex.json')
WORKER      = os.path.join(SCRIPT_DIR, 'plex_hardlink_sync.py')
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
# CONFIG I/O  —  unchanged format, backward compatible
# ══════════════════════════════════════════════════════════════════════════════

def load_plex_config() -> dict:
    """Read plex.json from disk. Use get_plex_config() during rendering."""
    defaults = {
        'host':                 'http://localhost:32400',
        'token':                '',
        'managed_user':         '',
        'sync_root':            '',
        'subtitle_languages':   ['en'],
        'subtitle_forced_only': False,
        'hidden_libraries':     [],
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

def get_section_items(section_key) -> list:
    cache_key = f'section_items_{section_key}'
    if cache_key in st.session_state:
        return st.session_state[cache_key]
    plex = get_browse_plex()
    if plex is None:
        return []
    section = plex.library.sectionByID(section_key)
    if section.type == 'show':
        raw   = section.searchShows()
        items = sorted(
            [{'title':          i.title,
              'year':           getattr(i, 'year', None),
              'ratingKey':      str(i.ratingKey),
              'unwatchedCount': getattr(i, 'unwatchedLeafCount', None)}
             for i in raw],
            key=lambda x: x['title'].lower()
        )
    else:
        raw   = section.all()
        items = sorted(
            [{'title':     i.title,
              'year':      getattr(i, 'year', None),
              'ratingKey': str(i.ratingKey)}
             for i in raw],
            key=lambda x: x['title'].lower()
        )
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
    """Returns {(title, item_type): ratingKey} for all cached section items."""
    if '_rk_index' in st.session_state:
        return st.session_state['_rk_index']
    idx = {}
    for section in get_sections():
        for item in st.session_state.get(f'section_items_{section["key"]}', []):
            idx[(item['title'], section['type'])] = item['ratingKey']
    st.session_state['_rk_index'] = idx
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
            'host':                 new_host.strip(),
            'token':                new_token or plex_cfg.get('token', ''),
            'managed_user':         '' if new_managed == '(main account)' else new_managed,
            'sync_root':            new_sync_root.strip(),
            'subtitle_languages':   langs,
            'subtitle_forced_only': new_sub_forced,
            'hidden_libraries':     new_hidden,
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
    with st.spinner(f"Loading {section['title']}…"):
        items = get_section_items(section['key'])

    if not items:
        st.info(f"No items found in {section['title']}.")
        return

    s_movies, s_shows, _ = _get_saved()

    search   = st.text_input(
        "Filter", placeholder=f"Filter {section['title']}…",
        key=f'search_{section["key"]}', label_visibility='collapsed'
    )
    filtered = [i for i in items if not search or search.lower() in i['title'].lower()]
    st.caption(f"{len(filtered)} of {len(items)}")

    if section['type'] == 'movie':
        cols = st.columns(3)
        for idx, item in enumerate(filtered):
            rk, title = item['ratingKey'], item['title']
            label     = f"{title} ({item['year']})" if item['year'] else title
            cols[idx % 3].checkbox(
                label,
                key=f'chk_mov_{slot}_{rk}',
                value=title in s_movies,
                on_change=_on_movie_change, args=(rk, title, slot)
            )

    elif section['type'] == 'show':
        h1, h2 = st.columns([3, 2])
        h1.caption("**Show**")
        h2.caption("**Sync mode**")
        for item in filtered:
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
                    "##", SYNC_MODE_LABELS,
                    key=f'mode_show_{slot}_{rk}',
                    index=SYNC_MODE_LABELS.index(
                        mode_cfg_to_label(s_shows.get(title, {}))
                    ),
                    label_visibility='collapsed',
                    on_change=_on_mode_change, args=(rk, title, slot)
                )
            else:
                c2.caption("—")


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

    # Phase 1: stream output
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


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def _do_save(slot: str) -> int:
    """Save current widget state to disk. Returns item count."""
    plex_cfg = get_plex_config()
    if not plex_cfg.get('sync_root', '').strip():
        st.error('Set the sync root directory in Settings before saving.')
        st.stop()
    selections = build_selections_from_widgets(slot)
    save_slot_config(slot, selections)
    st.session_state['_saved_movies']    = set(selections['movies'])
    st.session_state['_saved_playlists'] = set(selections['playlists'])
    st.session_state['_saved_shows']     = selections['shows']
    st.session_state['_dirty']           = False
    return len(selections['movies']) + len(selections['shows']) + len(selections['playlists'])


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
        b1, b2, b3 = st.columns(3)

        if b1.button("⚙", help="Settings", use_container_width=True):
            show_settings()

        if current_slot:
            save_icon = "💾●" if dirty else "💾"
            if b2.button(save_icon, help="Save", use_container_width=True):
                n = _do_save(current_slot)
                st.toast(f'Saved — {n} items', icon='💾')
                st.rerun()

            if b3.button("▶", help="Save & Sync", type="primary",
                         use_container_width=True):
                _do_save(current_slot)
                st.session_state['_pending_sync'] = current_slot
                st.rerun()

    st.divider()

    if not current_slot:
        return

    # Slot switch
    if st.session_state.get('_loaded_slot') != current_slot:
        switch_slot(current_slot)

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
