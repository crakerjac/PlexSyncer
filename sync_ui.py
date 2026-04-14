"""
sync_ui.py  —  PlexSyncer Management UI
https://github.com/your-repo/plexsyncer

Run:
    pip install streamlit plexapi requests
    streamlit run sync_ui.py
"""

import os, json, glob, subprocess, sys
from typing import Optional
import streamlit as st

VERSION = 'v0.1.18'
APP_ICON = '📼'

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIGS_DIR = os.path.join(SCRIPT_DIR, 'configs')
PLEX_CONFIG = os.path.join(CONFIGS_DIR, 'plex.json')
WORKER      = os.path.join(SCRIPT_DIR, 'plex_hardlink_sync.py')
os.makedirs(CONFIGS_DIR, exist_ok=True)

DEFAULT_SYNC_ROOT = '/media/drive/PlexSync'
PAGE_SIZE = 50

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
# CONFIG I/O
# ══════════════════════════════════════════════════════════════════════════════

def load_plex_config() -> dict:
    defaults = {
        'host':                 'http://192.168.1.100:32400',
        'token':                '',
        'managed_user':         '',
        'sync_root':            DEFAULT_SYNC_ROOT,
        'subtitle_languages':   ['en'],
        'subtitle_forced_only': False,
    }
    if os.path.exists(PLEX_CONFIG):
        with open(PLEX_CONFIG, encoding='utf-8') as f:
            defaults.update(json.load(f))
    return defaults

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
    managed = load_plex_config().get('managed_user', '').strip()
    if managed:
        try:
            st.session_state['plex_browse'] = admin.switchUser(managed)
            return
        except Exception:
            pass
    st.session_state['plex_browse'] = admin

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

def get_section_items(section_key) -> list:
    cache_key = f'section_items_{section_key}'
    if cache_key in st.session_state:
        return st.session_state[cache_key]
    plex = get_browse_plex()
    if plex is None:
        return []
    section = plex.library.sectionByID(section_key)
    if section.type == 'show':
        raw = section.searchShows()
        items = sorted(
            [{'title':          i.title,
              'year':           getattr(i, 'year', None),
              'ratingKey':      str(i.ratingKey),
              'unwatchedCount': getattr(i, 'unwatchedLeafCount', None)}
             for i in raw],
            key=lambda x: x['title'].lower()
        )
    else:
        raw = section.all()
        items = sorted(
            [{'title': i.title, 'year': getattr(i, 'year', None),
              'ratingKey': str(i.ratingKey)}
             for i in raw],
            key=lambda x: x['title'].lower()
        )
    st.session_state[cache_key] = items
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
        if key.startswith('section_items_') or key in ('plex_sections', 'plex_playlists'):
            del st.session_state[key]


# ══════════════════════════════════════════════════════════════════════════════
# SELECTION STATE
#
# _saved_movies    : set of titles  — canonical, survives widget key deletion
# _saved_shows     : dict title→mode_cfg
# _saved_playlists : set of titles
# _dirty           : bool — True when unsaved changes exist
#
# KEY INVARIANT: on_change callbacks update _saved_* IMMEDIATELY whenever any
# checkbox or selectbox changes. This means even if Streamlit deletes the
# widget key (e.g. when paginating away from a page), _saved_* already has
# the correct value. build_selections_from_widgets() can safely fall back to
# _saved_* for any item whose key is absent.
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
    checked          = st.session_state.get(f'chk_pl_{slot}_{rk}', False)
    saved_playlists  = st.session_state.get('_saved_playlists', set())
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
    even for off-screen paginated items.
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


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

def render_sidebar() -> Optional[str]:
    with st.sidebar:
        st.title(f'{APP_ICON} PlexSyncer')
        st.divider()

        # ── 1. Plex connection ────────────────────────────────────────────────
        st.subheader('Plex Connection')
        plex_cfg = load_plex_config()
        host  = st.text_input('Host',  value=plex_cfg['host'],  key='cfg_host')
        token = st.text_input('Token', value=plex_cfg['token'], key='cfg_token',
                              type='password')
        c1, c2 = st.columns(2)
        with c1:
            if st.button('Connect', use_container_width=True):
                with st.spinner('Connecting...'):
                    ok, msg = try_connect(host, token)
                if ok:
                    st.toast(f'✓ Connected to {msg}', icon='✅')
                else:
                    st.error(f'✗ {msg}')
        with c2:
            if st.button('Refresh', use_container_width=True):
                _invalidate_library_cache()
                st.rerun()

        browse = get_browse_plex()
        admin  = st.session_state.get('plex_admin')
        if browse:
            st.caption(f'✓ Connected: **{admin.friendlyName}**')
            home_users = get_home_users()
            if home_users:
                options  = ['(main account)'] + home_users
                saved    = plex_cfg.get('managed_user', '')
                def_i    = options.index(saved) if saved in options else 0
                st.selectbox('Browse as user', options, index=def_i,
                             key='cfg_managed_user')
            else:
                st.session_state['cfg_managed_user'] = '(main account)'
                st.caption('No Plex Home managed users found.')
        else:
            st.caption('⚠ Not connected')
            st.session_state['cfg_managed_user'] = '(main account)'

        st.divider()

        # ── 2. Slots ──────────────────────────────────────────────────────────
        st.subheader('Slots')
        slots = list_slots()
        if slots:
            current_slot = st.selectbox('Active slot', slots,
                                        key='active_slot_select')
        else:
            current_slot = None

        _cv             = st.session_state.get('_slot_input_v', 0)
        create_expanded = st.session_state.get('_create_expanded', not bool(slots))
        with st.expander('➕ New slot', expanded=create_expanded):
            new_name = st.text_input('Slot name', key=f'new_slot_name_{_cv}',
                                     placeholder='e.g. OnePlus13r')
            if st.button('Create slot', key='btn_create_slot',
                         use_container_width=True):
                name = new_name.strip()
                if not name:
                    st.warning('Enter a slot name.')
                elif name in slots:
                    st.warning(f'"{name}" already exists.')
                else:
                    save_slot_config(name, {'playlists': [], 'movies': [], 'shows': {}})
                    st.session_state['_slot_input_v']    = _cv + 1
                    st.session_state['_create_expanded'] = False
                    st.session_state['_toast_msg']       = ('✅', f'Created slot "{name}"')
                    st.rerun()

        if current_slot:
            with st.expander('🗑 Delete slot'):
                st.warning(f'Delete **{current_slot}**? Config only — no files deleted.')
                if st.button('Confirm delete', key='btn_delete_slot',
                             use_container_width=True, type='primary'):
                    deleted = current_slot
                    os.remove(os.path.join(CONFIGS_DIR, f'{current_slot}.json'))
                    st.session_state.pop('_loaded_slot', None)
                    st.session_state['_toast_msg'] = ('🗑', f'Deleted slot "{deleted}"')
                    st.rerun()

        st.divider()

        # ── 3. Global settings ────────────────────────────────────────────────
        st.subheader('Global Settings')
        sync_root = st.text_input(
            'Sync root directory', value=plex_cfg['sync_root'],
            key='cfg_sync_root', placeholder=DEFAULT_SYNC_ROOT)
        st.caption('Subtitle languages (comma-separated codes, or "all")')
        sub_lang_str = st.text_input(
            'Subtitle languages', value=', '.join(plex_cfg['subtitle_languages']),
            key='cfg_sub_langs', label_visibility='collapsed',
            placeholder='en, es  or  all')
        sub_forced = st.checkbox(
            'Forced subtitles only', value=plex_cfg['subtitle_forced_only'],
            key='cfg_sub_forced')
        if st.button('Save global settings', use_container_width=True):
            langs   = [l.strip() for l in sub_lang_str.split(',') if l.strip()] or ['all']
            managed = st.session_state.get('cfg_managed_user', '(main account)')
            save_plex_config({
                'host':                 host.strip(),
                'token':                token.strip(),
                'managed_user':         '' if managed == '(main account)' else managed,
                'sync_root':            sync_root.strip(),
                'subtitle_languages':   langs,
                'subtitle_forced_only': sub_forced,
            })
            _apply_managed_user()
            _invalidate_library_cache()
            st.toast('Global settings saved ✓', icon='✅')

    return current_slot


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY PANEL
# ══════════════════════════════════════════════════════════════════════════════

def render_summary(slot: str) -> None:
    """
    Reads _saved_* directly — always current because on_change callbacks
    update it on every interaction.
    """
    s_movies, s_shows, s_playlists = _get_saved()
    total = len(s_movies) + len(s_shows) + len(s_playlists)

    with st.expander(f'📋 Current sync list  ({total} items)', expanded=True):
        if total == 0:
            st.caption('Nothing selected yet. Use the library tabs below.')
            return
        col1, col2, col3 = st.columns(3)
        with col1:
            st.caption(f'**Movies** ({len(s_movies)})')
            for t in sorted(s_movies):
                st.caption(f'• {t}')
        with col2:
            st.caption(f'**TV Shows** ({len(s_shows)})')
            for t, cfg in sorted(s_shows.items()):
                st.caption(f'• {t}  _{mode_cfg_to_label(cfg)}_')
        with col3:
            st.caption(f'**Playlists** ({len(s_playlists)})')
            for t in sorted(s_playlists):
                st.caption(f'• {t}')


# ══════════════════════════════════════════════════════════════════════════════
# LIBRARY TABS
# ══════════════════════════════════════════════════════════════════════════════

def _pagination_controls(items: list, page_key: str) -> tuple:
    """Render pagination controls. Returns (page_items, n_pages, page)."""
    n_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    cur     = min(int(st.session_state.get(page_key, 1)), n_pages)
    page    = st.number_input(
        f'Page', min_value=1, max_value=n_pages, value=cur,
        key=page_key, step=1, label_visibility='collapsed')
    start = min((int(page) - 1) * PAGE_SIZE, max(0, len(items) - 1))
    return items[start:start + PAGE_SIZE], n_pages, int(page)

def _page_label(n_pages: int, page: int) -> str:
    return f'Page {page} of {n_pages}  ·  {PAGE_SIZE} per page'

def _sel_count_saved(saved_set_or_dict) -> int:
    """Count selected items from _saved_* (works for off-screen paginated items)."""
    return len(saved_set_or_dict)


def render_movie_tab(section: dict, slot: str) -> None:
    items = get_section_items(section['key'])
    if not items:
        st.info('No movies found.' if get_browse_plex() else 'Connect to Plex first.')
        return

    search   = st.text_input('🔍 Filter', key=f'search_mov_{section["key"]}',
                              placeholder='Type to filter...')
    filtered = [i for i in items if not search or search.lower() in i['title'].lower()]
    s_movies, _, _ = _get_saved()
    n_sel    = _sel_count_saved(s_movies)

    c_actions, c_info = st.columns([1, 4])
    with c_actions:
        with st.popover("☑️ Bulk Actions", use_container_width=True):
            if st.button('Select All', key=f'mov_all_{slot}_{section["key"]}', use_container_width=True):
                for i in items:
                    st.session_state[f'chk_mov_{slot}_{i["ratingKey"]}'] = True
                st.session_state['_saved_movies'] = {i['title'] for i in items}
                st.session_state['_dirty']        = True
                st.rerun()
            if st.button('Select None', key=f'mov_none_{slot}_{section["key"]}', use_container_width=True):
                for i in items:
                    st.session_state[f'chk_mov_{slot}_{i["ratingKey"]}'] = False
                st.session_state['_saved_movies'] = set()
                st.session_state['_dirty']        = True
                st.rerun()
            if st.button('Invert Selection', key=f'mov_inv_{slot}_{section["key"]}', use_container_width=True):
                for i in items:
                    k = f'chk_mov_{slot}_{i["ratingKey"]}'
                    st.session_state[k] = not st.session_state.get(k, False)
                st.session_state['_saved_movies'] = {
                    i['title'] for i in items
                    if st.session_state.get(f'chk_mov_{slot}_{i["ratingKey"]}', False)}
                st.session_state['_dirty'] = True
                st.rerun()
                
    c_info.caption(f'{n_sel} selected · {len(filtered)} shown · {len(items)} total')

    page_items, n_pages, page = _pagination_controls(filtered, f'page_mov_{section["key"]}')
    if n_pages > 1:
        st.caption(_page_label(n_pages, page))

    s_movies_cur, _, _ = _get_saved()
    cols = st.columns(3)
    for idx, item in enumerate(page_items):
        rk, title = item['ratingKey'], item['title']
        label = f"{title} ({item['year']})" if item['year'] else title
        # value= initialises the key when absent (navigation, tab switch, etc.)
        # When key exists (user on same page), Streamlit ignores value= and uses ss[key]
        cols[idx % 3].checkbox(label, key=f'chk_mov_{slot}_{rk}',
                               value=title in s_movies_cur,
                               on_change=_on_movie_change, args=(rk, title, slot))

    if n_pages > 1:
        st.caption(_page_label(n_pages, page))


def render_show_tab(section: dict, slot: str) -> None:
    items = get_section_items(section['key'])
    if not items:
        st.info('No shows found.' if get_browse_plex() else 'Connect to Plex first.')
        return

    search   = st.text_input('🔍 Filter', key=f'search_show_{section["key"]}',
                              placeholder='Type to filter...')
    filtered = [i for i in items if not search or search.lower() in i['title'].lower()]
    _, s_shows_all, _ = _get_saved()
    n_sel    = _sel_count_saved(s_shows_all)

    c_actions, c_info = st.columns([1, 4])
    with c_actions:
        with st.popover("☑️ Bulk Actions", use_container_width=True):
            if st.button('Select All', key=f'show_all_{slot}_{section["key"]}', use_container_width=True):
                for i in items:
                    st.session_state[f'chk_show_{slot}_{i["ratingKey"]}'] = True
                st.session_state['_saved_shows'] = {
                    i['title']: label_to_mode_cfg(
                        st.session_state.get(f'mode_show_{slot}_{i["ratingKey"]}', 'Next unwatched'))
                    for i in items}
                st.session_state['_dirty'] = True
                st.rerun()
            if st.button('Select None', key=f'show_none_{slot}_{section["key"]}', use_container_width=True):
                for i in items:
                    st.session_state[f'chk_show_{slot}_{i["ratingKey"]}'] = False
                st.session_state['_saved_shows'] = {}
                st.session_state['_dirty']       = True
                st.rerun()
            if st.button('Invert Selection', key=f'show_inv_{slot}_{section["key"]}', use_container_width=True):
                for i in items:
                    k = f'chk_show_{slot}_{i["ratingKey"]}'
                    st.session_state[k] = not st.session_state.get(k, False)
                st.session_state['_saved_shows'] = {
                    i['title']: label_to_mode_cfg(
                        st.session_state.get(f'mode_show_{slot}_{i["ratingKey"]}', 'Next unwatched'))
                    for i in items
                    if st.session_state.get(f'chk_show_{slot}_{i["ratingKey"]}', False)}
                st.session_state['_dirty'] = True
                st.rerun()

    c_info.caption(f'{n_sel} selected · {len(filtered)} shown · {len(items)} total')

    page_items, n_pages, page = _pagination_controls(filtered, f'page_show_{section["key"]}')
    if n_pages > 1:
        st.caption(_page_label(n_pages, page))

    h1, h2 = st.columns([3, 2])
    h1.caption('**Show**')
    h2.caption('**Sync mode**')

    for item in page_items:
        rk, title = item['ratingKey'], item['title']
        _, saved_shows, _ = _get_saved()

        # Build label with unwatched badge
        unwatched = item.get('unwatchedCount')
        base_label = f"{title} ({item['year']})" if item['year'] else title
        if unwatched is not None and unwatched > 0:
            badge_label = f"{base_label}  ·  {unwatched} unwatched"
        else:
            badge_label = base_label

        c1, c2  = st.columns([3, 2])
        # value= initialises the key when absent; ignored when key exists
        new_chk = c1.checkbox(badge_label, key=f'chk_show_{slot}_{rk}',
                              value=title in saved_shows,
                              on_change=_on_show_change, args=(rk, title, slot))
        if new_chk:
            c2.selectbox('##', SYNC_MODE_LABELS,
                         key=f'mode_show_{slot}_{rk}',
                         index=SYNC_MODE_LABELS.index(
                             mode_cfg_to_label(saved_shows.get(title, {}))
                         ),
                         label_visibility='collapsed',
                         on_change=_on_mode_change, args=(rk, title, slot))
        else:
            c2.caption('—')

    if n_pages > 1:
        st.caption(_page_label(n_pages, page))


def render_playlist_tab(slot: str) -> None:
    playlists = get_playlists()
    if not playlists:
        st.info('No video playlists found.' if get_browse_plex() else 'Connect to Plex first.')
        return

    search   = st.text_input('🔍 Filter', key='search_pl',
                              placeholder='Type to filter...')
    filtered = [p for p in playlists
                if not search or search.lower() in p['title'].lower()]
    _, _, s_playlists_all = _get_saved()
    n_sel    = _sel_count_saved(s_playlists_all)

    c_actions, c_info = st.columns([1, 4])
    with c_actions:
        with st.popover("☑️ Bulk Actions", use_container_width=True):
            if st.button('Select All', key=f'pl_all_{slot}', use_container_width=True):
                for p in playlists:
                    st.session_state[f'chk_pl_{slot}_{p["ratingKey"]}'] = True
                st.session_state['_saved_playlists'] = {p['title'] for p in playlists}
                st.session_state['_dirty']           = True
                st.rerun()
            if st.button('Select None', key=f'pl_none_{slot}', use_container_width=True):
                for p in playlists:
                    st.session_state[f'chk_pl_{slot}_{p["ratingKey"]}'] = False
                st.session_state['_saved_playlists'] = set()
                st.session_state['_dirty']           = True
                st.rerun()
            if st.button('Invert Selection', key=f'pl_inv_{slot}', use_container_width=True):
                for p in playlists:
                    k = f'chk_pl_{slot}_{p["ratingKey"]}'
                    st.session_state[k] = not st.session_state.get(k, False)
                st.session_state['_saved_playlists'] = {
                    p['title'] for p in playlists
                    if st.session_state.get(f'chk_pl_{slot}_{p["ratingKey"]}', False)}
                st.session_state['_dirty'] = True
                st.rerun()

    c_info.caption(f'{n_sel} selected · {len(playlists)} total')

    _, _, s_playlists_cur = _get_saved()
    for pl in filtered:
        rk, title = pl['ratingKey'], pl['title']
        # value= initialises the key when absent; ignored when key exists
        st.checkbox(f"{title}  ({pl['leafCount']} items)",
                    key=f'chk_pl_{slot}_{rk}',
                    value=title in s_playlists_cur,
                    on_change=_on_playlist_change, args=(rk, title, slot))


# ══════════════════════════════════════════════════════════════════════════════
# SYNC OUTPUT  (two-phase: phase 1 streams + stores; phase 2 displays)
# Uses st.status for a cleaner collapsible output container.
# ══════════════════════════════════════════════════════════════════════════════

def run_sync_live(slot_name: str) -> None:
    # Phase 2: output stored — display with st.status
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

    # Phase 1: run subprocess, stream into st.status
    with st.status(f'⏳ Syncing {slot_name}...', expanded=True) as status:
        lines: list = []
        output = st.empty()
        proc = subprocess.Popen(
            [sys.executable, WORKER, '--slot', slot_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=SCRIPT_DIR,
        )
        for raw in proc.stdout:
            lines.append(raw.rstrip())
            output.code('\n'.join(lines), language=None)
        proc.wait()
        if proc.returncode == 0:
            status.update(label='✅ Sync complete', state='complete', expanded=True)
        else:
            status.update(label=f'❌ Sync failed (exit {proc.returncode})',
                          state='error', expanded=True)

    st.session_state['_sync_output'] = lines
    st.session_state['_sync_rc']     = proc.returncode
    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# FOOTER
# ══════════════════════════════════════════════════════════════════════════════

def _footer() -> None:
    st.divider()
    st.caption(f'PlexSyncer {VERSION}')


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    st.set_page_config(
        page_title=f'PlexSyncer {VERSION}', page_icon=APP_ICON,
        layout='wide', initial_sidebar_state='expanded',
    )

    if '_toast_msg' in st.session_state:
        icon, msg = st.session_state.pop('_toast_msg')
        st.toast(msg, icon=icon)

    current_slot = render_sidebar()

    if not current_slot:
        st.title(f'{APP_ICON} PlexSyncer')
        st.info('👈 Create a slot in the sidebar to get started.')
        _footer()
        return

    # Switch slot on first load, genuine slot change, or return from sync
    if st.session_state.get('_loaded_slot') != current_slot:
        switch_slot(current_slot)

    # Sync view
    if st.session_state.get('_pending_sync') == current_slot:
        del st.session_state['_pending_sync']
        st.session_state['_show_sync'] = True

    if st.session_state.get('_show_sync') or '_sync_output' in st.session_state:
        st.title(f'{APP_ICON} PlexSyncer  —  {current_slot}')
        run_sync_live(current_slot)
        _footer()
        return

    # Config view
    plex_cfg  = load_plex_config()
    sync_root = plex_cfg.get('sync_root', DEFAULT_SYNC_ROOT)
    slot_dir  = os.path.join(sync_root, current_slot)

    st.title(f'{APP_ICON} PlexSyncer  —  {current_slot}')
    st.caption(f'Sync directory: `{slot_dir}`')

    managed = plex_cfg.get('managed_user', '')
    if managed:
        st.caption(f'Browsing as managed user: **{managed}**')

    # Unsaved changes indicator + action buttons
    dirty = st.session_state.get('_dirty', False)
    c_save, c_sync, c_warn = st.columns([1, 1, 5])
    save_clicked = c_save.button('💾 Save',       use_container_width=True)
    sync_clicked = c_sync.button('▶ Save & Sync', use_container_width=True,
                                 type='primary')
                                 
    # Fix: Use an empty container so we can clear the warning after saving in the same run
    warn_placeholder = c_warn.empty()
    if dirty:
        warn_placeholder.warning('⚠ Unsaved changes', icon=None)

    if save_clicked or sync_clicked:
        if not sync_root.strip():
            st.error('Set the sync root directory in Global Settings first.')
            st.stop()
        selections = build_selections_from_widgets(current_slot)
        save_slot_config(current_slot, selections)
        st.session_state['_saved_movies']    = set(selections['movies'])
        st.session_state['_saved_playlists'] = set(selections['playlists'])
        st.session_state['_saved_shows']     = selections['shows']
        st.session_state['_dirty']           = False
        
        warn_placeholder.empty()  # Instantly clear the warning message
        
        if save_clicked:
            n = len(selections['movies']) + len(selections['shows']) + len(selections['playlists'])
            st.toast(f'Config saved — {n} items', icon='💾')
        if sync_clicked:
            st.session_state['_pending_sync'] = current_slot
            st.rerun()

    st.divider()
    render_summary(current_slot)
    st.divider()

    plex = get_browse_plex()
    if not plex:
        st.info('👈 Connect to Plex in the sidebar to browse your libraries.')
        _footer()
        return

    sections   = get_sections()
    tab_labels = [s['title'] for s in sections] + ['Playlists']
    if not sections:
        st.warning('No movie or TV show libraries found on this server.')
        _footer()
        return

    tabs = st.tabs(tab_labels)
    for i, section in enumerate(sections):
        with tabs[i]:
            if section['type'] == 'movie':
                render_movie_tab(section, current_slot)
            elif section['type'] == 'show':
                render_show_tab(section, current_slot)
    with tabs[-1]:
        render_playlist_tab(current_slot)

    _footer()


if __name__ == '__main__':
    main()
