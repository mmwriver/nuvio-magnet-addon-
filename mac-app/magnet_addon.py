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
asks yt-dlp (which must be installed on this Mac) for a direct stream
address, so the first play can take a few seconds. A YouTube quality
setting on the web page caps how high that stream can go. With the
quality on 1080p or Best, regular YouTube videos also get an HD merge
stream: the addon joins YouTube's separate picture and sound streams
together with ffmpeg as it plays (starts slower, seeking is limited).
Live streams list only a relay stream: YouTube's live
playlists only hand out a short window of video at a time and expect
the player to keep re-asking for more, which some players don't keep
up with - playback stops when the window runs out. So for live items
the addon pulls the feed itself with ffmpeg and hands Nuvio one
continuous stream instead.

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

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, urljoin

PORT = int(os.environ.get("PORT", "7777"))
HERE = os.path.dirname(os.path.abspath(__file__))
_home = os.environ.get("MAGNET_ADDON_HOME")
DATA_DIR = _home if _home else HERE
if _home:
    os.makedirs(DATA_DIR, exist_ok=True)
STORE = os.path.join(DATA_DIR, "magnets.json")
LINK_STORE = os.path.join(DATA_DIR, "links.json")
YT_STORE = os.path.join(DATA_DIR, "youtube.json")
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
    "version": "1.5.3",
    "name": "My Magnet Links",
    "description": "Your own pasted magnet links, web video links and YouTube videos, playable in Nuvio.",
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


_cookies_cache = {"done": False, "path": None}


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
    non-empty file wins. The result is cached for the process; pass
    refresh=True to search again (callers do that once when a fetch
    fails, in case the file was exported in the meantime)."""
    if _cookies_cache["done"] and not refresh:
        return _cookies_cache["path"]
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
    _cookies_cache["done"] = True
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
    """Pick the (video, audio) playlist pair out of a live master
    playlist, or None. Fetches the playlist and scans its
    #EXT-X-STREAM-INF entries for the highest-bandwidth one whose
    video codec is avc1 (H.264) - the relay copies the stream into an
    MP4 without re-encoding, and a VP9/AV1 variant would produce a
    file some players can't handle.

    The variant playlist alone is NOT enough: YouTube's variant
    playlists carry video only, and the sound lives in a separate
    #EXT-X-MEDIA audio rendition (the group the variant's AUDIO
    attribute names). So the return is (variant_url, audio_url) -
    audio_url is the group's DEFAULT rendition (or its first), or
    None when the master names no audio group. ffmpeg follows both
    playlists itself, reloading them as new segments appear, which is
    the whole point of the relay. Any failure - fetch error, no avc1
    variant - returns None and the caller uses the master URL
    itself."""
    try:
        req = urllib.request.Request(
            master_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read(262144).decode("utf-8", "replace")
    except Exception:
        return None
    lines = text.splitlines()
    audio_groups = {}  # group id -> [(is_default, url), ...]
    for line in lines:
        line = line.strip()
        if not line.startswith("#EXT-X-MEDIA:") or "TYPE=AUDIO" not in line:
            continue
        gm = re.search(r'GROUP-ID="([^"]+)"', line)
        um = re.search(r'URI="([^"]+)"', line)
        if gm and um:
            audio_groups.setdefault(gm.group(1), []).append(
                ("DEFAULT=YES" in line, urljoin(master_url, um.group(1)))
            )
    best = None  # (bandwidth, variant_url, audio_group)
    for i, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
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
                best = (bw, urljoin(master_url, nxt), group)
            break
    if not best:
        return None
    audio_url = None
    renditions = audio_groups.get(best[2] or "", [])
    if renditions:
        audio_url = next(
            (u for is_default, u in renditions if is_default), renditions[0][1]
        )
    return (best[1], audio_url)


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
    retried without cookies (exported cookies go stale)."""
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


def find_youtube_item(raw_id):
    """The saved YouTube item for a /play or /live path tail - accepts
    the full "youtube:..." id or the bare video id. None if not saved."""
    item_id = unquote(raw_id or "")
    with LOCK:
        for it in load_store(YT_STORE):
            if it["id"] == item_id or it["id"] == "youtube:" + item_id:
                return it
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


def configure_page(msg=""):
    with LOCK:
        magnets = load_store(STORE)
        links = load_store(LINK_STORE)
        youtubes = load_store(YT_STORE)
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
<p style="color:#999">With quality on 1080p or Best, regular YouTube
videos also get an <b>HD merge</b> stream listed first: the app joins
YouTube's separate picture and sound streams together as it plays. It
starts slower and seeking is limited; the plain stream stays listed as
a fallback.</p>
<p style="color:#999">Live items get a <b>Live relay</b> stream:
the app pulls the live feed itself and hands it to Nuvio as one
continuous stream.</p>""" % options
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
    for it in magnets:
        rows += (
            '<div style="padding:10px 0;border-bottom:1px solid #333">'
            "<b>%s</b><br><small style='color:#999'>%s</small><br>"
            '<a href="/delete?id=%s" style="color:#e66">Delete</a></div>'
            % (esc(it["name"]), esc(it["id"]), esc(it["id"]))
        )
    if not magnets and not links and not youtubes:
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
<label><b>Paste any link</b> - magnet, web video address, or YouTube.
This box sorts out which kind it is.</label>
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
<h3>Saved</h3>%s
</body></html>""" % (PORT, note, quality_form, rows)
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
        # Feed ffmpeg the best H.264 variant plus its audio rendition
        # (two playlists - the variant alone carries no sound) rather
        # than the master itself, so the -c copy into MP4 stays H.264;
        # if no variant qualifies (or the playlist can't be read), the
        # master goes in and ffmpeg picks on its own. The audio in
        # these playlists is ADTS, which an MP4 can't hold as-is - the
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
            vurl, aurl = picked
            cmd += ["-i", vurl]
            if aurl:
                cmd += reconnect + ["-i", aurl]
                amap = "1:a:0"
            else:
                amap = "0:a:0?"
            cmd += ["-map", "0:v:0", "-map", amap]
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

    def do_HEAD(self):
        parsed = urlparse(self.path)
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
            self.send_html(configure_page())
            return
        if path.startswith("/play/"):
            self.handle_play(path[len("/play/"):])
            return
        if path.startswith("/live/"):
            self.handle_live(path[len("/live/"):])
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
            metas = [meta_of(i) for i in items if not search or search in i["name"].lower()]
            self.send_json({"metas": metas})
            return
        m = re.match(r"^/meta/movie/(.+)\.json$", path)
        if m:
            item = find_item(m.group(1))
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
                self.send_json({"streams": streams})
                return
            if item.get("kind") == "youtube":
                resolved = resolve_youtube(item)
                plain = None
                if resolved:
                    plain = {
                        "name": "YouTube",
                        "title": item["name"] + "\nYouTube stream",
                        "url": resolved,
                    }
                streams = []
                if plain and _is_hls_url(resolved):
                    # Live: only the relay. The addon follows the
                    # live playlist itself and feeds the player one
                    # continuous stream - a player handed the
                    # playlist directly can run out of segments and
                    # stop, so the direct stream is not listed at
                    # all. The HD merge doesn't apply to live either.
                    streams.append(
                        {
                            "name": "YouTube Live (relay)",
                            "title": item["name"]
                            + "\nLive relay — app feeds the stream, plays continuously",
                            "url": "http://127.0.0.1:%d/live/%s"
                            % (PORT, item["id"]),
                        }
                    )
                else:
                    if load_settings()["quality"] in ("1080", "best"):
                        streams.append(
                            {
                                "name": "YouTube HD (merge)",
                                "title": item["name"]
                                + "\n1080p merge — app joins picture+sound live",
                                "url": "http://127.0.0.1:%d/play/%s"
                                % (PORT, item["id"]),
                            }
                        )
                    if plain:
                        streams.append(plain)
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
            self.send_html(configure_page("That link did not parse - paste a magnet link, a web video address, or a YouTube link. Nothing saved."))
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
