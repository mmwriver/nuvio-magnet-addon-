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
Live streams are unaffected by the merge.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

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
LIVE_TTL = 10 * 60
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
    "version": "1.5.0",
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


def resolve_youtube(item):
    """Ask yt-dlp for a direct stream URL for a YouTube item.

    Honors the quality setting (a height cap; "best" is uncapped).
    Client ladder, first success wins:
      1. android_vr client, capped format
      2. android_vr client, "best"
      3. default client, capped format
      4. default client, "best" (only when capped)
      5. tv client, capped format
      6. ios client, capped format
    android_vr goes first: on some networks/accounts it is the only
    client that offers a combined format at all, and walking the other
    clients first just burns seconds before it answers. (Some clients
    only hand out split audio/video formats, where no combined "best"
    exists; the ladder still walks them as a fallback.) Works for
    regular videos (direct file URL) and live streams (an .m3u8 URL).
    Results are cached in memory - 4 hours for regular streams, 10
    minutes for live manifests. Returns None on any failure.
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
    cookies = os.path.expanduser("~/Downloads/cookies.txt")
    base = [ytdlp, "-g", "--no-warnings"]
    if os.path.isfile(cookies):
        base += ["--cookies", cookies]

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
    unique_ladder = []
    for step in ladder:
        if step not in seen:
            seen.add(step)
            unique_ladder.append(step)
    for client, f in unique_ladder:
        url = _ytdlp_get(ytdlp, base, client_args(client), f, item["url"])
        if url:
            cache_resolve_put(vid, quality, url)
            return url
    return None


def fetch_youtube_title(url):
    """Best-effort real title for a YouTube video via yt-dlp, or None.
    Default client first, android_vr as the retry (the same pattern as
    youtube_info). Any failure just means the caller keeps the default
    name, so adding a video never breaks over a title lookup."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        return None
    cookies = os.path.expanduser("~/Downloads/cookies.txt")
    base = [ytdlp, "--no-warnings", "--print", "%(title)s"]
    if os.path.isfile(cookies):
        base += ["--cookies", cookies]
    attempts = [
        [],
        ["--extractor-args", "youtube:player_client=android_vr"],
    ]
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
    Default client first, android_vr as the retry."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        return None
    cookies = os.path.expanduser("~/Downloads/cookies.txt")
    base = [ytdlp, "-j", "--no-warnings"]
    if os.path.isfile(cookies):
        base += ["--cookies", cookies]
    attempts = [
        [],
        ["--extractor-args", "youtube:player_client=android_vr"],
    ]
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
a fallback. Live streams are unaffected.</p>""" % options
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

    def handle_play(self, raw_id, head_only=False):
        """Serve the HD merge for a saved YouTube item at /play/<id>.

        Live items and items without a mergeable format pair fall back
        to a redirect to the plain combined stream. The merge itself is
        ffmpeg joining the separate picture and sound streams into a
        fragmented MP4 on the fly - no seeking, close-delimited body.
        """
        item_id = unquote(raw_id or "")
        item = None
        with LOCK:
            for it in load_store(YT_STORE):
                if it["id"] == item_id or it["id"] == "youtube:" + item_id:
                    item = it
                    break
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
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except Exception:
            fallback()
            return
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

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/play/"):
            self.handle_play(parsed.path[len("/play/"):], head_only=True)
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
                streams = []
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
                resolved = resolve_youtube(item)
                if resolved:
                    streams.append(
                        {
                            "name": "YouTube",
                            "title": item["name"] + "\nYouTube stream",
                            "url": resolved,
                        }
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
