#!/usr/bin/env python3
"""
My Magnet Links - a tiny addon for Nuvio (Stremio-compatible).

Paste magnet links on its web page and they appear inside Nuvio as a
catalog called "My Magnet Links". Pick one and it plays as a P2P
torrent stream. You can also paste a magnet link straight into Nuvio's
search bar - it shows up as an item and this addon gives it a Play
stream the same way.

You can also paste direct web video links (http/https, e.g. an .mp4,
.mkv or .m3u8 address). They appear in the same catalog and play
straight from the web - no torrent, no VPN needed. The link has to be
the video file/stream address itself, not a web page.

You can also paste YouTube links (regular videos and live streams).
They appear in the same catalog too. When you press play, the addon
asks yt-dlp (which must be installed on this Mac) for the stream
address, so the first play can take a few seconds. A YouTube quality
setting on the web page caps how high the picture can go. Every
YouTube item plays through the addon itself - the stream list shows
a single relay entry per item, no direct links: for regular videos
the addon joins YouTube's separate picture and sound streams
together with ffmpeg as it plays (starts slower, seeking is
limited). Live streams list only a relay stream: YouTube's live
playlists only hand out a short window of video at a time and expect
the player to keep re-asking for more, which some players don't keep
up with - playback stops when the window runs out. So for live items
the addon pulls the feed itself with ffmpeg and hands Nuvio one
continuous stream instead.

You can also add whole YouTube channels. A channel's newest uploads
(up to ten) appear in the same catalog on their own: the addon
re-checks each channel about every half hour while it runs, so new
uploads show up by themselves without you pasting anything else.
Each one is labeled in the list with its channel's name -
"Channel name — video title" - so you can tell where it came from.
Channel videos play exactly like individually pasted YouTube videos.

Any saved item can also be deleted from inside Nuvio itself:
open the item and pick the Delete entry at the bottom of its
stream list. (The stream protocol has no buttons or menus, so
deleting rides in as one more "stream"; picking it removes the
item and plays a one-second blank clip.)

YouTube fetches use a browser cookies file when one is around - the
addon finds it by itself in Downloads, including the file Opera's
cookies extension saves in Downloads/Temp - and if the exported
cookies have gone stale it retries without them rather than failing.

One paste box on the web page takes any of the three link kinds and
sorts out which it is. YouTube items show their video's picture in
Nuvio, and a YouTube video added without a name gets its real title
filled in automatically. Repeat plays are fast: resolved stream
addresses are remembered in memory for a few hours (live stream
addresses only briefly, since they rotate).

No dependencies - Python 3 standard library only.
Run it on the same Mac as Nuvio:   python3 magnet_addon.py
Then add  http://127.0.0.1:7777/manifest.json  in Nuvio (Addons ->
Install by URL). Keep this window open while you watch.
Magnet playback is P2P torrent streaming - have your VPN on.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote, urljoin

PORT = int(os.environ.get("PORT", "7777"))
UPDATE_URL = os.environ.get(
    "MAGNET_UPDATE_URL",
    "https://raw.githubusercontent.com/mmwriver/nuvio-magnet-addon-/main/magnet_addon.py",
)
HERE = os.path.dirname(os.path.abspath(__file__))
_home = os.environ.get("MAGNET_ADDON_HOME")
DATA_DIR = _home if _home else HERE
if _home:
    os.makedirs(DATA_DIR, exist_ok=True)
STORE = os.path.join(DATA_DIR, "magnets.json")
LINK_STORE = os.path.join(DATA_DIR, "links.json")
YT_STORE = os.path.join(DATA_DIR, "youtube.json")
CHANNEL_STORE = os.path.join(DATA_DIR, "channels.json")
SETTINGS_STORE = os.path.join(DATA_DIR, "settings.json")

QUALITY_VALUES = ("1080", "720", "480", "360", "best")
DEFAULT_QUALITY = "720"
QUALITY_LABELS = (
    ("best", "Best available"),
    ("1080", "1080p"),
    ("720", "720p"),
    ("480", "480p"),
    ("360", "360p"),
)
LOCK = threading.Lock()

# In-memory stream-address cache (process memory only - nothing is
# written to disk). Resolved YouTube stream URLs are signed but stay
# valid for hours, so repeat plays skip the yt-dlp call entirely. HLS
# manifest URLs for live streams rotate much faster, so those get a
# short TTL. The /play/ merge pair (separate picture + sound URLs) is
# cached the same way; a pair is only ever cached for a non-live item,
# so a cache hit also settles the live check.
RESOLVE_TTL = 4 * 3600
LIVE_TTL = 60
PAIR_TTL = 4 * 3600
CACHE_LOCK = threading.Lock()
_resolve_cache = {}  # (video_id, quality) -> (url, timestamp)
_pair_cache = {}  # (video_id, cap) -> (video_url, audio_url, timestamp)

# Channel uploads cache: a channel's newest uploads are re-fetched at
# most every CHANNEL_TTL - often enough that new uploads appear on
# their own, rare enough that opening the catalog stays fast.
CHANNEL_TTL = 30 * 60
CHANNEL_LIMIT = 10
_channel_cache = {}  # channel item id -> ([(video_id, title)], timestamp)


def _is_hls_url(url):
    u = (url or "").lower()
    return ".m3u8" in u or "manifest" in u


def cache_resolve_get(video_id, quality):
    with CACHE_LOCK:
        hit = _resolve_cache.get((video_id, quality))
    if not hit:
        return None
    url, ts = hit
    ttl = LIVE_TTL if _is_hls_url(url) else RESOLVE_TTL
    if time.time() - ts < ttl:
        return url
    return None


def cache_resolve_put(video_id, quality, url):
    with CACHE_LOCK:
        _resolve_cache[(video_id, quality)] = (url, time.time())


def cache_pair_get(video_id, cap):
    with CACHE_LOCK:
        hit = _pair_cache.get((video_id, cap))
    if not hit:
        return None
    vurl, aurl, ts = hit
    if time.time() - ts < PAIR_TTL:
        return (vurl, aurl)
    return None


def cache_pair_put(video_id, cap, vurl, aurl):
    with CACHE_LOCK:
        _pair_cache[(video_id, cap)] = (vurl, aurl, time.time())

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
]

MANIFEST = {
    "id": "community.mymagnetlinks",
    "version": "1.6.5",
    "name": "My Magnet Links",
    "description": "Your own pasted magnet links, web video links, YouTube videos and channels, playable in Nuvio.",
    "logo": "",
    "resources": ["catalog", "meta", "stream"],
    "types": ["movie"],
    "idPrefixes": ["magnet:", "link:", "youtube:"],
    "catalogs": [
        {
            "type": "movie",
            "id": "mymagnets",
            "name": "My Magnet Links",
            "extra": [{"name": "search", "isRequired": False}],
        }
    ],
    "behaviorHints": {"configurable": True},
}


def load_store(path=STORE):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_store(items, path=STORE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)


def all_items():
    """Magnets, web links and YouTube items together, each list newest-first."""
    return load_store(STORE) + load_store(LINK_STORE) + load_store(YT_STORE)


def load_settings():
    """Settings dict; quality falls back to the default on anything bad."""
    try:
        with open(SETTINGS_STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("quality") in QUALITY_VALUES:
            return {"quality": data["quality"]}
    except Exception:
        pass
    return {"quality": DEFAULT_QUALITY}


def save_settings(settings):
    with open(SETTINGS_STORE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def parse_magnet(magnet):
    """Return a store item dict from a magnet URI, or None if invalid."""
    magnet = (magnet or "").strip()
    if not magnet.lower().startswith("magnet:?"):
        return None
    qs = parse_qs(urlparse(magnet).query)
    xt = (qs.get("xt") or [""])[0]
    m = re.search(r"btih:([a-zA-Z0-9]+)", xt)
    if not m:
        return None
    infohash = m.group(1).lower()
    name = (qs.get("dn") or [""])[0].strip() or ("Magnet " + infohash[:8])
    trackers = [t for t in (qs.get("tr") or []) if t]
    sources = ["tracker:" + t for t in (trackers or DEFAULT_TRACKERS)]
    return {
        "id": "magnet:" + infohash,
        "name": name,
        "magnet": magnet,
        "infoHash": infohash,
        "sources": sources,
    }


def parse_link(url, name=""):
    """Return a store item dict from a web video URL, or None if invalid."""
    url = (url or "").strip()
    low = url.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return None
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    name = (name or "").strip()
    if not name:
        seg = ""
        for part in parsed.path.split("/"):
            if part:
                seg = part
        seg = unquote(seg)
        if seg:
            root, dot, ext = seg.rpartition(".")
            if dot and root and len(ext) <= 5:
                seg = root
            name = seg
        else:
            name = parsed.netloc
    lid = "link:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return {
        "id": lid,
        "name": name,
        "url": url,
        "kind": "link",
    }


YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|live/|shorts/|embed/|v/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


def parse_youtube(url, name=""):
    """Return a store item dict from a YouTube URL, or None if invalid."""
    url = (url or "").strip()
    low = url.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return None
    m = YT_ID_RE.search(url)
    if not m:
        return None
    vid = m.group(1)
    name = (name or "").strip() or ("YouTube " + vid)
    return {
        "id": "youtube:" + vid,
        "name": name,
        "url": "https://www.youtube.com/watch?v=" + vid,
        "kind": "youtube",
    }


CHANNEL_RE = re.compile(
    r"youtube\.com/(?:@([A-Za-z0-9._-]+)|channel/(UC[A-Za-z0-9_-]{22})"
    r"|c/([A-Za-z0-9._-]+)|user/([A-Za-z0-9._-]+))",
    re.IGNORECASE,
)


def parse_channel(url, name=""):
    """Return a store item dict from a YouTube channel URL, or None.

    Accepts the channel page shapes: youtube.com/@handle (any tab
    suffix - /videos, /streams, /featured, ... - is fine),
    youtube.com/channel/UC..., youtube.com/c/<name> and
    youtube.com/user/<name>. Video URLs are NOT channel URLs: they
    return None here and stay individual YouTube items. The item id
    is provisional - the UC id when the URL itself carries it, else
    a hash of the canonical URL; resolve_channel() upgrades it with
    the real channel id and title when yt-dlp can be asked."""
    url = (url or "").strip()
    low = url.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return None
    if "youtu.be/" in low:
        return None
    m = CHANNEL_RE.search(url)
    if not m:
        return None
    handle, ucid, cname, uname = m.groups()
    if ucid:
        base = "https://www.youtube.com/channel/" + ucid
        fallback = ucid
        key = ucid
    elif handle:
        base = "https://www.youtube.com/@" + handle
        fallback = handle
        key = None
    elif cname:
        base = "https://www.youtube.com/c/" + cname
        fallback = cname
        key = None
    else:
        base = "https://www.youtube.com/user/" + uname
        fallback = uname
        key = None
    if not key:
        key = hashlib.sha1(base.encode("utf-8")).hexdigest()
    item = {
        "id": "channel:" + key,
        "name": (name or "").strip() or fallback,
        "url": base,
        "videos_url": base + "/videos",
        "kind": "channel",
    }
    if ucid:
        item["ucid"] = ucid
    return item


def save_channel(item):
    """Insert a channel item at the front of channels.json, replacing
    any existing entry for the same channel - matched by item id, by
    videos URL, or by resolved UC id, so adding a channel twice (once
    by handle, once by channel URL) never duplicates it."""
    def same(old):
        if old.get("id") == item["id"]:
            return True
        if old.get("videos_url") == item["videos_url"]:
            return True
        if item.get("ucid") and old.get("ucid") == item["ucid"]:
            return True
        return False

    with LOCK:
        items = [i for i in load_store(CHANNEL_STORE) if not same(i)]
        items.insert(0, item)
        save_store(items, CHANNEL_STORE)


def find_ytdlp():
    """Locate a yt-dlp binary, or None."""
    env = os.environ.get("YTDLP_BIN")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    found = shutil.which("yt-dlp")
    if found:
        return found
    for cand in (
        "/opt/homebrew/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
        os.path.expanduser("~/.local/bin/yt-dlp"),
        os.path.expanduser("~/yt-dlp"),
    ):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


_cookies_cache = {"path": None, "searched_at": 0.0}
_COOKIES_RETRY_AFTER = 30.0


def find_cookies(refresh=False):
    """Locate a browser cookies file for yt-dlp, or None.

    Opera's "Get cookies.txt LOCALLY" extension saves its export as
    "www.youtube.com_cookies.txt" inside ~/Downloads/Temp, NOT as
    ~/Downloads/cookies.txt - checking only the textbook path means
    running with no cookies at all and hitting YouTube's bot wall.
    So search, in order: ~/Downloads/cookies.txt,
    ~/Downloads/Temp/cookies.txt,
    ~/Downloads/Temp/www.youtube.com_cookies.txt,
    ~/Downloads/www.youtube.com_cookies.txt, then any *cookies*.txt in
    ~/Downloads/Temp or ~/Downloads, newest first. The first existing
    non-empty file wins. A found file is cached for the process; a
    "not found" result is only trusted for ~30 seconds, so a file
    exported after the addon started is picked up on its own - no
    restart needed. Pass refresh=True to search again right away
    (callers do that once when a fetch fails, in case the file was
    exported in the meantime)."""
    if not refresh:
        if _cookies_cache["path"]:
            return _cookies_cache["path"]
        if time.time() - _cookies_cache["searched_at"] < _COOKIES_RETRY_AFTER:
            return None
    downloads = os.path.expanduser("~/Downloads")
    temp = os.path.join(downloads, "Temp")
    candidates = [
        os.path.join(downloads, "cookies.txt"),
        os.path.join(temp, "cookies.txt"),
        os.path.join(temp, "www.youtube.com_cookies.txt"),
        os.path.join(downloads, "www.youtube.com_cookies.txt"),
    ]

    def usable(path):
        try:
            return os.path.isfile(path) and os.path.getsize(path) > 0
        except OSError:
            return False

    found = None
    for cand in candidates:
        if usable(cand):
            found = cand
            break
    if not found:
        pool = []
        for folder in (temp, downloads):
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                low = name.lower()
                if "cookies" in low and low.endswith(".txt"):
                    path = os.path.join(folder, name)
                    if usable(path):
                        try:
                            pool.append((os.path.getmtime(path), path))
                        except OSError:
                            pass
        if pool:
            pool.sort(reverse=True)
            found = pool[0][1]
    _cookies_cache["searched_at"] = time.time()
    _cookies_cache["path"] = found
    return found


def _ytdlp_get(ytdlp, base, extra, fmt, url):
    """One yt-dlp -g attempt. Returns the URL only when the attempt yields
    exactly one http line - a combined stream. Zero or 2+ lines (a split
    video+audio pair) counts as failure: the player can't use split URLs."""
    cmd = base + list(extra) + ["-f", fmt, "--", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except Exception:
        return None
    lines = [
        ln.strip()
        for ln in (out.stdout or "").splitlines()
        if ln.strip().startswith("http")
    ]
    if len(lines) == 1:
        return lines[0]
    return None


def youtube_video_id(item):
    """The bare 11-char video id for a YouTube item."""
    iid = item.get("id") or ""
    if iid.startswith("youtube:"):
        return iid[len("youtube:"):]
    m = YT_ID_RE.search(item.get("url") or "")
    return m.group(1) if m else iid


def _resolve_ladder(ytdlp, item_url, fmt, capped, cookies_path):
    """Walk the client ladder once and return the first combined stream
    URL, or None. cookies_path may be None (fetch without cookies).
    Ladder order (android_vr first - see resolve_youtube):
      android_vr capped, android_vr "best", default capped,
      default "best" (only when capped), tv capped, ios capped."""
    base = [ytdlp, "-g", "--no-warnings"]
    if cookies_path:
        base += ["--cookies", cookies_path]

    def client_args(name):
        if name is None:
            return []
        return ["--extractor-args", "youtube:player_client=" + name]

    ladder = [("android_vr", fmt), ("android_vr", "best"), (None, fmt)]
    if capped is not None:
        ladder.append((None, "best"))
    ladder.append(("tv", fmt))
    ladder.append(("ios", fmt))
    seen = set()
    for client, f in ladder:
        if (client, f) in seen:
            continue
        seen.add((client, f))
        url = _ytdlp_get(ytdlp, base, client_args(client), f, item_url)
        if url:
            return url
    return None


def live_master_url(item):
    """For a live YouTube item, the adaptive master playlist URL from
    the yt-dlp -j dump, or None. HLS formats carry it as manifest_url:
    an .../api/manifest/hls_variant/... URL (no single variant locked
    in) whose playlist lists every variant, so the player can adapt to
    the connection. The formats' own url fields are hls_playlist URLs
    pinned to one variant each. Identified by the hls_variant path
    segment - the signed URL itself is used exactly as served."""
    info = youtube_info(item)
    if not info or not info.get("is_live"):
        return None
    for f in (info.get("formats") or []):
        mu = f.get("manifest_url") or ""
        if "/api/manifest/hls_variant/" in mu:
            return mu
    return None


def pick_live_variant(master_url):
    """Pick the live relay's variant out of a live master playlist,
    or None. Fetches the playlist and scans its #EXT-X-STREAM-INF
    entries for the highest-bandwidth one whose video codec is avc1
    (H.264) - the relay copies the stream into an MP4 without
    re-encoding, and a VP9/AV1 variant would produce a file some
    players can't handle.

    The variant playlist alone is NOT enough: YouTube's variant
    playlists carry video only, and the sound lives in a separate
    #EXT-X-MEDIA audio rendition (the group the variant's AUDIO
    attribute names). So the return is (variant_url, audio_url,
    variant_ordinal, audio_ordinal) - audio_url is the group's
    DEFAULT rendition (or its first), or None when the master names
    no audio group. The ordinals are the 0-based positions of the
    chosen variant among all #EXT-X-STREAM-INF entries and of the
    chosen rendition among all #EXT-X-MEDIA audio entries: they are
    how ffmpeg numbers the streams of the master playlist when it is
    read as ONE input (0:v:N / 0:a:N), which is how the relay feeds
    it - see handle_live. Any failure - fetch error, no avc1
    variant - returns None and the caller falls back."""
    try:
        req = urllib.request.Request(
            master_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read(262144).decode("utf-8", "replace")
    except Exception:
        return None
    lines = text.splitlines()
    audio_groups = {}  # group id -> [(is_default, url, ordinal), ...]
    audio_count = 0
    for line in lines:
        line = line.strip()
        if not line.startswith("#EXT-X-MEDIA:") or "TYPE=AUDIO" not in line:
            continue
        gm = re.search(r'GROUP-ID="([^"]+)"', line)
        if not gm:
            continue
        um = re.search(r'URI="([^"]+)"', line)
        url = urljoin(master_url, um.group(1)) if um else None
        audio_groups.setdefault(gm.group(1), []).append(
            ("DEFAULT=YES" in line, url, audio_count)
        )
        audio_count += 1
    best = None  # (bandwidth, variant_url, audio_group, variant_ordinal)
    variant_count = 0
    for i, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        v_ord = variant_count
        variant_count += 1
        attrs = line.split(":", 1)[1] if ":" in line else ""
        cm = re.search(r'CODECS="([^"]+)"', attrs)
        codecs = cm.group(1) if cm else ""
        first = codecs.split(",")[0].strip().lower()
        if not first.startswith("avc1"):
            continue
        bw = 0
        bm = re.search(r"BANDWIDTH=(\d+)", attrs)
        if bm:
            bw = int(bm.group(1))
        am = re.search(r'AUDIO="([^"]+)"', attrs)
        group = am.group(1) if am else None
        # The variant URL is the next non-empty, non-tag line.
        for nxt in lines[i + 1:]:
            nxt = nxt.strip()
            if not nxt:
                continue
            if nxt.startswith("#"):
                break
            if best is None or bw > best[0]:
                best = (bw, urljoin(master_url, nxt), group, v_ord)
            break
    if not best:
        return None
    audio_url = None
    audio_ord = None
    renditions = audio_groups.get(best[2] or "", [])
    if renditions:
        chosen = next((r for r in renditions if r[0]), renditions[0])
        audio_url = chosen[1]
        if audio_url:
            audio_ord = chosen[2]
    return (best[1], audio_url, best[3], audio_ord)


def resolve_youtube(item):
    """Ask yt-dlp for a direct stream URL for a YouTube item.

    Honors the quality setting (a height cap; "best" is uncapped).
    The client ladder (see _resolve_ladder) tries android_vr first:
    on some networks/accounts it is the only client that offers a
    combined format at all, and walking the other clients first just
    burns seconds before it answers. (Some clients only hand out split
    audio/video formats, where no combined "best" exists; the ladder
    still walks them as a fallback.) Works for regular videos (direct
    file URL) and live streams (an .m3u8 URL - the adaptive master
    playlist when YouTube exposes one, so the player can step down on
    a slow connection instead of stalling on a pinned variant).

    Cookies: the ladder runs with the cookies file find_cookies()
    turns up. If every attempt with cookies fails, the ladder runs
    once more WITHOUT cookies before giving up - browsers rotate
    exported cookies, and a stale file can fail every client while
    the same fetch succeeds with no cookies at all.

    Results are cached in memory - 4 hours for regular streams, a
    minute for live manifests. Returns None on any failure.
    """
    quality = load_settings()["quality"]
    vid = youtube_video_id(item)
    cached = cache_resolve_get(vid, quality)
    if cached:
        return cached
    ytdlp = find_ytdlp()
    if not ytdlp:
        return None
    capped = None if quality == "best" else int(quality)
    fmt = "best" if capped is None else "best[height<=%d]/best" % capped
    url = None
    cookies_path = find_cookies()
    if cookies_path:
        url = _resolve_ladder(ytdlp, item["url"], fmt, capped, cookies_path)
        if not url:
            # The cookies file may have been exported (or moved) since
            # the cached lookup - search once more before falling back.
            fresh = find_cookies(refresh=True)
            if fresh and fresh != cookies_path:
                url = _resolve_ladder(ytdlp, item["url"], fmt, capped, fresh)
        if not url:
            url = _resolve_ladder(ytdlp, item["url"], fmt, capped, None)
    else:
        url = _resolve_ladder(ytdlp, item["url"], fmt, capped, None)
    if not url:
        return None
    if _is_hls_url(url):
        master = live_master_url(item)
        if master:
            url = master
    cache_resolve_put(vid, quality, url)
    return url


def fetch_youtube_title(url):
    """Best-effort real title for a YouTube video via yt-dlp, or None.
    Default client first, android_vr as the retry (the same pattern as
    youtube_info). If a cookies file was used and both attempts fail,
    both are retried without cookies (exported cookies go stale).
    Any failure just means the caller keeps the default name, so
    adding a video never breaks over a title lookup."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        return None
    attempts = [
        [],
        ["--extractor-args", "youtube:player_client=android_vr"],
    ]

    def try_all(cookies_path):
        base = [ytdlp, "--no-warnings", "--print", "%(title)s"]
        if cookies_path:
            base += ["--cookies", cookies_path]
        for extra in attempts:
            try:
                out = subprocess.run(
                    base + extra + ["--", url],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if out.returncode != 0:
                    continue
                for ln in (out.stdout or "").splitlines():
                    title = ln.strip()
                    if title:
                        return title[:120]
            except Exception:
                continue
        return None

    cookies_path = find_cookies()
    title = try_all(cookies_path)
    if not title and cookies_path:
        title = try_all(None)
    return title


def _channel_dump(videos_url, playlist_end, timeout):
    """One yt-dlp flat-playlist JSON dump of a channel's videos tab,
    or None on any failure. The top level carries the channel's own
    fields (channel = title, channel_id = UC id) and each entry a
    video's id and title. Cookies when a file is found, with the
    usual refresh + no-cookies retries (exported cookies go stale)."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        return None

    def attempt(cookies_path):
        cmd = [
            ytdlp,
            "--flat-playlist",
            "--playlist-end",
            str(playlist_end),
            "-J",
            "--no-warnings",
        ]
        if cookies_path:
            cmd += ["--cookies", cookies_path]
        cmd += ["--", videos_url]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception:
            return None
        if out.returncode != 0:
            return None
        try:
            data = json.loads(out.stdout)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    cookies_path = find_cookies()
    data = attempt(cookies_path)
    if data is None and cookies_path:
        fresh = find_cookies(refresh=True)
        if fresh and fresh != cookies_path:
            data = attempt(fresh)
        if data is None:
            data = attempt(None)
    return data


def resolve_channel(item, keep_name=False):
    """Best-effort fill-in of a channel item's real title and UC id
    via yt-dlp (one flat entry is enough to get the channel fields).
    When the UC id comes back, the item's id and URLs are upgraded
    to the canonical /channel/UC... form. A name the user typed
    (keep_name) is never overwritten. Any failure just leaves the
    URL-derived values in place, so adding a channel never breaks
    over a lookup."""
    data = _channel_dump(item["videos_url"], 1, 30)
    if not data:
        return item
    ucid = data.get("channel_id") or ""
    if ucid.startswith("UC"):
        item["ucid"] = ucid
        item["id"] = "channel:" + ucid
        item["url"] = "https://www.youtube.com/channel/" + ucid
        item["videos_url"] = item["url"] + "/videos"
    title = (data.get("channel") or "").strip()
    if title and not keep_name:
        item["name"] = title[:120]
    return item


def fetch_channel_videos(channel):
    """The channel's newest uploads as [(video_id, title)], newest
    first, or None when the fetch itself failed (callers then serve
    the last good list instead of wiping the channel's videos)."""
    data = _channel_dump(channel["videos_url"], CHANNEL_LIMIT, 60)
    if data is None:
        return None
    entries = data.get("entries")
    if not isinstance(entries, list):
        return None
    videos = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        vid = str(e.get("id") or "")
        if len(vid) != 11:
            continue
        title = (e.get("title") or "").strip() or ("YouTube " + vid)
        videos.append((vid, title[:200]))
    return videos


def channel_videos(channel):
    """Cache-aware newest-uploads list for a saved channel. The
    fetch repeats at most every CHANNEL_TTL; a failed refetch serves
    the last good list (never an empty wipe)."""
    cid = channel["id"]
    with CACHE_LOCK:
        hit = _channel_cache.get(cid)
    if hit and time.time() - hit[1] < CHANNEL_TTL:
        return hit[0]
    fresh = fetch_channel_videos(channel)
    if fresh is not None:
        with CACHE_LOCK:
            _channel_cache[cid] = (fresh, time.time())
        return fresh
    return hit[0] if hit else []


def channel_display_name(channel_name, title):
    """The catalog display name for one of a channel's uploads:
    "<Channel name> — <video title>", so the list in Nuvio says
    which channel each video came from. Falls back to the plain
    title when the channel has no name."""
    channel_name = (channel_name or "").strip()
    return (channel_name + " — " + title) if channel_name else title


def cached_channel_display_name(vid):
    """The channel display name for a video id, when a saved
    channel's uploads are already cached in memory - else None.
    Memory only: this never triggers a fetch, so it is safe to call
    from endpoints that should answer fast (the meta endpoint)."""
    with CACHE_LOCK:
        per_channel = [
            (cid, list(videos)) for cid, (videos, _ts) in _channel_cache.items()
        ]
    found = None
    for cid, videos in per_channel:
        for v, title in videos:
            if v == vid:
                found = (cid, title)
                break
        if found:
            break
    if not found:
        return None
    cid, title = found
    with LOCK:
        names = {c["id"]: c.get("name") for c in load_store(CHANNEL_STORE)}
    return channel_display_name(names.get(cid), title)


# The clip GET /remove/ serves: tapping Delete in Nuvio opens the
# remove URL like a video, so the player gets this tiny blank MP4
# (about a second of black and silence, fragmented like the other
# media the addon serves) and shows a harmless blink instead of an
# error. The deletion itself happens on that same request.
BLANK_CLIP = base64.b64decode(
    "AAAAHGZ0eXBpc281AAACAGlzbzVpc282bXA0MQAABM9tb292AAAAbG12aGQAAAAAAAAAAAAAAAAA"
    "AAPoAAAAAAABAAABAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAB8nRyYWsAAABcdGtoZAAAAAMAAAAAAAAA"
    "AAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAA"
    "AAAAAEAAAAABQAAAALQAAAAAAY5tZGlhAAAAIG1kaGQAAAAAAAAAAAAAAAAAADwAAAAAAFXEAAAA"
    "AAAtaGRscgAAAAAAAAAAdmlkZQAAAAAAAAAAAAAAAFZpZGVvSGFuZGxlcgAAAAE5bWluZgAAABR2"
    "bWhkAAAAAQAAAAAAAAAAAAAAJGRpbmYAAAAcZHJlZgAAAAAAAAABAAAADHVybCAAAAABAAAA+XN0"
    "YmwAAACtc3RzZAAAAAAAAAABAAAAnWF2YzEAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAABQAC0AEgA"
    "AABIAAAAAAAAAAEVTGF2YzYyLjI4LjEwMiBsaWJ4MjY0AAAAAAAAAAAAAAAY//8AAAA3YXZjQwFk"
    "AA3/4QAaZ2QADazZQUGfnwEQAAADABAAAAMDwPFCmWABAAZo6+PLIsD9+PgAAAAAEHBhc3AAAAAB"
    "AAAAAQAAABBzdHRzAAAAAAAAAAAAAAAQc3RzYwAAAAAAAAAAAAAAFHN0c3oAAAAAAAAAAAAAAAAA"
    "AAAQc3RjbwAAAAAAAAAAAAABv3RyYWsAAABcdGtoZAAAAAMAAAAAAAAAAAAAAAIAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAQEAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAA"
    "AAAAAVttZGlhAAAAIG1kaGQAAAAAAAAAAAAAAAAAAKxEAAAAAFXEAAAAAAAtaGRscgAAAAAAAAAA"
    "c291bgAAAAAAAAAAAAAAAFNvdW5kSGFuZGxlcgAAAAEGbWluZgAAABBzbWhkAAAAAAAAAAAAAAAk"
    "ZGluZgAAABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAADKc3RibAAAAH5zdHNkAAAAAAAAAAEA"
    "AABubXA0YQAAAAAAAAABAAAAAAAAAAAAAgAQAAAAAKxEAAAAAAA2ZXNkcwAAAAADgICAJQACAASA"
    "gIAXQBUAAAAAAfQAAAH0AAWAgIAFEhBW5QAGgICAAQIAAAAUYnRydAAAAAAAAfQAAAH0AAAAABBz"
    "dHRzAAAAAAAAAAAAAAAQc3RzYwAAAAAAAAAAAAAAFHN0c3oAAAAAAAAAAAAAAAAAAAAQc3RjbwAA"
    "AAAAAAAAAAAASG12ZXgAAAAgdHJleAAAAAAAAAABAAAAAQAAAAAAAAAAAAAAAAAAACB0cmV4AAAA"
    "AAAAAAIAAAABAAAAAAAAAAAAAAAAAAAAYnVkdGEAAABabWV0YQAAAAAAAAAhaGRscgAAAAAAAAAA"
    "bWRpcmFwcGwAAAAAAAAAAAAAAAAtaWxzdAAAACWpdG9vAAAAHWRhdGEAAAABAAAAAExhdmY2Mi4x"
    "Mi4xMDIAAAMMbW9vZgAAABBtZmhkAAAAAAAAAAEAAAFAdHJhZgAAABx0ZmhkAAIAOAAAAAEAAAIA"
    "AAAC7gEBAAAAAAAUdGZkdAEAAAAAAAAAAAAAAAAAAQh0cnVuAAAKBQAAAB4AAAMUAgAAAAAAAu4A"
    "AAQAAAAAEAAACgAAAAANAAAEAAAAAA0AAAAAAAAADQAAAgAAAAAWAAAKAAAAAA8AAAQAAAAADQAA"
    "AAAAAAANAAACAAAAABYAAAoAAAAADwAABAAAAAANAAAAAAAAAA0AAAIAAAAAFgAACgAAAAAPAAAE"
    "AAAAAA0AAAAAAAAADQAAAgAAAAAWAAAKAAAAAA8AAAQAAAAADQAAAAAAAAANAAACAAAAABYAAAoA"
    "AAAADwAABAAAAAANAAAAAAAAAA0AAAIAAAAAFgAACgAAAAAPAAAEAAAAAA0AAAAAAAAADQAAAgAA"
    "AAAWAAAEAAAAAbR0cmFmAAAAHHRmaGQAAgA4AAAAAgAAC3wAAAAXAgAAAAAAABR0ZmR0AQAAAAAA"
    "AAAAAAAAAAABfHRydW4AAAMBAAAALQAAB8kAAAt8AAAAFwAABAAAAAAGAAAEAAAAAAYAAAQAAAAA"
    "BgAABAAAAAAGAAAEAAAAAAYAAAQAAAAABgAABAAAAAAGAAAEAAAAAAYAAAQAAAAABgAABAAAAAAG"
    "AAAEAAAAAAYAAAQAAAAABgAABAAAAAAGAAAEAAAAAAYAAAQAAAAABgAABAAAAAAGAAAEAAAAAAYA"
    "AAQAAAAABgAABAAAAAAGAAAEAAAAAAYAAAQAAAAABgAABAAAAAAGAAAEAAAAAAYAAAQAAAAABgAA"
    "BAAAAAAGAAAEAAAAAAYAAAQAAAAABgAABAAAAAAGAAAEAAAAAAYAAAQAAAAABgAABAAAAAAGAAAE"
    "AAAAAAYAAAQAAAAABgAABAAAAAAGAAAEAAAAAAYAAAQAAAAABgAABAAAAAAGAAAEAAAAAAYAAAQA"
    "AAAABgAABAAAAAAGAAAEAAAAAAYAAAQAAAAABgAABAAAAAAGAAAARAAAAAYAAAXcbWRhdAAAAq4G"
    "Bf//qtxF6b3m2Ui3lizYINkj7u94MjY0IC0gY29yZSAxNjQgcjMxMDggMzFlMTlmOSAtIEguMjY0"
    "L01QRUctNCBBVkMgY29kZWMgLSBDb3B5bGVmdCAyMDAzLTIwMjMgLSBodHRwOi8vd3d3LnZpZGVv"
    "bGFuLm9yZy94MjY0Lmh0bWwgLSBvcHRpb25zOiBjYWJhYz0xIHJlZj0zIGRlYmxvY2s9MTowOjAg"
    "YW5hbHlzZT0weDM6MHgxMTMgbWU9aGV4IHN1Ym1lPTcgcHN5PTEgcHN5X3JkPTEuMDA6MC4wMCBt"
    "aXhlZF9yZWY9MSBtZV9yYW5nZT0xNiBjaHJvbWFfbWU9MSB0cmVsbGlzPTEgOHg4ZGN0PTEgY3Ft"
    "PTAgZGVhZHpvbmU9MjEsMTEgZmFzdF9wc2tpcD0xIGNocm9tYV9xcF9vZmZzZXQ9LTIgdGhyZWFk"
    "cz02IGxvb2thaGVhZF90aHJlYWRzPTEgc2xpY2VkX3RocmVhZHM9MCBucj0wIGRlY2ltYXRlPTEg"
    "aW50ZXJsYWNlZD0wIGJsdXJheV9jb21wYXQ9MCBjb25zdHJhaW5lZF9pbnRyYT0wIGJmcmFtZXM9"
    "MyBiX3B5cmFtaWQ9MiBiX2FkYXB0PTEgYl9iaWFzPTAgZGlyZWN0PTEgd2VpZ2h0Yj0xIG9wZW5f"
    "Z29wPTAgd2VpZ2h0cD0yIGtleWludD0yNTAga2V5aW50X21pbj0yNSBzY2VuZWN1dD00MCBpbnRy"
    "YV9yZWZyZXNoPTAgcmNfbG9va2FoZWFkPTQwIHJjPWNyZiBtYnRyZWU9MSBjcmY9MjMuMCBxY29t"
    "cD0wLjYwIHFwbWluPTAgcXBtYXg9NjkgcXBzdGVwPTQgaXBfcmF0aW89MS40MCBhcT0xOjEuMDAA"
    "gAAAADhliIQAN//+9vD+BTZWBFCXEc3onTMfvxW4ujQ3vc4AAq1PEG+4LBNpT2AQQAABnxHdErgs"
    "pBAdkQAAAAxBmiRsQ3/+p4QAA/wAAAAJQZ5CeIV/AANTAAAACQGeYXRCfwAEPAAAAAkBnmNqQn8A"
    "BD0AAAASQZpoSahBaJlMCG///qeEAAP9AAAAC0GehkURLCv/AANTAAAACQGepXRCfwAEPQAAAAkB"
    "nqdqQn8ABDwAAAASQZqsSahBbJlMCG///qeEAAP8AAAAC0GeykUVLCv/AANTAAAACQGe6XRCfwAE"
    "PAAAAAkBnutqQn8ABDwAAAASQZrwSahBbJlMCG///qeEAAP9AAAAC0GfDkUVLCv/AANTAAAACQGf"
    "LXRCfwAEPQAAAAkBny9qQn8ABDwAAAASQZs0SahBbJlMCG///qeEAAP8AAAAC0GfUkUVLCv/AANT"
    "AAAACQGfcXRCfwAEPAAAAAkBn3NqQn8ABDwAAAASQZt4SahBbJlMCGf//p4QAA+ZAAAAC0GflkUV"
    "LCv/AANSAAAACQGftXRCfwAEPQAAAAkBn7dqQn8ABD0AAAASQZu8SahBbJlMCFf//jhAAD0gAAAA"
    "C0Gf2kUVLCv/AANTAAAACQGf+XRCfwAEPAAAAAkBn/tqQn8ABD0AAAASQZv9SahBbJlMCE///fEA"
    "AJeB3gIATGF2YzYyLjI4LjEwMgBCIAjBGDghEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwh"
    "EARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARg"
    "jBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwh"
    "EARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARg"
    "jBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwhEARgjBwh"
    "EARgjBwAAABubWZyYQAAACt0ZnJhAQAAAAAAAAEAAAAAAAAAAQAAAAAAAAQAAAAAAAAABOsBAQEA"
    "AAArdGZyYQEAAAAAAAACAAAAAAAAAAEAAAAAAAAAAAAAAAAAAATrAQEBAAAAEG1mcm8AAAAAAAAA"
    "bg=="
)


def cached_channel_of(vid):
    """(channel id, channel name) when a saved channel's cached
    uploads include this video id - else None. Memory only (never
    fetches): the same in-memory channel uploads cache the
    channel-name labels read."""
    with CACHE_LOCK:
        per_channel = [
            (cid, list(videos)) for cid, (videos, _ts) in _channel_cache.items()
        ]
    hit = None
    for cid, videos in per_channel:
        if any(v == vid for v, _t in videos):
            hit = cid
            break
    if not hit:
        return None
    with LOCK:
        names = {c["id"]: c.get("name") for c in load_store(CHANNEL_STORE)}
    if hit not in names:
        return None
    return (hit, names.get(hit) or "")


def remove_stored_item(item_id):
    """Delete an id from whichever store holds it - all four stores
    are checked, the same sweep the configure page's /delete does.
    Returns True when something was actually removed."""
    removed = False
    with LOCK:
        for path in (STORE, LINK_STORE, YT_STORE, CHANNEL_STORE):
            items = load_store(path)
            kept = [i for i in items if i["id"] != item_id]
            if len(kept) != len(items):
                save_store(kept, path)
                removed = True
    return removed


def delete_stream_entry(target_id, title):
    """The Delete stream entry appended last to a stored item's
    stream list. The Stremio protocol has no buttons or menus, so
    deleting from inside Nuvio rides in as one more "stream":
    playing it calls the addon's /remove/ route for the target."""
    return {
        "name": "Delete",
        "title": title,
        "url": "http://127.0.0.1:%d/remove/%s" % (PORT, target_id),
    }


def find_ffmpeg():
    """Locate an ffmpeg binary, or None (same lookup pattern as yt-dlp)."""
    env = os.environ.get("FFMPEG_BIN")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in (
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        os.path.expanduser("~/.local/bin/ffmpeg"),
    ):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def youtube_info(item):
    """One yt-dlp -j dump for a YouTube item, or None on any failure.
    Default client first, android_vr as the retry. If a cookies file
    was used and both attempts fail, the search runs once more (the
    file may have been exported since) and then both attempts are
    retried without cookies (exported cookies go stale). If the
    attempts ran with NO cookies and failed, one fresh cookie search
    is forced and, when a file turns up, the attempts run once more
    with it - a file exported after the addon started must not need
    a restart to take effect."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        return None
    attempts = [
        [],
        ["--extractor-args", "youtube:player_client=android_vr"],
    ]

    def try_all(cookies_path):
        base = [ytdlp, "-j", "--no-warnings"]
        if cookies_path:
            base += ["--cookies", cookies_path]
        for extra in attempts:
            try:
                out = subprocess.run(
                    base + extra + ["--", item["url"]],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if out.returncode != 0:
                    continue
                info = json.loads(out.stdout)
                if isinstance(info, dict):
                    return info
            except Exception:
                continue
        return None

    cookies_path = find_cookies()
    info = try_all(cookies_path)
    if info is None and cookies_path:
        fresh = find_cookies(refresh=True)
        if fresh and fresh != cookies_path:
            info = try_all(fresh)
        if info is None:
            info = try_all(None)
    if info is None and not cookies_path:
        # The attempts above ran with no cookies at all (the lookup
        # found no file). The file may have been exported since that
        # lookup - force one fresh search now, ignoring the "not
        # found" pause, and retry once with it before giving up.
        fresh = find_cookies(refresh=True)
        if fresh:
            info = try_all(fresh)
    return info


def pick_merge_pair(info, cap):
    """Pick the (video, audio) format pair for the HD merge from a yt-dlp
    -j dump: the highest avc1 (H.264) video-only format at or under the
    height cap, plus the best mp4a audio-only format. Returns None when
    either side is missing - the player needs H.264+AAC in an MP4, which
    is why the video side insists on avc1."""
    videos = []
    audios = []
    for f in (info.get("formats") or []):
        if not f.get("url"):
            continue
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        if vcodec.startswith("avc1") and acodec == "none":
            height = f.get("height") or 0
            if height and height <= cap:
                videos.append((height, f.get("tbr") or 0, f))
        elif acodec.startswith("mp4a") and vcodec == "none":
            audios.append((f.get("abr") or f.get("tbr") or 0, f))
    if not videos or not audios:
        return None
    videos.sort(key=lambda t: (t[0], t[1]))
    audios.sort(key=lambda t: t[0])
    return videos[-1][2], audios[-1][1]


def synth_youtube_item(vid):
    """A playable YouTube item built from just a video id - used for
    videos that belong to a saved channel (they are never stored
    individually) and for well-formed ids Nuvio asks about directly.
    Everything downstream (poster, title fetch, stream resolution)
    already derives what it needs from the id."""
    return {
        "id": "youtube:" + vid,
        "name": "YouTube " + vid,
        "url": "https://www.youtube.com/watch?v=" + vid,
        "kind": "youtube",
    }


def find_youtube_item(raw_id):
    """The saved YouTube item for a /play or /live path tail - accepts
    the full "youtube:..." id or the bare video id. An unsaved but
    well-formed id (e.g. a video from a saved channel) synthesizes a
    playable item; anything else is None."""
    item_id = unquote(raw_id or "")
    with LOCK:
        for it in load_store(YT_STORE):
            if it["id"] == item_id or it["id"] == "youtube:" + item_id:
                return it
    m = re.match(r"(?:youtube:)?([A-Za-z0-9_-]{11})$", item_id)
    if m:
        return synth_youtube_item(m.group(1))
    return None


def find_item(item_id):
    item_id = unquote(item_id or "")
    if item_id.lower().startswith("magnet:?"):
        return parse_magnet(item_id)
    with LOCK:
        for it in load_store(STORE):
            if it["id"] == item_id:
                return it
        for it in load_store(LINK_STORE):
            if it["id"] == item_id:
                return it
        for it in load_store(YT_STORE):
            if it["id"] == item_id:
                return it
    # Not saved: a well-formed YouTube id still plays (this is how
    # videos from saved channels resolve - they are never stored).
    m = re.match(r"youtube:([A-Za-z0-9_-]{11})$", item_id)
    if m:
        return synth_youtube_item(m.group(1))
    # Not saved: still playable from the bare hash.
    m = re.match(r"magnet:([a-f0-9]{40})$", item_id)
    if m:
        h = m.group(1)
        return {
            "id": item_id,
            "name": "Magnet " + h[:8],
            "magnet": "magnet:?xt=urn:btih:" + h,
            "infoHash": h,
            "sources": ["tracker:" + t for t in DEFAULT_TRACKERS],
        }
    return None


def meta_of(item):
    if item.get("kind") == "link":
        desc = "Your pasted web video link."
    elif item.get("kind") == "youtube":
        desc = "Your pasted YouTube video."
    else:
        desc = "Your pasted magnet link (P2P)."
    meta = {
        "id": item["id"],
        "type": "movie",
        "name": item["name"],
        "description": desc,
    }
    if item.get("kind") == "youtube":
        vid = youtube_video_id(item)
        if vid:
            poster = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid
            meta["poster"] = poster
            meta["background"] = poster
    return meta


def esc(s):
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fetched_version(text):
    """The version marker of a magnet_addon.py source text - the same
    "version" field the manifest carries - or None if there is none."""
    m = re.search(r'"version"\s*:\s*"([0-9]+(?:\.[0-9]+)*)"', text or "")
    return m.group(1) if m else None


def _version_key(version):
    return tuple(int(p) for p in version.split("."))


def fetch_update():
    """Fetch the newest magnet_addon.py and install it over the
    running script's own file - but only when what came back is valid
    Python carrying a strictly newer version. Returns (new_version,
    None) on success and (None, reason) otherwise; on any failure the
    running file is left exactly as it was."""
    try:
        req = urllib.request.Request(
            UPDATE_URL,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except Exception as e:
        return None, "Could not fetch the update (%s). Nothing changed." % e
    try:
        text = raw.decode("utf-8")
    except Exception:
        return None, "The update that came back was not readable text. Nothing changed."
    new_version = _fetched_version(text)
    if not new_version:
        return None, "The update that came back has no version marker. Nothing changed."
    if _version_key(new_version) <= _version_key(MANIFEST["version"]):
        return None, (
            "Already up to date - the newest version out there is %s "
            "and you are running %s." % (new_version, MANIFEST["version"])
        )
    try:
        compile(text, "magnet_addon.py", "exec")
    except Exception:
        return None, "The update that came back is not valid Python. Nothing changed."
    target = os.path.abspath(__file__)
    tmp = target + ".update-tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return None, "Could not write the update (%s). Nothing changed." % e
    return new_version, None


def updated_page(version):
    return """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>My Magnet Links</title>
<meta http-equiv="refresh" content="4;url=/configure">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{background:#111;color:#eee;font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:24px auto;padding:0 16px}</style>
</head><body>
<h2>Updated to version %s</h2>
<p>The app is restarting on the new version - this page will reload
to the configure page in a few seconds.</p>
<script>setTimeout(function(){ location.replace("/configure"); }, 4000);</script>
</body></html>""" % esc(version)


def configure_page(msg=""):
    with LOCK:
        magnets = load_store(STORE)
        links = load_store(LINK_STORE)
        youtubes = load_store(YT_STORE)
        channels = load_store(CHANNEL_STORE)
        quality = load_settings()["quality"]
    options = ""
    for value, label in QUALITY_LABELS:
        sel = " selected" if value == quality else ""
        options += '<option value="%s"%s>%s</option>' % (value, sel, label)
    quality_form = """<form method="post" action="/settings" style="margin-top:28px">
<label>YouTube quality</label>
<select name="quality">%s</select>
<p style="color:#999;margin:4px 0 8px">Higher isn't always offered -
YouTube hands this player a single combined file, so the app takes the
best combined stream at or under your pick.</p>
<button type="submit">Save quality</button>
</form>
<p style="color:#999">YouTube videos play through the app's own
<b>relay</b> stream - the app fetches the video and serves it to
Nuvio itself, joining YouTube's separate picture and sound streams
together at up to your quality setting where YouTube splits them.
It can start a little slower, and seeking is limited.</p>
<p style="color:#999">Live items get a <b>Live relay</b> stream:
the app pulls the live feed itself and hands it to Nuvio as one
continuous stream.</p>
<p style="color:#999">You can also delete items from <b>inside
Nuvio</b>: open an item and pick the <b>Delete</b> entry at the
bottom of its stream list (for a channel's video it removes the
whole channel). There is no undo - add the link again to bring
something back. A deleted item can still show in Nuvio's list
until the list refreshes; reopening Nuvio, or searching, shows
the updated list.</p>""" % options
    update_form = """<form method="post" action="/update" style="margin-top:28px">
<label>Updates</label>
<p style="color:#999;margin:4px 0 8px">You are running version %s.
Pressing Update fetches the newest version and installs it in place;
the app restarts itself and this page comes back on the new version.
Approving the app in macOS Settings is a one-time thing - updates
through this button never need it again.</p>
<button type="submit">Update</button>
</form>""" % MANIFEST["version"]
    channel_form = """<form method="post" action="/addchannel" style="margin-top:28px">
<label>Name (optional)</label>
<input name="name" placeholder="Channel name - the real one fills in by itself">
<label>YouTube channel link</label>
<input name="channel" placeholder="https://www.youtube.com/@channelname">
<button type="submit">Add channel</button>
</form>
<p style="color:#999">A channel's <b>newest uploads</b> (up to ten)
join the catalog on their own: the app re-checks each channel about
every 30 minutes while it runs, so new uploads appear without you
pasting anything. Nuvio shows them when its list refreshes -
reopening Nuvio, or searching for the title.</p>"""
    rows = ""
    for it in links:
        rows += (
            '<div style="padding:10px 0;border-bottom:1px solid #333">'
            "<b>%s</b> <small style='color:#7ab'>(web link)</small><br>"
            "<small style='color:#999'>%s</small><br>"
            '<a href="/delete?id=%s" style="color:#e66">Delete</a></div>'
            % (esc(it["name"]), esc(it["url"]), esc(it["id"]))
        )
    for it in youtubes:
        rows += (
            '<div style="padding:10px 0;border-bottom:1px solid #333">'
            "<b>%s</b> <small style='color:#7ab'>(YouTube)</small><br>"
            "<small style='color:#999'>%s</small><br>"
            '<a href="/delete?id=%s" style="color:#e66">Delete</a></div>'
            % (esc(it["name"]), esc(it["url"]), esc(it["id"]))
        )
    for it in channels:
        rows += (
            '<div style="padding:10px 0;border-bottom:1px solid #333">'
            "<b>%s</b> <small style='color:#7ab'>(channel)</small><br>"
            "<small style='color:#999'>%s</small><br>"
            '<a href="/delete?id=%s" style="color:#e66">Delete</a></div>'
            % (esc(it["name"]), esc(it["url"]), esc(it["id"]))
        )
    for it in magnets:
        rows += (
            '<div style="padding:10px 0;border-bottom:1px solid #333">'
            "<b>%s</b><br><small style='color:#999'>%s</small><br>"
            '<a href="/delete?id=%s" style="color:#e66">Delete</a></div>'
            % (esc(it["name"]), esc(it["id"]), esc(it["id"]))
        )
    if not magnets and not links and not youtubes and not channels:
        rows = "<p style='color:#999'>Nothing saved yet.</p>"
    note = ('<p style="color:#6f6">%s</p>' % esc(msg)) if msg else ""
    html = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>My Magnet Links</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{background:#111;color:#eee;font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:24px auto;padding:0 16px}
input,textarea,select{width:100%%;box-sizing:border-box;padding:10px;margin:6px 0;background:#222;color:#eee;border:1px solid #444;border-radius:6px}
button{background:#2b6cb0;color:#fff;border:0;padding:12px 20px;border-radius:6px;font-size:16px}
a{color:#7ab}</style></head><body>
<h2>My Magnet Links</h2>
<p>Install in Nuvio with this URL:<br><code>http://127.0.0.1:%d/manifest.json</code></p>
<p>Paste a magnet link or a web video link below. Both appear in Nuvio
under <b>My Magnet Links</b>. Keep this program running while you watch.
Magnets play peer-to-peer, so have your VPN on for those. Web video
links play straight from the web - no VPN needed. A web link has to be
the video file/stream address itself (like .mp4, .mkv or .m3u8),
not a web page.</p>
<p>YouTube links resolve when you press play, so yt-dlp needs to be
installed on this Mac, and the first play can take a few seconds.</p>
%s
<form method="post" action="/addany" style="border:1px solid #2b6cb0;border-radius:6px;padding:12px">
<label><b>Paste any link</b> - magnet, web video address, YouTube
video, or YouTube channel. This box sorts out which kind it is.</label>
<input name="paste" placeholder="magnet:?... or https://...">
<label>Name (optional)</label>
<input name="name" placeholder="Name - YouTube titles fill in by themselves">
<button type="submit">Add it</button>
</form>
<p style="color:#999">The separate boxes below still work too - they
are the same thing, one kind at a time.</p>
<form method="post" action="/add">
<label>Name (optional)</label>
<input name="name" placeholder="Movie name">
<label>Magnet link</label>
<textarea name="magnet" rows="3" placeholder="magnet:?xt=urn:btih:..."></textarea>
<button type="submit">Add magnet</button>
</form>
<form method="post" action="/addlink" style="margin-top:28px">
<label>Name (optional)</label>
<input name="name" placeholder="Video name">
<label>Web video link</label>
<input name="link" placeholder="https://.../movie.mp4">
<button type="submit">Add web link</button>
</form>
<form method="post" action="/addyoutube" style="margin-top:28px">
<label>Name (optional)</label>
<input name="name" placeholder="Video name">
<label>YouTube link</label>
<input name="youtube" placeholder="https://www.youtube.com/watch?v=...">
<button type="submit">Add YouTube video</button>
</form>
%s
%s
%s
<h3>Saved</h3>%s
</body></html>""" % (PORT, note, quality_form, channel_form, update_form, rows)
    return html


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def redirect_absolute(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _pump_ffmpeg(self, cmd):
        """Run ffmpeg and stream its stdout to the client as a
        close-delimited video/mp4 response. Returns False only when
        ffmpeg could not even be started (the caller then falls back);
        once the response is being served it returns True - including
        when the client goes away mid-stream, in which case ffmpeg is
        killed and reaped either way."""
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except Exception:
            return False
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    # read1 returns whatever ffmpeg has produced so far
                    # instead of waiting to fill the whole buffer - so a
                    # client that goes away is noticed on the next write,
                    # not a full buffer later.
                    chunk = proc.stdout.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                try:
                    proc.kill()
                except Exception:
                    pass
        finally:
            try:
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        return True

    def handle_play(self, raw_id, head_only=False):
        """Serve the HD merge for a saved YouTube item at /play/<id>.

        Live items and items without a mergeable format pair fall back
        to a redirect to the plain combined stream. The merge itself is
        ffmpeg joining the separate picture and sound streams into a
        fragmented MP4 on the fly - no seeking, close-delimited body.
        """
        item = find_youtube_item(raw_id)
        if not item:
            if head_only:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
            else:
                self.send_json({"error": "not found"}, 404)
            return
        if head_only:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        def fallback():
            combined = resolve_youtube(item)
            if combined:
                self.redirect_absolute(combined)
            else:
                self.send_json({"error": "stream unavailable"}, 404)

        quality = load_settings()["quality"]
        cap = int(quality) if quality.isdigit() else 1080
        vid = youtube_video_id(item)
        pair = None
        cached_pair = cache_pair_get(vid, cap)
        if cached_pair:
            # A cached pair only ever exists for a non-live item, so
            # the live check is already settled and the yt-dlp -j call
            # can be skipped entirely.
            pair = ({"url": cached_pair[0]}, {"url": cached_pair[1]})
        else:
            info = youtube_info(item)
            if info is not None and info.get("is_live"):
                fallback()
                return
            picked = pick_merge_pair(info, cap) if info else None
            if picked:
                pair = picked
                cache_pair_put(vid, cap, picked[0]["url"], picked[1]["url"])
        ffmpeg = find_ffmpeg() if pair else None
        if not pair or not ffmpeg:
            fallback()
            return
        vfmt, afmt = pair
        cmd = [
            ffmpeg,
            "-v",
            "error",
            "-i",
            vfmt["url"],
            "-i",
            afmt["url"],
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]
        if not self._pump_ffmpeg(cmd):
            fallback()
            return

    def handle_live(self, raw_id, head_only=False):
        """Relay a live YouTube item at /live/<id> as one continuous
        stream.

        YouTube live playlists only hand out a short window of
        segments per snapshot and expect the player to keep re-asking;
        a player that doesn't keep up runs out of video and stops.
        Here ffmpeg follows the (variant) playlist itself - reloading
        it as new segments appear - and the client just receives one
        endless fragmented MP4. Non-live items redirect to their plain
        resolved stream instead.
        """
        item = find_youtube_item(raw_id)
        if not item:
            if head_only:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
            else:
                self.send_json({"error": "not found"}, 404)
            return
        if head_only:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        resolved = resolve_youtube(item)
        if not resolved:
            self.send_json({"error": "stream unavailable"}, 404)
            return
        if not _is_hls_url(resolved):
            self.redirect_absolute(resolved)
            return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.redirect_absolute(resolved)
            return
        # Feed ffmpeg the master playlist as ONE input, mapping the
        # best H.264 variant and its audio rendition by their
        # positions in the playlist (pick_live_variant's ordinals).
        # The relay used to feed the variant and audio playlists as
        # TWO separate inputs - but each input then runs its own
        # live-playlist cycle, and the two can drift a whole segment
        # (~5s) apart in what they're actually playing: the picture
        # and the sound come out of sync (measured +5.03s of skew on
        # a 90s capture, and a 5.04s hole at the start of the audio
        # on another). One input means one demuxer serving both
        # renditions off the same playlist snapshot, so they stay
        # locked together. If no variant qualifies (or the playlist
        # can't be read), the master still goes in as one input and
        # ffmpeg picks the first streams. The audio in these
        # playlists is ADTS, which an MP4 can't hold as-is - the
        # aac_adtstoasc bitstream filter rewrites the headers on the
        # way through (still a copy, no re-encode).
        reconnect = [
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ]
        picked = pick_live_variant(resolved)
        cmd = [ffmpeg, "-v", "error"] + reconnect
        if picked:
            _vurl, _aurl, v_ord, a_ord = picked
            cmd += ["-i", resolved, "-map", "0:v:%d" % v_ord]
            if a_ord is not None:
                cmd += ["-map", "0:a:%d" % a_ord]
            else:
                cmd += ["-map", "0:a:0?"]
        else:
            cmd += ["-i", resolved, "-map", "0:v:0", "-map", "0:a:0?"]
        cmd += [
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]
        if not self._pump_ffmpeg(cmd):
            self.redirect_absolute(resolved)
            return

    def send_blank_clip(self):
        """Serve the tiny blank MP4 (see BLANK_CLIP)."""
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(BLANK_CLIP)))
        self.end_headers()
        self.wfile.write(BLANK_CLIP)

    def handle_remove(self, raw_id, head_only=False):
        """Delete a saved item from inside Nuvio: GET /remove/<id>.

        The Delete stream entry points here. A GET removes the id
        from whichever store holds it and serves the blank clip, so
        the player shows a harmless blink instead of an error; an
        unknown id removes nothing and still gets the clip. A HEAD
        request (players probing the "video" first) only answers
        headers - it never deletes."""
        if head_only:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(BLANK_CLIP)))
            self.end_headers()
            return
        remove_stored_item(unquote(raw_id or ""))
        self.send_blank_clip()

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/remove/"):
            self.handle_remove(parsed.path[len("/remove/"):], head_only=True)
            return
        if parsed.path.startswith("/play/"):
            self.handle_play(parsed.path[len("/play/"):], head_only=True)
            return
        if parsed.path.startswith("/live/"):
            self.handle_live(parsed.path[len("/live/"):], head_only=True)
            return
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/configure"):
            q = parse_qs(parsed.query)
            msg = (q.get("msg") or [""])[0]
            self.send_html(configure_page(msg))
            return
        if path.startswith("/play/"):
            self.handle_play(path[len("/play/"):])
            return
        if path.startswith("/live/"):
            self.handle_live(path[len("/live/"):])
            return
        if path.startswith("/remove/"):
            self.handle_remove(path[len("/remove/"):])
            return
        if path == "/manifest.json":
            self.send_json(MANIFEST)
            return
        if path == "/delete":
            q = parse_qs(parsed.query)
            did = (q.get("id") or [""])[0]
            with LOCK:
                save_store([i for i in load_store(STORE) if i["id"] != did], STORE)
                save_store([i for i in load_store(LINK_STORE) if i["id"] != did], LINK_STORE)
                save_store([i for i in load_store(YT_STORE) if i["id"] != did], YT_STORE)
                save_store([i for i in load_store(CHANNEL_STORE) if i["id"] != did], CHANNEL_STORE)
            self.redirect("/configure")
            return
        # /catalog/movie/mymagnets[.json|/search=..json]
        m = re.match(r"^/catalog/movie/mymagnets(?:/(.*))?\.json$", path)
        if m:
            extra = m.group(1) or ""
            eq = parse_qs(extra.replace("=", "==", 1).replace("==", "=", 1)) if "=" in extra else {}
            search = ""
            if extra.startswith("search="):
                search = unquote(extra[len("search="):]).lower()
            with LOCK:
                items = all_items()
                channels = load_store(CHANNEL_STORE)
                saved_vids = {youtube_video_id(i) for i in load_store(YT_STORE)}
            # Channel uploads come after his own saved items, as one
            # block per channel in saved-channel order, newest first.
            # A video he already saved individually is not repeated.
            seen = set(saved_vids)
            for ch in channels:
                for vid, title in channel_videos(ch):
                    if vid in seen:
                        continue
                    seen.add(vid)
                    virtual = synth_youtube_item(vid)
                    virtual["name"] = channel_display_name(ch.get("name"), title)
                    items.append(virtual)
            metas = [meta_of(i) for i in items if not search or search in i["name"].lower()]
            self.send_json({"metas": metas})
            return
        m = re.match(r"^/meta/movie/(.+)\.json$", path)
        if m:
            item = find_item(m.group(1))
            if item and item.get("kind") == "youtube":
                # A video that came from a saved channel carries the
                # channel's name in the list ("<Channel> — <title>");
                # serve the same name here when the channel's
                # uploads are in memory. A copy saved individually
                # keeps its own name.
                with LOCK:
                    saved_ids = {i["id"] for i in load_store(YT_STORE)}
                if item["id"] not in saved_ids:
                    disp = cached_channel_display_name(youtube_video_id(item))
                    if disp:
                        item["name"] = disp
            self.send_json({"meta": meta_of(item)} if item else {"meta": None})
            return
        m = re.match(r"^/stream/movie/(.+)\.json$", path)
        if m:
            item = find_item(m.group(1))
            if not item:
                self.send_json({"streams": []})
                return
            if item.get("kind") == "link":
                streams = [
                    {
                        "name": "Web link",
                        "title": item["name"] + "\nDirect web stream",
                        "url": item["url"],
                    },
                ]
                streams.append(
                    delete_stream_entry(
                        item["id"],
                        "Delete this item\n%s — removes it from My Magnet Links"
                        % item["name"],
                    )
                )
                self.send_json({"streams": streams})
                return
            if item.get("kind") == "youtube":
                resolved = resolve_youtube(item)
                if resolved and _is_hls_url(resolved):
                    # Live: only the relay. The addon follows the
                    # live playlist itself and feeds the player one
                    # continuous stream - a player handed the
                    # playlist directly can run out of segments and
                    # stop, so the direct stream is not listed at
                    # all. The HD merge doesn't apply to live either.
                    streams = [
                        {
                            "name": "YouTube Live (relay)",
                            "title": item["name"]
                            + "\nLive relay — app feeds the stream, plays continuously",
                            "url": "http://127.0.0.1:%d/live/%s"
                            % (PORT, item["id"]),
                        }
                    ]
                else:
                    # Everything else plays through the app too: the
                    # direct stream links proved unreliable, so a
                    # YouTube item lists exactly one stream - the
                    # /play/ relay - at every quality setting. Inside
                    # /play/ the addon merges the separate picture and
                    # sound streams at the quality cap, and a video it
                    # can't merge still plays through the fallback
                    # there, so nothing is lost by not listing the
                    # direct link.
                    streams = [
                        {
                            "name": "YouTube (relay)",
                            "title": item["name"]
                            + "\nApp-served stream — the app feeds the video",
                            "url": "http://127.0.0.1:%d/play/%s"
                            % (PORT, item["id"]),
                        }
                    ]
                # The Delete entry rides last. It points at the
                # video itself when the video is saved on its own,
                # and at the whole channel when the video came from
                # a saved channel (those videos are never stored -
                # they only exist in the channel cache). A bare id
                # that is neither gets no Delete entry.
                chan = cached_channel_of(youtube_video_id(item))
                if chan:
                    streams.append(
                        delete_stream_entry(
                            chan[0],
                            "Remove this channel\n%s — removes the channel and its videos"
                            % (chan[1] or chan[0]),
                        )
                    )
                else:
                    with LOCK:
                        saved_ids = {i["id"] for i in load_store(YT_STORE)}
                    if item["id"] in saved_ids:
                        streams.append(
                            delete_stream_entry(
                                item["id"],
                                "Delete this item\n%s — removes it from My Magnet Links"
                                % item["name"],
                            )
                        )
                self.send_json({"streams": streams})
                return
            streams = [
                {
                    "name": "Magnet",
                    "title": item["name"] + "\nP2P torrent stream",
                    "infoHash": item["infoHash"],
                    "sources": item["sources"],
                },
                {
                    "name": "Magnet link",
                    "title": item["name"] + "\nDirect magnet (P2P)",
                    "url": item["magnet"],
                },
            ]
            # Only a magnet that is actually saved gets the Delete
            # entry - a magnet Nuvio was handed directly (bare hash
            # or a pasted magnet URI) is not stored, so there is
            # nothing to delete.
            with LOCK:
                saved_ids = {i["id"] for i in load_store(STORE)}
            if item["id"] in saved_ids:
                streams.append(
                    delete_stream_entry(
                        item["id"],
                        "Delete this item\n%s — removes it from My Magnet Links"
                        % item["name"],
                    )
                )
            self.send_json({"streams": streams})
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/addany":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            raw = (form.get("paste") or [""])[0].strip()
            name = (form.get("name") or [""])[0].strip()
            if raw.lower().startswith("magnet:?"):
                item = parse_magnet(raw)
                if not item:
                    self.send_html(configure_page("That magnet link did not parse - nothing saved."))
                    return
                if name:
                    item["name"] = name
                with LOCK:
                    items = [i for i in load_store(STORE) if i["id"] != item["id"]]
                    items.insert(0, item)
                    save_store(items, STORE)
                self.redirect("/configure")
                return
            ch_item = parse_channel(raw, name)
            if ch_item:
                ch_item = resolve_channel(ch_item, keep_name=bool(name))
                save_channel(ch_item)
                self.redirect("/configure")
                return
            yt_item = parse_youtube(raw, name)
            if yt_item:
                if not name:
                    title = fetch_youtube_title(yt_item["url"])
                    if title:
                        yt_item["name"] = title
                with LOCK:
                    items = [i for i in load_store(YT_STORE) if i["id"] != yt_item["id"]]
                    items.insert(0, yt_item)
                    save_store(items, YT_STORE)
                self.redirect("/configure")
                return
            link_item = parse_link(raw, name)
            if link_item:
                with LOCK:
                    items = [i for i in load_store(LINK_STORE) if i["id"] != link_item["id"]]
                    items.insert(0, link_item)
                    save_store(items, LINK_STORE)
                self.redirect("/configure")
                return
            self.send_html(configure_page("That link did not parse - paste a magnet link, a web video address, or a YouTube video or channel link. Nothing saved."))
            return
        if parsed.path == "/add":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            magnet = (form.get("magnet") or [""])[0]
            name = (form.get("name") or [""])[0].strip()
            item = parse_magnet(magnet)
            if not item:
                self.send_html(configure_page("That magnet link did not parse - nothing saved."))
                return
            if name:
                item["name"] = name
            with LOCK:
                items = [i for i in load_store(STORE) if i["id"] != item["id"]]
                items.insert(0, item)
                save_store(items, STORE)
            self.redirect("/configure")
            return
        if parsed.path == "/addlink":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            link = (form.get("link") or [""])[0]
            name = (form.get("name") or [""])[0].strip()
            item = parse_link(link, name)
            if not item:
                self.send_html(configure_page("That web link did not parse - it has to start with http:// or https://. Nothing saved."))
                return
            with LOCK:
                items = [i for i in load_store(LINK_STORE) if i["id"] != item["id"]]
                items.insert(0, item)
                save_store(items, LINK_STORE)
            self.redirect("/configure")
            return
        if parsed.path == "/addyoutube":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            yt = (form.get("youtube") or [""])[0]
            name = (form.get("name") or [""])[0].strip()
            item = parse_youtube(yt, name)
            if not item:
                self.send_html(configure_page("That YouTube link did not parse - it has to be a youtube.com or youtu.be video link. Nothing saved."))
                return
            if not name:
                title = fetch_youtube_title(item["url"])
                if title:
                    item["name"] = title
            with LOCK:
                items = [i for i in load_store(YT_STORE) if i["id"] != item["id"]]
                items.insert(0, item)
                save_store(items, YT_STORE)
            self.redirect("/configure")
            return
        if parsed.path == "/addchannel":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            ch = (form.get("channel") or [""])[0]
            name = (form.get("name") or [""])[0].strip()
            item = parse_channel(ch, name)
            if not item:
                self.send_html(configure_page("That channel link did not parse - it has to be a YouTube channel page (like youtube.com/@channelname), not a video link. Nothing saved."))
                return
            item = resolve_channel(item, keep_name=bool(name))
            save_channel(item)
            self.redirect("/configure")
            return
        if parsed.path == "/update":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            new_version, err = fetch_update()
            if err:
                self.redirect("/configure?msg=" + quote(err))
                return
            self.send_html(updated_page(new_version))
            try:
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True

            def _restart():
                time.sleep(0.5)
                os.execv(
                    sys.executable,
                    [sys.executable, os.path.abspath(__file__)] + sys.argv[1:],
                )

            threading.Thread(target=_restart, daemon=True).start()
            return
        if parsed.path == "/settings":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            quality = (form.get("quality") or [""])[0].strip()
            if quality not in QUALITY_VALUES:
                self.send_html(configure_page("That quality choice was not recognized - nothing changed."))
                return
            with LOCK:
                save_settings({"quality": quality})
            label = dict(QUALITY_LABELS)[quality]
            self.send_html(configure_page("YouTube quality saved: %s." % label))
            return
        self.send_json({"error": "not found"}, 404)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("My Magnet Links addon is running.")
    print("Configure page:  http://127.0.0.1:%d/configure" % PORT)
    print("Install in Nuvio: http://127.0.0.1:%d/manifest.json" % PORT)
    print("Leave this window open while you watch. Ctrl+C stops it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
