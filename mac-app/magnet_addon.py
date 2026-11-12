#!/usr/bin/env python3
"""
StreamSearch - a tiny addon for Nuvio (Stremio-compatible).

Paste magnet links on its web page and they appear inside Nuvio as a
catalog called "StreamSearch". Pick one and it plays as a P2P
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
YouTube item lists a single stream served by the addon itself, no
direct links: for regular videos at quality settings of 720p or
lower, when YouTube offers the video as one combined file, pressing
play hands the player that plain file (the addon steps aside and
lets the player fetch it), so fast-forward and seeking work from
the very first play. At higher settings YouTube only offers
separate picture and sound streams, so the addon joins them
together with ffmpeg as it plays (starts slower, and that first
play can't be scrubbed - but the addon starts building a seekable
copy of the video on disk the moment you open it, before you even
press play, so the copy may already be ready for that first play,
and from the next play on the same video is served as a plain
file and fast-forward and seeking work). Live streams list only a relay stream: YouTube's live
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

Searching the StreamSearch catalog in Nuvio also searches
YouTube: alongside any saved items that match, the addon asks
YouTube for videos matching the search words and lists those too,
with their real titles and pictures. They play through the same
relay streams as pasted YouTube videos. Search results are only
remembered while the addon runs - they are never saved.

Channels that turn up in a search show as their own item too:
open one and the addon lists that channel's videos (newest
first, up to fifty), with the channel's live video pinned at
the top when it is live - tap any video in the list to play it
through the same relays. Channel items show the channel's own
logo (its YouTube avatar) as their picture once the addon has
fetched it - a channel may briefly show a video picture the
first time it appears. Logos are remembered in
channel_logos.json next to the other saved lists, so each
channel's logo is fetched once, not on every start.

Searching also finds magnet links: matching releases from a
public torrent index appear in the results between the saved
items and the YouTube videos, each showing its seeders and size.
They play peer-to-peer exactly like pasted magnets (VPN on).
Like the YouTube results, they are remembered in memory only -
never saved.

Any saved item can also be tagged with a show or movie: type its
title and the app looks the IMDb code (tt.......) up on Cinemeta by
itself, or give the code / an IMDb title page address directly -
tt.......:season:episode, or a title with a trailing 1:2 / S1E2,
tags one episode of a series. A tagged item
also shows up under that show's own sources in Nuvio: search the
show on Nuvio's main page, open it, and the saved item is listed
with the rest of the sources. Untagged items work as before, in the
StreamSearch catalog only. Under a show's sources the addon
only ever serves links that were saved in it - it does not go
looking for sources on its own.

YouTube fetches use a browser cookies file when one is around - the
addon finds it by itself in Downloads, including the file Opera's
cookies extension saves in Downloads/Temp. When there is no file,
or the exported cookies have gone stale (yt-dlp's own error says a
sign-in is needed), playback resolution reads the sign-in straight
out of a browser he is already logged into - Opera GX first - so
the file rarely needs re-exporting at all; and if that fails too
it retries without cookies rather than failing.

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
RELAY_CACHE_DIR = os.path.join(DATA_DIR, "relay-cache")

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
# written to disk). Repeat plays skip the yt-dlp call entirely
# while an entry is fresh. The TTLs are deliberately short-ish:
# these are signed URLs, and a cached one that outlives its
# signature fails instantly with no retry - half an hour is long
# enough to cover every reopen inside a viewing session without
# ever handing out a stale address. HLS manifest URLs for live
# streams rotate much faster, so those get two minutes. The
# /play/ merge pair (separate picture + sound URLs) is cached
# the same way; a pair is only ever cached for a non-live item,
# so a cache hit also settles the live check.
RESOLVE_TTL = 30 * 60
LIVE_TTL = 120
PAIR_TTL = 30 * 60
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


# Progressive first play (v1.8.10): at quality settings of 720p
# or lower, YouTube offers most videos as ONE combined MP4 file
# (picture and sound together). A plain file like that, fetched
# by the player straight from its host with HTTP ranges, is the
# one stream shape the player has always been able to scrub -
# the relay-cache files have played that way since v1.7.3. So
# when such a file fits the quality cap, /play/ simply redirects
# the player to it and seeking works from the first play; the
# merge relay below stays for the higher settings, where
# YouTube only serves split streams. (Two fancier first-play
# relay shapes were tried and the player would not play either:
# a playlist of cut segments in v1.8.8, a DASH manifest in
# v1.8.9. Neither is in this build.) The picked file's signed
# address is cached like the merge pair's, on the same TTL -
# signed addresses go stale, and a repeat play inside a viewing
# session should not pay another yt-dlp round-trip.
PROGRESSIVE_MAX_HEIGHT = 720
_prog_cache = {}  # (video_id, cap) -> (url, timestamp)


def cache_prog_get(video_id, cap):
    with CACHE_LOCK:
        hit = _prog_cache.get((video_id, cap))
    if not hit:
        return None
    url, ts = hit
    if time.time() - ts < PAIR_TTL:
        return url
    return None


def cache_prog_put(video_id, cap, url):
    with CACHE_LOCK:
        _prog_cache[(video_id, cap)] = (url, time.time())


# Prefetch: when a channel's series meta is opened, the first few
# uploads are what gets clicked next, so their merge pairs are
# resolved ahead of time - one daemon thread working through them
# sequentially - into the pair cache above. A first click on any
# of them then skips the yt-dlp -j round-trip entirely. Live
# videos are skipped: they never take the pair path.
PREFETCH_MAX = 5
_prefetching = set()  # video ids with a prefetch in flight


def _prefetch_pair(vid, cap):
    """Resolve one video's merge pair into the pair cache - the
    same work handle_play does on a cold click, done early."""
    try:
        info = youtube_info(synth_youtube_item(vid))
        if info is None or info.get("is_live"):
            return
        picked = pick_merge_pair(info, cap)
        if picked:
            cache_pair_put(vid, cap, picked[0]["url"], picked[1]["url"])
    except Exception:
        pass
    finally:
        with CACHE_LOCK:
            _prefetching.discard(vid)


def prefetch_pairs(videos):
    """Kick off background pair resolution for the first
    PREFETCH_MAX non-live videos of a channel listing. Videos
    already pair-cached, already being prefetched, or holding a
    finished seek-cache file are skipped. Never blocks, never
    raises."""
    try:
        quality = load_settings()["quality"]
        cap = int(quality) if quality.isdigit() else 1080
        todo = []
        for v in videos:
            if len(todo) >= PREFETCH_MAX:
                break
            if not isinstance(v, dict) or v.get("is_live"):
                continue
            vid = str(v.get("id") or "")
            if len(vid) != 11:
                continue
            if cache_pair_get(vid, cap):
                continue
            if relay_cache_ready(vid, quality):
                continue
            with CACHE_LOCK:
                if vid in _prefetching:
                    continue
                _prefetching.add(vid)
            todo.append(vid)
        if not todo:
            return

        def run():
            for vid in todo:
                _prefetch_pair(vid, cap)

        threading.Thread(target=run, daemon=True).start()
    except Exception:
        pass


# Seekable VOD cache: /play/ pipes ffmpeg's output live (chunked,
# no Content-Length, no ranges), so a player treats it as a live
# pipe and won't scrub it. While a video plays through that pipe,
# the addon also merges the same pair into a plain faststart MP4
# under relay-cache/ in the data home; from the next play on,
# /play/ serves that file with full Range support and seeking
# works. Live items never enter the cache - /live/ is unchanged.
RELAY_CACHE_MAX = 5
_cache_building = set()  # (video_id, quality) merges in flight


def relay_cache_path(video_id, quality):
    return os.path.join(RELAY_CACHE_DIR, "%s-%s.mp4" % (video_id, quality))


def relay_cache_ready(video_id, quality):
    """The finished cache file for a video+quality, or None. A
    file only reaches its final name via an atomic rename after a
    complete merge, so a file that exists is always whole."""
    path = relay_cache_path(video_id, quality)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    except OSError:
        pass
    return None


def prune_relay_cache():
    """Keep at most RELAY_CACHE_MAX cache files, least recently
    used out first. "Used" is the file's mtime - serving a file
    refreshes it - and a file touched within the last minute is
    never removed, so the file being served right now survives."""
    try:
        names = os.listdir(RELAY_CACHE_DIR)
    except OSError:
        return
    entries = []
    for name in names:
        if not name.endswith(".mp4"):
            continue
        path = os.path.join(RELAY_CACHE_DIR, name)
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            continue
    entries.sort()
    excess = len(entries) - RELAY_CACHE_MAX
    now = time.time()
    for mtime, path in entries:
        if excess <= 0:
            break
        if now - mtime < 60:
            continue
        try:
            os.remove(path)
            excess -= 1
        except OSError:
            pass


# ffmpeg input-probing trim for the two VOD merge commands (the
# /play/ pipe and the cache-file build below). Their inputs are
# direct googlevideo MP4/M4A addresses whose headers describe the
# streams completely, so ffmpeg's default probing (5 MB /
# 5 seconds per input) only delays the first frame. 1 MB / 1 s is
# still generous for a header read. The live relay is NOT trimmed:
# its HLS inputs must be probed as-is - that path is proven
# exactly as it stands.
PROBE_TRIM = ["-probesize", "1000000", "-analyzeduration", "1000000"]


def _build_cache_file(video_id, quality, video_url, audio_url):
    """Background merge of one video's pair into its cache file:
    ffmpeg stream-copy into a plain MP4 with +faststart (the moov
    atom up front is what makes a served file seekable), then an
    atomic rename into place and a prune. Any failure removes the
    temp file and is only logged - the /play/ pipe stays the
    permanent fallback for that video."""
    key = (video_id, quality)
    final = relay_cache_path(video_id, quality)
    tmp = final + ".part"
    try:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return
        os.makedirs(RELAY_CACHE_DIR, exist_ok=True)
        cmd = [
            ffmpeg,
            "-v",
            "error",
            *PROBE_TRIM,
            "-i",
            video_url,
            *PROBE_TRIM,
            "-i",
            audio_url,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-y",
            tmp,
        ]
        done = subprocess.run(cmd, capture_output=True, timeout=3600)
        ok = False
        if done.returncode == 0:
            try:
                ok = os.path.getsize(tmp) > 0
            except OSError:
                ok = False
        if ok:
            os.replace(tmp, final)
            prune_relay_cache()
        else:
            tail = (done.stderr or b"").decode("utf-8", "replace")[-200:]
            print(
                "relay cache: merge failed for %s (%s)"
                % (video_id, tail.strip() or "no output file")
            )
            try:
                os.remove(tmp)
            except OSError:
                pass
    except Exception as e:
        print("relay cache: merge failed for %s (%s)" % (video_id, e))
        try:
            os.remove(tmp)
        except OSError:
            pass
    finally:
        with CACHE_LOCK:
            _cache_building.discard(key)


def start_cache_merge(video_id, quality, video_url, audio_url):
    """Kick off the background cache merge for a video+quality,
    unless the finished file already exists or a merge for it is
    already running. Never blocks the caller."""
    if relay_cache_ready(video_id, quality):
        return
    key = (video_id, quality)
    with CACHE_LOCK:
        if key in _cache_building:
            return
        _cache_building.add(key)
    threading.Thread(
        target=_build_cache_file,
        args=(video_id, quality, video_url, audio_url),
        daemon=True,
    ).start()


# Pre-build (v1.8.12): the cache merge above used to start only
# when a video was already playing, so the seekable copy could
# not exist before the second play. Now opening a video - its
# meta page or its stream list, the two requests Nuvio makes for
# one video at a time - starts the same build right away, so the
# copy may already be finished when play is pressed (and that
# first press is then served the file, with seeking). List
# requests (catalog, search, channel and subscription listings)
# never trigger this: they name many videos at once, and
# building for all of them would flood the connection. The work
# itself is unchanged - the same pair picking /play/ does, the
# same start_cache_merge - it just starts earlier. Live items
# never pre-build, and at quality settings where /play/ hands
# the player a single combined file instead (720p and under,
# when YouTube offers one) there is nothing to build: that file
# already seeks from the first play.
_prebuilding = set()  # (video_id, quality) resolutions in flight


def maybe_prebuild_cache(item):
    """Start the seekable-copy build for one opened YouTube
    video, ahead of any play press. Cheap guards only - the
    finished file, a merge already running (started by a play
    or an earlier open) and a resolution already in flight all
    answer no - then the real work moves to a daemon thread, so
    the request being answered never waits on it."""
    try:
        vid = youtube_video_id(item)
    except Exception:
        return
    if not vid or len(vid) != 11:
        return
    quality = load_settings()["quality"]
    if relay_cache_ready(vid, quality):
        return
    hit = search_hit(vid)
    if hit and hit.get("is_live"):
        return
    key = (vid, quality)
    with CACHE_LOCK:
        if key in _cache_building or key in _prebuilding:
            return
        _prebuilding.add(key)
    threading.Thread(
        target=_prebuild_cache,
        args=(item, vid, quality),
        daemon=True,
    ).start()


def _prebuild_cache(item, vid, quality):
    """Resolve one video's merge pair exactly the way
    handle_play would - same caches first, same live check,
    same combined-file detour at 720p and under - and hand it
    to start_cache_merge. Any failure just means no head
    start: pressing play behaves exactly as before."""
    key = (vid, quality)
    try:
        cap = int(quality) if quality.isdigit() else 1080
        if cap <= PROGRESSIVE_MAX_HEIGHT:
            # /play/ would hand the player a combined file for
            # this video if YouTube offers one - that path
            # already seeks, so there is nothing to build.
            # Only when no combined file fits does /play/ fall
            # back to the merge, and only then is a head start
            # worth anything.
            if cache_prog_get(vid, cap):
                return
            info = youtube_info(item)
            if info is None or info.get("is_live"):
                return
            picked_prog = pick_progressive_format(info, cap)
            if picked_prog is None:
                alt = _progressive_info(item)
                if alt is not None:
                    if alt.get("is_live"):
                        return
                    picked_prog = pick_progressive_format(alt, cap)
            if picked_prog:
                cache_prog_put(vid, cap, picked_prog["url"])
                return
            picked = pick_merge_pair(info, cap)
            if picked:
                cache_pair_put(
                    vid, cap, picked[0]["url"], picked[1]["url"])
                start_cache_merge(
                    vid, quality, picked[0]["url"], picked[1]["url"])
            return
        cached_pair = cache_pair_get(vid, cap)
        if cached_pair:
            start_cache_merge(
                vid, quality, cached_pair[0], cached_pair[1])
            return
        info = youtube_info(item)
        if info is None or info.get("is_live"):
            return
        picked = pick_merge_pair(info, cap)
        if picked:
            cache_pair_put(
                vid, cap, picked[0]["url"], picked[1]["url"])
            start_cache_merge(
                vid, quality, picked[0]["url"], picked[1]["url"])
    except Exception:
        pass
    finally:
        with CACHE_LOCK:
            _prebuilding.discard(key)


DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
]

MANIFEST = {
    "id": "community.mymagnetlinks",
    "version": "1.8.12",
    "name": "StreamSearch",
    "description": "Your own pasted magnet links, web video links, YouTube videos and channels, playable in Nuvio. Searching the catalog also searches YouTube.",
    "logo": "",
    # Detailed resources: catalog and meta only cover this addon's
    # own items (its own id prefixes). Catalog and meta also cover
    # the series type for "ytchannel:" items - a YouTube channel
    # found through catalog search opens as a series whose videos
    # are the channel's uploads. The stream resource additionally
    # covers series and IMDb "tt" ids, so a saved item tagged with
    # a show/movie code can be served under that show's sources
    # when Nuvio asks for streams by its tt id.
    "resources": [
        {
            "name": "catalog",
            "types": ["movie", "series"],
            "idPrefixes": ["magnet:", "link:", "youtube:", "ytchannel:"],
        },
        {
            "name": "meta",
            "types": ["movie", "series"],
            "idPrefixes": ["magnet:", "link:", "youtube:", "ytchannel:"],
        },
        {
            "name": "stream",
            "types": ["movie", "series"],
            "idPrefixes": ["magnet:", "link:", "youtube:", "tt"],
        },
    ],
    "types": ["movie", "series"],
    "idPrefixes": ["magnet:", "link:", "youtube:", "ytchannel:", "tt"],
    "catalogs": [
        {
            "type": "movie",
            "id": "mymagnets",
            "name": "StreamSearch",
            "extra": [{"name": "search", "isRequired": False}],
        },
        {
            "type": "series",
            "id": "subscriptions",
            "name": "Subscriptions",
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


ATTACH_RE = re.compile(r"^(tt\d{7,8})(?::(\d{1,3}):(\d{1,3}))?$")
IMDB_URL_RE = re.compile(r"imdb\.com/title/(tt\d{7,8})", re.IGNORECASE)


def parse_attach(value):
    """Normalize a show/movie tag (an item's "attach" field), or
    None when the value is not a usable tag - the item still saves,
    just untagged. Accepts the bare code "tt1234567" (7-8 digits),
    the episode form "tt1234567:1:2" (season:episode, normalized to
    plain numbers), and a full IMDb title page address
    (https://www.imdb.com/title/tt1234567/...), which normalizes to
    the bare code. Everything is lowercased. The same normalization
    is applied to ids Nuvio asks streams for, so a stored tag and a
    requested id meet in the same shape."""
    v = (value or "").strip().lower()
    if not v:
        return None
    m = IMDB_URL_RE.search(v)
    if m:
        return m.group(1)
    m = ATTACH_RE.match(v)
    if not m:
        return None
    if m.group(2) is not None:
        return "%s:%d:%d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    return m.group(1)


EP_MARKER_RES = (
    re.compile(r"^(.*?)\s+(\d{1,3}):(\d{1,3})$"),
    re.compile(r"^(.*?)\s*[Ss](\d{1,3})[Ee](\d{1,3})$"),
)
YEAR_MARKER_RE = re.compile(r"^(.*?)\s+((?:19|20)\d{2})$")
CINEMETA_BASE = "https://v3-cinemeta.strem.io"


def _cinemeta_search(kind, title):
    """One Cinemeta catalog search ("series" or "movie") for a typed
    title: a list of {"id", "name", "year"} for the results whose id
    is an IMDb tt id, in the order Cinemeta returned them. Any
    failure - no network, bad JSON, unexpected shape - is an empty
    list, so a title lookup can never break saving an item."""
    url = "%s/catalog/%s/top/search=%s.json" % (
        CINEMETA_BASE, kind, quote(title, safe=""))
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []
    metas = data.get("metas") if isinstance(data, dict) else None
    out = []
    for meta in metas or []:
        if not isinstance(meta, dict):
            continue
        mid = str(meta.get("id") or "")
        if not mid.startswith("tt"):
            continue
        year = None
        for key in ("releaseInfo", "year", "released"):
            ym = re.search(r"(?:19|20)\d{2}", str(meta.get(key) or ""))
            if ym:
                year = ym.group(0)
                break
        out.append({
            "id": mid,
            "name": (meta.get("name") or "").strip(),
            "year": year,
        })
    return out


def lookup_title(title, year=None):
    """Look a typed show/movie title up on Cinemeta and return
    (tt id, display name) for the best match, or None. Cinemeta is
    the catalog whose ids Nuvio's main-page search uses, so its ids
    are the ones a tag has to carry. Pick order, series and movie
    results together: an exact name match (ignoring case) wins -
    among exact matches, one whose year matches the year the user
    typed wins - and when nothing matches exactly, the first
    result, series before movie, whose name actually relates to
    what was typed: the typed text sits inside the result name
    (a partly typed title), or the result name sits inside the
    typed text with only a few characters to spare (a title typed
    with a stray mark or word). Whether a typed title is a series
    or a movie is not knowable up front, so series is simply
    checked first in both steps.
    The relation check on the fallback matters: Cinemeta fuzzy-
    matches single words, so gibberish can come back with a random
    real title that shares one word - a fallback pick with no real
    relation to the typed text is no match at all. The display
    name carries the found year when there is one, so the Saved
    list can show what the item was tagged as."""
    series = _cinemeta_search("series", title)
    movies = _cinemeta_search("movie", title)
    wanted = title.strip().lower()
    exacts = [m for m in series + movies if m["name"].lower() == wanted]
    chosen = None
    if exacts:
        if year:
            for m in exacts:
                if m["year"] == year:
                    chosen = m
                    break
        if chosen is None:
            chosen = exacts[0]
    else:
        for m in series + movies:
            name = m["name"].lower()
            if name and (
                wanted in name
                or (name in wanted and len(wanted) - len(name) <= 4)
            ):
                chosen = m
                break
    if chosen is None:
        return None
    display = chosen["name"]
    if chosen["year"]:
        display = "%s (%s)" % (display, chosen["year"])
    return chosen["id"], display


def resolve_attach(value):
    """Resolve whatever a save form's tag box holds to an item's
    (attach, attach_title) pair; (None, None) when it does not
    resolve - the item still saves, untagged, as an invalid code
    always did. A tt code, an episode-form code or an IMDb title
    address goes through parse_attach() directly and carries no
    attach_title. Anything else is treated as a typed title: a
    trailing episode marker ("Title 1:2" or "Title S1E2") is split
    off first, then a trailing year ("Title 2020") is split off to
    prefer a same-year match, and the title is looked up on
    Cinemeta (lookup_title) - the found tt id gets the episode
    marker appended back, and the found name becomes attach_title
    so the Saved list shows what the item was tagged as."""
    direct = parse_attach(value)
    if direct:
        return direct, None
    text = (value or "").strip()
    if not text:
        return None, None
    season = episode = None
    for rx in EP_MARKER_RES:
        m = rx.match(text)
        if m:
            season, episode = int(m.group(2)), int(m.group(3))
            text = m.group(1).strip()
            break
    year = None
    ym = YEAR_MARKER_RE.match(text)
    if ym:
        year = ym.group(2)
        text = ym.group(1).strip()
    if not text:
        return None, None
    found = lookup_title(text, year)
    if not found:
        return None, None
    tt_id, display = found
    if season is not None:
        tt_id = "%s:%d:%d" % (tt_id, season, episode)
    return tt_id, display


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


# Set whenever a -g attempt fails and yt-dlp's own error text
# blames the sign-in ("Login details are needed", "Sign in to
# confirm", a cookies complaint) - the tell that an exported
# cookies file has gone stale. _resolve_ladder resets it per
# run and resolve_youtube reads it to decide whether the
# browser-cookie fallback gets a turn.
_login_hint = {"seen": False}
_LOGIN_HINT_WORDS = ("login", "sign in", "sign-in", "cookies")


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
    if any(w in (out.stderr or "").lower() for w in _LOGIN_HINT_WORDS):
        _login_hint["seen"] = True
    return None


def youtube_video_id(item):
    """The bare 11-char video id for a YouTube item."""
    iid = item.get("id") or ""
    if iid.startswith("youtube:"):
        return iid[len("youtube:"):]
    m = YT_ID_RE.search(item.get("url") or "")
    return m.group(1) if m else iid


def _resolve_ladder(ytdlp, item_url, fmt, capped, cookies_path,
                    cookie_args=None):
    """Walk the client ladder once and return the first combined stream
    URL, or None. cookies_path may be None (fetch without cookies).
    cookie_args, when given, are used verbatim in place of
    --cookies <path> - the browser-cookie fallback passes
    --cookies-from-browser <spec> that way. Ladder order
    (android_vr first - see resolve_youtube):
      android_vr capped, android_vr "best", default capped,
      default "best" (only when capped), tv capped, ios capped."""
    _login_hint["seen"] = False
    base = [ytdlp, "-g", "--no-warnings"]
    if cookie_args:
        base += list(cookie_args)
    elif cookies_path:
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


# Which cookie source last got a video resolved - the file
# (detail = its path) or a browser (detail = its label) - for
# the configure page's status paragraph and subs-debug.log,
# the addon's one debug log. Playback resolution otherwise
# leaves no paper trail; this is what makes "the relay stopped
# working" diagnosable after the fact.
_resolve_source = {"kind": None, "detail": None, "at": 0.0}


def _note_resolve_source(kind, detail):
    """Remember the winning cookie source, and leave one line
    in subs-debug.log when the winner changes. Never raises -
    a logging failure must not break playback."""
    with CACHE_LOCK:
        changed = (
            _resolve_source["kind"] != kind
            or _resolve_source["detail"] != detail
        )
        _resolve_source.update(
            {"kind": kind, "detail": detail, "at": time.time()})
    if not changed:
        return
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "%s | resolve | cookies: %s | OK: resolved with %s\n" % (
            stamp, detail or "none", kind)
        with open(SUBS_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _resolve_source_html():
    """The sign-in sentence appended to the configure page's
    subscriptions status paragraph once a playback resolution
    has succeeded this session - which cookie source is
    working, so a stale export is visible at a glance. Empty
    until then, so the paragraph is unchanged on a fresh start."""
    with CACHE_LOCK:
        kind = _resolve_source["kind"]
        detail = _resolve_source["detail"]
    if kind == "browser":
        return (
            " YouTube sign-in for playback: %s browser - read "
            "live from the browser, no cookies file needed."
            % esc(detail))
    if kind == "file":
        return (
            " YouTube sign-in for playback: cookies file (%s)."
            % esc(detail))
    return ""


# Browser-cookie fallback (v1.8.7). The relay never changed;
# what breaks playback is YouTube resolution failing once the
# exported cookies.txt goes stale, and re-exporting it by hand
# every few weeks was the chore. So when the file cannot sign
# in - there is no file at all, or yt-dlp's own error says a
# sign-in is needed - resolve_youtube retries with yt-dlp
# reading the sign-in straight out of a browser he is already
# logged into (--cookies-from-browser). The winning browser
# is remembered for the session, so later resolves go straight
# to it after the file attempt; when every browser fails, that
# failure is remembered for a few minutes instead of being
# re-probed on every play. The file path still runs first and
# untouched whenever it works, and the bare no-cookies run
# stays the last resort.
_BROWSER_FAIL_TTL = 300.0
_browser_cookies = {"winner": None, "fail_until": 0.0}


def _browser_cookie_sources():
    """(label, --cookies-from-browser spec) pairs, best first.
    Opera GX keeps its profile in its own Application Support
    folder rather than Opera's, so it comes first with the
    folder spelled out; the plain browser keys follow for
    machines where one of those is the signed-in browser."""
    gx = os.path.join(
        os.path.expanduser("~"), "Library", "Application Support",
        "com.operasoftware.OperaGX")
    return [
        ("Opera GX", "opera:" + gx),
        ("Opera", "opera"),
        ("Chrome", "chrome"),
        ("Safari", "safari"),
    ]


def _browser_cookie_ladder(ytdlp, item_url, fmt, capped):
    """The client ladder with yt-dlp reading cookies from a
    browser, one browser at a time (the session's winner
    first). Returns the stream URL on the first success and
    remembers that browser; None when every browser fails -
    or failed within the last few minutes."""
    if time.time() < _browser_cookies["fail_until"]:
        return None
    sources = _browser_cookie_sources()
    winner = _browser_cookies["winner"]
    if winner:
        sources = [s for s in sources if s[1] == winner[1]] + \
                  [s for s in sources if s[1] != winner[1]]
    for label, spec in sources:
        url = _resolve_ladder(
            ytdlp, item_url, fmt, capped, None,
            ["--cookies-from-browser", spec])
        if url:
            _browser_cookies["winner"] = (label, spec)
            _note_resolve_source("browser", label)
            return url
    _browser_cookies["fail_until"] = \
        time.time() + _BROWSER_FAIL_TTL
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


def pick_live_variant_full(master_url):
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
    variant - returns None and the caller falls back.

    The _full form additionally returns, as a fifth element, a
    ready-made "mini master" playlist text holding ONLY the
    chosen variant and its audio rendition - their lines copied
    from this same fetch of the master, the rendition's URI
    absolutized - or None when it cannot be assembled (no audio
    rendition). handle_live serves it back to ffmpeg as a
    one-variant master for TS-layout broadcasts (see there)."""
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
            ("DEFAULT=YES" in line, url, audio_count, line)
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
                best = (bw, urljoin(master_url, nxt), group, v_ord, line)
            break
    if not best:
        return None
    audio_url = None
    audio_ord = None
    media_line = None
    renditions = audio_groups.get(best[2] or "", [])
    if renditions:
        chosen = next((r for r in renditions if r[0]), renditions[0])
        audio_url = chosen[1]
        if audio_url:
            audio_ord = chosen[2]
            media_line = chosen[3]
    mini_text = None
    if audio_url and media_line:
        media_abs = re.sub(
            r'URI="[^"]*"', 'URI="%s"' % audio_url, media_line
        )
        mini_text = (
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            "#EXT-X-INDEPENDENT-SEGMENTS\n%s\n%s\n%s\n"
            % (media_abs, best[4], best[1])
        )
    return (best[1], audio_url, best[3], audio_ord, mini_text)


def pick_live_variant(master_url):
    """pick_live_variant_full without the mini-master text - the
    (variant_url, audio_url, variant_ordinal, audio_ordinal) tuple
    the caller outside the relay's TS form uses."""
    full = pick_live_variant_full(master_url)
    if full is None:
        return None
    return full[:4]


_MINI_MASTERS = {}
_MINI_MASTER_TTL = 600.0


def stash_mini_master(text):
    """Keep a mini master playlist in memory for ffmpeg to fetch
    from this same server (the /minimaster/ route in do_GET);
    returns the token naming it. Entries live ten minutes - ffmpeg
    fetches the master once, when it starts, so that is ample."""
    now = time.time()
    for tok in [t for t, (ts, _) in list(_MINI_MASTERS.items())
                if now - ts > _MINI_MASTER_TTL]:
        del _MINI_MASTERS[tok]
    token = hashlib.sha1(
        ("%s|%s|%d" % (text, now, os.getpid())).encode("utf-8")
    ).hexdigest()[:16]
    _MINI_MASTERS[token] = (now, text)
    return token


def get_mini_master(token):
    entry = _MINI_MASTERS.get(token or "")
    if not entry:
        return None
    return entry[1]


def live_variant_layout(variant_url):
    """Classify a live variant playlist by its segment layout:
    True when it is an fMP4 playlist (its segments are named by an
    #EXT-X-MAP tag), False when it is a plain TS-segment playlist
    (no #EXT-X-MAP anywhere in it), None when the playlist cannot
    be fetched. Only the head of the playlist is read - the tag, if
    present, sits at the top. The relay picks its ffmpeg form by
    this layout: see handle_live."""
    try:
        req = urllib.request.Request(
            variant_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read(65536).decode("utf-8", "replace")
    except Exception:
        return None
    return "#EXT-X-MAP" in text


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
    turns up. If the file cannot sign in - there is no file, or
    yt-dlp's error says a sign-in is needed (the tell of a stale
    export) - the ladder retries with yt-dlp reading the sign-in
    from a browser (see _browser_cookie_ladder). If every attempt
    still fails, the ladder runs once more WITHOUT cookies before
    giving up - a stale file can fail every client while the
    same fetch succeeds with no cookies at all.

    Results are cached in memory - 30 minutes for regular streams,
    two minutes for live manifests. Returns None on any failure.
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
        login_hint = _login_hint["seen"]
        if not url:
            # The cookies file may have been exported (or moved) since
            # the cached lookup - search once more before falling back.
            fresh = find_cookies(refresh=True)
            if fresh and fresh != cookies_path:
                url = _resolve_ladder(ytdlp, item["url"], fmt, capped, fresh)
                login_hint = login_hint or _login_hint["seen"]
                if url:
                    cookies_path = fresh
        if url:
            _note_resolve_source("file", cookies_path)
        else:
            # The file could not sign in: read the sign-in from a
            # browser instead of making him re-export the file.
            # The no-cookies run stays last, exactly as before.
            if login_hint:
                url = _browser_cookie_ladder(
                    ytdlp, item["url"], fmt, capped)
            if not url:
                url = _resolve_ladder(ytdlp, item["url"], fmt, capped, None)
    else:
        # No cookies file at all: the browser sign-in gets its
        # turn before the bare no-cookies run.
        url = _browser_cookie_ladder(ytdlp, item["url"], fmt, capped)
        if not url:
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
    if ucid.startswith("UC"):
        note_channel_logo(
            ucid,
            _avatar_from_thumbs(data.get("thumbnails"),
                                square_only=True))
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


# YouTube search: searching the addon's own catalog in Nuvio also
# searches YouTube. Results live in memory only - the per-query
# lists for SEARCH_TTL (so repeating a search is instant) and
# every hit by video id in _yt_hits, which is what the meta and
# stream routes consult for ids that were never saved. Nothing
# here is ever written to a store. Channels in the same results
# are collected alongside (per-query lists in
# _channel_search_cache, every channel by id in _channel_hits)
# and surface as "ytchannel:" series items whose meta lists the
# channel's uploads.
SEARCH_TTL = 10 * 60
SEARCH_LIMIT = 10
_search_cache = {}  # normalized query -> ([hit, ...], timestamp)
_yt_hits = {}  # video id -> {"title": str, "is_live": bool}
_channel_search_cache = {}  # normalized query -> ([channel hit, ...], timestamp)
_channel_hits = {}  # channel UC id -> {"name": str, "poster": str or None}


def _yt_search_dump(query):
    """One yt-dlp flat dump of a YouTube search, or None on any
    failure. Cookies when a file is found, with the usual refresh
    + no-cookies retries (exported cookies go stale) - the same
    pattern as _channel_dump."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        return None

    def attempt(cookies_path):
        cmd = [
            ytdlp,
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
        ]
        if cookies_path:
            cmd += ["--cookies", cookies_path]
        cmd += ["--", "ytsearch%d:%s" % (SEARCH_LIMIT, query)]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=25
            )
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


def _channel_id_of(e):
    """The UC channel id a flat search entry points at, or None.
    Channel entries arrive in two shapes: _type "channel", and -
    from current yt-dlp - _type "url" with ie_key "YoutubeTab",
    the entry's own id the UC id and its url the /channel/ page.
    Either way the entry's id (or its /channel/ url) carries the
    UC id; a video entry's 11-char id never matches the pattern,
    so this cannot swallow a video."""
    cid = str(e.get("id") or "")
    if re.match(r"^UC[A-Za-z0-9_-]{22}$", cid):
        return cid
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})",
                  str(e.get("url") or ""))
    return m.group(1) if m else None


def _parse_channel_entry(e):
    """One channel hit {"id", "name", "poster"} from a flat
    search entry that points at a channel, or None when the
    entry has no UC id or no name. The poster is the entry's
    channel thumbnail when the entry carries one (the last
    thumbnail in the list is the largest); thumbnail urls can
    arrive protocol-relative ("//yt3...") and are made absolute."""
    cid = _channel_id_of(e)
    if not cid:
        return None
    name = (
        (e.get("channel") or e.get("title") or e.get("uploader") or "")
        .strip()
    )
    if not name:
        return None
    poster = None
    thumbs = e.get("thumbnails")
    if isinstance(thumbs, list):
        for t in reversed(thumbs):
            if isinstance(t, dict) and t.get("url"):
                poster = t["url"]
                break
    if not poster and e.get("thumbnail"):
        poster = e["thumbnail"]
    if poster and poster.startswith("//"):
        poster = "https:" + poster
    return {"id": cid, "name": name[:200], "poster": poster}


def youtube_search(query):
    """YouTube videos matching a catalog search, as hit dicts
    {"id", "title", "is_live"} - at most SEARCH_LIMIT, videos only
    (playlist entries are skipped, as is anything without an
    11-char video id). "is_upcoming" premieres count as not
    live: there is no feed to relay yet. Results are cached per
    query for SEARCH_TTL, and every hit is remembered in _yt_hits
    for the meta/stream routes. Any failure is an empty list - a
    failed YouTube search never breaks the catalog.

    Channel entries in the same results are collected on the
    same pass (see youtube_channel_search): the two per-query
    caches are always filled together, from the one dump."""
    key = " ".join((query or "").lower().split())
    if not key:
        return []
    with CACHE_LOCK:
        hit = _search_cache.get(key)
    if hit and time.time() - hit[1] < SEARCH_TTL:
        return hit[0]
    data = _yt_search_dump(key)
    results = []
    channels = []
    seen_channels = set()
    if data:
        for e in (data.get("entries") or []):
            if not isinstance(e, dict):
                continue
            if _channel_id_of(e):
                ch = _parse_channel_entry(e)
                if ch and ch["id"] not in seen_channels:
                    seen_channels.add(ch["id"])
                    channels.append(ch)
                continue
            if e.get("_type") in ("playlist", "multi_video"):
                continue
            vid = str(e.get("id") or "")
            if len(vid) != 11:
                continue
            if len(results) >= SEARCH_LIMIT:
                continue
            title = (e.get("title") or "").strip() or ("YouTube " + vid)
            results.append({
                "id": vid,
                "title": title[:200],
                "is_live": e.get("live_status") == "is_live",
            })
    with CACHE_LOCK:
        _search_cache[key] = (results, time.time())
        _channel_search_cache[key] = (channels, time.time())
        for r in results:
            _yt_hits[r["id"]] = {
                "title": r["title"],
                "is_live": r["is_live"],
            }
        for ch in channels:
            _channel_hits[ch["id"]] = {
                "name": ch["name"],
                "poster": ch["poster"],
            }
    for ch in channels:
        # A search entry's channel picture is the channel's
        # avatar itself - keep it as the channel's logo.
        note_channel_logo(ch["id"], ch["poster"])
    return results


def youtube_channel_search(query):
    """YouTube channels matching a catalog search, as hit dicts
    {"id", "name", "poster"} - the channel entries of the same
    flat results youtube_search parses, deduped by channel id.
    Served from the per-query cache youtube_search fills; a miss
    runs that search (one dump fills both caches). Any failure
    is an empty list."""
    key = " ".join((query or "").lower().split())
    if not key:
        return []
    with CACHE_LOCK:
        hit = _channel_search_cache.get(key)
    if hit and time.time() - hit[1] < SEARCH_TTL:
        return hit[0]
    youtube_search(key)
    with CACHE_LOCK:
        hit = _channel_search_cache.get(key)
    return hit[0] if hit else []


def meta_of_channel_hit(hit):
    """The catalog entry for one channel search hit: a series
    item named for the channel, with its picture when the
    search carried one. Opening it serves the channel's uploads
    as the series' videos (see channel_series_meta)."""
    meta = {
        "id": "ytchannel:" + hit["id"],
        "type": "series",
        "name": hit["name"],
    }
    poster = channel_poster(hit["id"], hit.get("poster"))
    if poster:
        meta["poster"] = poster
        meta["background"] = poster
    return meta


def search_hit(vid):
    """The remembered search hit for a video id, or None."""
    with CACHE_LOCK:
        hit = _yt_hits.get(vid)
    return dict(hit) if hit else None


def streams_for_search_hit(item):
    """The one-stream list for a YouTube item that came from a
    catalog search (never saved): exactly the shapes saved
    YouTube items get from streams_for_item - the live relay for
    a live video, the /play/ relay for a regular one - with the
    live flag taken from the search hit instead of a resolve.
    None when the id is not a remembered hit."""
    hit = search_hit(youtube_video_id(item))
    if not hit:
        return None
    if hit["is_live"]:
        return [
            {
                "name": "YouTube Live (relay)",
                "title": hit["title"]
                + "\nLive relay — app feeds the stream, plays continuously",
                "url": "http://127.0.0.1:%d/live/%s"
                % (PORT, item["id"]),
            }
        ]
    return [
        {
            "name": "YouTube (relay)",
            "title": hit["title"]
            + "\nApp-served stream — the app feeds the video",
            "url": "http://127.0.0.1:%d/play/%s"
            % (PORT, item["id"]),
        }
    ]


# Channel browsing: a channel found through catalog search opens
# as a series whose videos are that channel's uploads. Listings
# live in memory only, cached for CHANNEL_TTL like the saved
# channels' uploads.
YTCHANNEL_LIMIT = 50
_ytchannel_cache = {}  # channel UC id -> ((name, [video, ...]), timestamp)


def _entry_released(e):
    """The release date of a flat entry as an ISO string, or None
    when the entry carries no date (flat channel listings usually
    don't). Accepts yt-dlp's release_timestamp/timestamp epoch or
    a release_date in YYYYMMDD (or already-ISO) form."""
    ts = e.get("release_timestamp") or e.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime(ts))
    rd = str(e.get("release_date") or "")
    if re.match(r"^\d{8}$", rd):
        return "%s-%s-%sT00:00:00.000Z" % (rd[:4], rd[4:6], rd[6:8])
    if re.match(r"^\d{4}-\d{2}-\d{2}$", rd):
        return rd + "T00:00:00.000Z"
    return None


def fetch_ytchannel_listing(ucid):
    """A searched channel's uploads as (channel_name, [video, ...]),
    each video {"id", "title", "is_live", "released"} - newest
    first, at most YTCHANNEL_LIMIT. The channel's live video -
    the streams tab's first entry, when it is currently live -
    is pinned to the top and flagged live. None when the uploads
    fetch itself failed."""
    base = "https://www.youtube.com/channel/" + ucid
    data = _channel_dump(base + "/videos", YTCHANNEL_LIMIT, 25)
    if data is None:
        return None
    # The dump's top level carries the channel's own thumbnails
    # - its logo is in there at no extra fetch cost.
    if str(data.get("channel_id") or "") == ucid:
        note_channel_logo(
            ucid,
            _avatar_from_thumbs(data.get("thumbnails"),
                                square_only=True))
    name = (data.get("channel") or "").strip()
    videos = []
    seen = set()
    entries = data.get("entries")
    if isinstance(entries, list):
        for e in entries:
            if not isinstance(e, dict):
                continue
            vid = str(e.get("id") or "")
            if len(vid) != 11 or vid in seen:
                continue
            seen.add(vid)
            title = (e.get("title") or "").strip() or ("YouTube " + vid)
            videos.append({
                "id": vid,
                "title": title[:200],
                "is_live": False,
                "released": _entry_released(e),
            })
    live = None
    sdata = _channel_dump(base + "/streams", 3, 25)
    if sdata:
        sentries = sdata.get("entries")
        if isinstance(sentries, list) and sentries:
            first = sentries[0]
            if isinstance(first, dict) \
                    and first.get("live_status") == "is_live":
                lvid = str(first.get("id") or "")
                if len(lvid) == 11:
                    ltitle = (first.get("title") or "").strip() \
                        or ("YouTube " + lvid)
                    live = (lvid, ltitle[:200])
    if live:
        lvid, ltitle = live
        pinned = None
        rest = []
        for v in videos:
            if v["id"] == lvid:
                pinned = {
                    "id": lvid,
                    "title": ltitle,
                    "is_live": True,
                    "released": v["released"],
                }
            else:
                rest.append(v)
        if pinned is None:
            pinned = {
                "id": lvid,
                "title": ltitle,
                "is_live": True,
                "released": None,
            }
        videos = [pinned] + rest
    return (name, videos)


def ytchannel_listing(ucid):
    """Cache-aware (name, videos) for a searched channel. The
    listing repeats at most every CHANNEL_TTL; a failed refetch
    serves the last good listing, and no history at all gives
    (None, []) - the meta then just has an empty videos list."""
    with CACHE_LOCK:
        hit = _ytchannel_cache.get(ucid)
    if hit and time.time() - hit[1] < CHANNEL_TTL:
        return hit[0]
    fresh = fetch_ytchannel_listing(ucid)
    if fresh is not None:
        with CACHE_LOCK:
            _ytchannel_cache[ucid] = (fresh, time.time())
        return fresh
    return hit[0] if hit else (None, [])


def channel_series_meta(ucid):
    """The series meta for a "ytchannel:" item: the channel as a
    series whose videos are its uploads, newest first, the live
    video pinned at the top when the channel is live. Every
    listed video is registered in _yt_hits - the live one
    flagged live - so the stream route answers for these ids at
    once, in the same one-relay shapes as any other search hit.
    A failed fetch still returns the meta, with an empty
    videos list."""
    with CACHE_LOCK:
        known = _channel_hits.get(ucid) or {}
    name = known.get("name") or ""
    poster = channel_poster(ucid, known.get("poster"))
    videos = []
    if re.match(r"^UC[A-Za-z0-9_-]{22}$", ucid or ""):
        fetched_name, videos = ytchannel_listing(ucid)
        if fetched_name:
            name = fetched_name
    with CACHE_LOCK:
        for v in videos:
            _yt_hits[v["id"]] = {
                "title": v["title"],
                "is_live": v["is_live"],
            }
    # Opening a channel is the moment its first uploads are about
    # to be clicked - warm their merge pairs in the background.
    prefetch_pairs(videos)
    entries = []
    for i, v in enumerate(videos):
        entry = {
            "id": "youtube:" + v["id"],
            "title": v["title"],
            "season": 1,
            "episode": i + 1,
            "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg"
            % v["id"],
        }
        if v.get("released"):
            entry["released"] = v["released"]
        entries.append(entry)
    meta = {
        "id": "ytchannel:" + ucid,
        "type": "series",
        "name": name or "YouTube Channel",
        "videos": entries,
    }
    if poster:
        meta["poster"] = poster
        meta["background"] = poster
    return meta


# Channel logos: a channel's poster - in the Subscriptions
# catalog, in channel search results, and on the channel's own
# series page - is the channel's real logo (its YouTube avatar)
# once the addon knows it, not the first-video thumbnail the
# subscriptions feed falls back to. A logo is learned, cheapest
# first: from a search or channels-page entry that already
# carries the channel's avatar (yt3 addresses in a channel
# entry's thumbnails); from the top level of any channel dump
# the addon fetches anyway (opening a channel, resolving a
# saved one) - that list mixes the wide banner in with square
# avatar sizes, so only a square yt3 image counts there; or,
# for a channel neither source covered, from one background
# yt-dlp dump of the channel's videos tab. Resolved logos -
# and, briefly, failures - persist in channel_logos.json in
# the data folder, so the cost is paid once per channel, not
# once per run. Serving never waits on a fetch: a channel
# shows its fallback poster while its logo resolves in the
# background, and the logo takes over from the next serve on -
# the same stale-while-revalidate shape as the subscriptions
# sources below.
LOGO_TTL = 7 * 24 * 60 * 60
LOGO_FAIL_TTL = 6 * 60 * 60
LOGO_STORE = os.path.join(DATA_DIR, "channel_logos.json")
_logo_cache = None  # ucid -> {"logo": url or None, "ts": float}
_logo_lock = threading.Lock()
_logo_pending = set()  # ucids queued or being resolved
_logo_queue = []  # ucids waiting for the background worker
_logo_worker_on = False


def _load_logos():
    global _logo_cache
    with _logo_lock:
        if _logo_cache is None:
            try:
                with open(LOGO_STORE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _logo_cache = data if isinstance(data, dict) else {}
            except Exception:
                _logo_cache = {}
        return _logo_cache


def _save_logos():
    try:
        with open(LOGO_STORE, "w", encoding="utf-8") as f:
            json.dump(_logo_cache, f)
    except Exception:
        pass


def _is_avatar_url(url):
    """A channel avatar address: served from YouTube's yt3
    image host - never a video thumbnail (those live on
    i.ytimg.com) and never a banner crop (fcrop in the sizing
    parameters)."""
    u = str(url or "")
    return ("yt3.googleusercontent.com" in u
            or "yt3.ggpht.com" in u) and "fcrop" not in u


def _avatar_from_thumbs(thumbs, square_only=False):
    """The channel avatar in a thumbnails list, or None.
    Square sizes are the avatar; wide ones are the banner.
    Among sized squares the one closest to 176px wins - big
    enough for a poster, small to load. A dimensionless yt3
    address counts only when nothing sized exists and
    square_only is off: search and channels-page entries carry
    the avatar without dimensions and hold no banner, while a
    channel dump's top level mixes the banner in, so there
    only a proven square counts."""
    if not isinstance(thumbs, list):
        return None
    sized = []
    loose = None
    for t in thumbs:
        if not isinstance(t, dict):
            continue
        url = t.get("url")
        if not _is_avatar_url(url):
            continue
        w, h = t.get("width"), t.get("height")
        if isinstance(w, int) and isinstance(h, int) and w > 0:
            if w == h:
                sized.append((w, url))
        elif loose is None:
            loose = url
    if sized:
        sized.sort(key=lambda p: abs(p[0] - 176))
        return sized[0][1]
    if square_only:
        return None
    return loose


def note_channel_logo(ucid, url):
    """Record a channel logo learned from data the addon
    already had. Also refreshes the remembered channel hit, so
    a series meta picks the logo up even where it reads the
    remembered poster."""
    if not ucid or not _is_avatar_url(url):
        return
    logos = _load_logos()
    with _logo_lock:
        cur = logos.get(ucid)
        if cur and cur.get("logo") == url \
                and time.time() - cur.get("ts", 0) < LOGO_TTL:
            return
        logos[ucid] = {"logo": url, "ts": time.time()}
        _save_logos()
    with CACHE_LOCK:
        known = _channel_hits.get(ucid)
        if known is not None:
            known["poster"] = url


def _fetch_channel_logo(ucid):
    """One channel dump to read the avatar off its top level,
    or None. The videos tab at playlist-end 1 is the cheapest
    dump that still carries the channel's own fields."""
    data = _channel_dump(
        "https://www.youtube.com/channel/%s/videos" % ucid, 1, 30)
    if not data:
        return None
    if str(data.get("channel_id") or "") != ucid:
        return None
    return _avatar_from_thumbs(data.get("thumbnails"),
                               square_only=True)


def _logo_worker():
    global _logo_worker_on
    while True:
        with _logo_lock:
            if not _logo_queue:
                _logo_worker_on = False
                return
            ucid = _logo_queue.pop(0)
        try:
            url = _fetch_channel_logo(ucid)
        except Exception:
            url = None
        try:
            if url:
                note_channel_logo(ucid, url)
            else:
                logos = _load_logos()
                with _logo_lock:
                    cur = logos.get(ucid) or {}
                    if not cur.get("logo"):
                        logos[ucid] = {"logo": None,
                                       "ts": time.time()}
                        _save_logos()
        finally:
            with _logo_lock:
                _logo_pending.discard(ucid)


def _schedule_logo(ucid):
    """Queue a channel for background logo resolution - at
    most one fetch per channel per run, however often it is
    served while unresolved."""
    global _logo_worker_on
    start = False
    with _logo_lock:
        if ucid in _logo_pending:
            return
        _logo_pending.add(ucid)
        _logo_queue.append(ucid)
        if not _logo_worker_on:
            _logo_worker_on = True
            start = True
    if start:
        try:
            threading.Thread(target=_logo_worker,
                             daemon=True).start()
        except Exception:
            with _logo_lock:
                _logo_worker_on = False


def channel_logo(ucid):
    """The channel's known logo, or None. A fresh cache entry
    answers at once; a stale one is served while a background
    refresh runs; a miss schedules resolution and answers None
    (the caller falls back to its old poster). A failure is
    cached briefly, so a channel with no readable avatar is
    not re-fetched on every serve."""
    if not ucid:
        return None
    logos = _load_logos()
    with _logo_lock:
        entry = logos.get(ucid)
    if entry:
        age = time.time() - entry.get("ts", 0)
        ttl = LOGO_TTL if entry.get("logo") else LOGO_FAIL_TTL
        if age < ttl:
            return entry.get("logo")
        _schedule_logo(ucid)
        return entry.get("logo")
    _schedule_logo(ucid)
    return None


def channel_poster(ucid, fallback):
    """The poster a channel should show right now: its logo
    once known, otherwise the fallback the caller used before
    logos existed (a video thumbnail, or None)."""
    return channel_logo(ucid) or fallback


# Subscriptions: one extra catalog built from the signed-in
# YouTube account. "Subscriptions" (series) lists EVERY channel
# he follows - each one the same "ytchannel:" series a channel
# search produces, so opening it browses the channel's uploads
# exactly like v1.7.5; the clips load only once a channel is
# opened, never up front. The channel list merges three
# sources, in order: the channels page
# (youtube.com/feed/channels - the complete list of followed
# channels, in page order), any extra channel the
# subscriptions feed (yt-dlp's ":ytsubscriptions") surfaces
# that the page missed, and channels he saved by hand that
# neither source listed. Everything lives in memory only,
# cached for SUBS_TTL; any failure (no cookies, a rejected
# fetch, an empty page) yields an empty catalog and is logged,
# never raised - the rest of the addon is untouched by this
# feature.
#
# v1.8.4 bounded the fetch (measured on the old build: one
# cold catalog open took 243 seconds - each dump's 3-attempt
# cookies ladder at a 60s timeout, and the empty failure was
# not cached, so every open paid it again; Nuvio gives up long
# before that and the Subscriptions row simply vanishes).
# Now: each dump attempt is capped at SUBS_DUMP_TIMEOUT, the
# ladder is the cookies attempt plus at most ONE retry, the
# two dumps run side by side on a cold open, and a completed
# fetch is cached even when it came back empty or failed -
# later opens serve the cache at once for SUBS_TTL and
# refresh in the background, like the rest of the addon.
# Every fetch also leaves a plain-language line in
# subs-debug.log in the data folder, and the latest check is
# summarized on the configure page, so a silent empty list is
# diagnosable instead of guessable.
SUBS_TTL = 30 * 60
SUBS_FEED_LIMIT = 100
SUBS_PAGE_LIMIT = 500
SUBS_DUMP_TIMEOUT = 20
SUBS_DEBUG_LOG = os.path.join(DATA_DIR, "subs-debug.log")
_subs_cache = {}  # "feed" -> ((channels, videos), ts); "page" -> (channels, ts)
_subs_refreshing = set()  # "page" / "feed" refreshes in flight
# Last completed check per source, for the configure page:
# "page"/"feed" -> {"at": ts, "ok": bool, "count": int|None,
# "reason": str|None, "cookies": path|None}
_subs_status = {}


def _subs_debug(source, cookies_path, ok, detail):
    """One plain-language line per subscriptions fetch, in
    subs-debug.log in the data folder: when, which source,
    which cookies file was used (or none), and either the
    result count or the failure reason. Never raises - a
    logging failure must not break the catalog."""
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        cookies = cookies_path if cookies_path else "none found"
        outcome = "OK: %s" % detail if ok else "FAILED: %s" % detail
        line = "%s | %s | cookies: %s | %s\n" % (
            stamp, source, cookies, outcome)
        with open(SUBS_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _subs_record(source, cookies_path, ok, count=None, reason=None):
    """Remember the latest check for one source (for the
    configure page status paragraph) and write its debug-log
    line. count is the channel count on success."""
    with CACHE_LOCK:
        _subs_status[source] = {
            "at": time.time(),
            "ok": ok,
            "count": count,
            "reason": reason,
            "cookies": cookies_path,
        }
    if ok:
        detail = "%d channels" % (count or 0)
    else:
        detail = reason or "fetch failed"
    _subs_debug(source, cookies_path, ok, detail)


def _subs_status_html():
    """The configure page's ONE subscriptions status paragraph:
    when the last check ran, which cookies file was found, and
    what each source returned. Read-only - it never triggers a
    fetch, so the page itself stays fast."""
    with CACHE_LOCK:
        page = _subs_status.get("page")
        feed = _subs_status.get("feed")
    if not page and not feed:
        return (
            '<p style="color:#999"><b>Subscriptions:</b> not '
            "checked yet since the app started - open the "
            "Subscriptions list in Nuvio once and this fills in. "
            "Every check is also written to subs-debug.log in "
            "the app's data folder.%s</p>"
            % _resolve_source_html()
        )
    cookies_path = None
    latest = 0.0
    for st in (page, feed):
        if st:
            latest = max(latest, st["at"])
            if st.get("cookies"):
                cookies_path = st["cookies"]
    if cookies_path:
        cookies_txt = "cookies file found (%s)" % esc(cookies_path)
    else:
        cookies_txt = "no cookies file found"

    def describe(st):
        if not st:
            return "not checked yet"
        if st["ok"]:
            return "%d channels" % (st["count"] or 0)
        return "failed (%s)" % esc(st["reason"] or "fetch failed")

    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest))
    return (
        '<p style="color:#999"><b>Subscriptions:</b> last '
        "checked %s - %s. Channels page: %s. Subscriptions "
        "feed: %s. Full log: subs-debug.log in the app's data "
        "folder.%s</p>"
        % (esc(when), cookies_txt, describe(page), describe(feed),
           _resolve_source_html())
    )


def _subs_refresh_async(key):
    """Refresh one subscriptions source in the background - the
    revalidate half of the stale-while-revalidate pattern in
    followed_channels and subscriptions_feed. At most one
    refresh per source runs at a time; a failed refresh simply
    leaves the cached data in place."""
    with CACHE_LOCK:
        if key in _subs_refreshing:
            return
        _subs_refreshing.add(key)

    def run():
        try:
            if key == "page":
                fresh = fetch_followed_channels()
                if fresh is not None:
                    _store_followed(fresh)
            else:
                fresh = fetch_subscriptions()
                if fresh is not None:
                    _store_feed(fresh)
        except Exception:
            pass
        finally:
            with CACHE_LOCK:
                _subs_refreshing.discard(key)

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        with CACHE_LOCK:
            _subs_refreshing.discard(key)


def _subs_dump_attempt(ytdlp, target, limit, cookies_path):
    """One bounded yt-dlp flat dump attempt for a subscriptions
    source. Returns (data, None) on success or (None, reason)
    with a short plain-language failure reason - including
    yt-dlp's own first stderr line when it errors, so the debug
    log and configure page can say WHY, not just THAT."""
    cmd = [
        ytdlp,
        "--flat-playlist",
        "--playlist-end",
        str(limit),
        "--dump-single-json",
        "--no-warnings",
    ]
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    cmd += ["--", target]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=SUBS_DUMP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, "timed out after %ds" % SUBS_DUMP_TIMEOUT
    except Exception as e:
        return None, "could not run yt-dlp (%s)" % type(e).__name__
    if out.returncode != 0:
        first = ""
        for ln in (out.stderr or "").splitlines():
            if ln.strip():
                first = ln.strip()
                break
        if first:
            return None, "yt-dlp error: %s" % first[:200]
        return None, "yt-dlp failed (exit %d)" % out.returncode
    try:
        data = json.loads(out.stdout)
    except Exception:
        return None, "yt-dlp returned unreadable output"
    if not isinstance(data, dict):
        return None, "yt-dlp returned unreadable output"
    return data, None


def _subs_dump_run(target, limit):
    """A bounded subscriptions dump: the cookies attempt plus
    at most ONE retry (a refreshed cookies file when one shows
    up, otherwise a no-cookies attempt). Returns
    (data, cookies_path, reason): data is None on failure, with
    the last attempt's reason; cookies_path is the file
    find_cookies located (or None) for the log line. Worst
    case is 2 x SUBS_DUMP_TIMEOUT."""
    ytdlp = find_ytdlp()
    cookies_path = find_cookies()
    if not ytdlp:
        return None, cookies_path, "yt-dlp not found"
    data, reason = _subs_dump_attempt(
        ytdlp, target, limit, cookies_path)
    if data is None and cookies_path:
        fresh = find_cookies(refresh=True)
        if fresh and fresh != cookies_path:
            data, reason = _subs_dump_attempt(
                ytdlp, target, limit, fresh)
        else:
            data, reason = _subs_dump_attempt(
                ytdlp, target, limit, None)
    return data, cookies_path, reason


def _subs_dump():
    """One bounded yt-dlp flat dump of the subscriptions feed:
    (data, cookies_path, reason) - data None on any failure.
    The ladder is trimmed on purpose (v1.8.4): the old 3-attempt
    ladder at a 60s timeout let one catalog open hang for
    minutes on a slow connection."""
    return _subs_dump_run(":ytsubscriptions", SUBS_FEED_LIMIT)


def _channels_page_dump():
    """One bounded yt-dlp flat dump of the signed-in channels
    page (youtube.com/feed/channels): (data, cookies_path,
    reason) - data None on any failure. That page lists EVERY
    channel the account follows, unlike the subscriptions
    feed, which only surfaces channels that have recent
    videos. Same trimmed ladder as _subs_dump (v1.8.4)."""
    return _subs_dump_run(
        "https://www.youtube.com/feed/channels", SUBS_PAGE_LIMIT)


def _page_channel_of(e):
    """A channel hit {"id", "name", "poster"} from one
    channels-page entry, or None when the entry carries no
    usable UC id. Entries there are channels themselves: the
    UC id is the entry's own id (or its channel fields, via
    _feed_channel_id), the name its title, and the poster its
    avatar thumbnail when the dump carries one."""
    if not isinstance(e, dict):
        return None
    ucid = str(e.get("id") or "")
    if not re.match(r"^UC[A-Za-z0-9_-]{22}$", ucid):
        ucid = _feed_channel_id(e) or ""
    if not ucid:
        return None
    name = (
        (e.get("title") or e.get("channel") or e.get("uploader") or "")
        .strip()
    )
    poster = None
    thumbs = e.get("thumbnails")
    if isinstance(thumbs, list):
        for th in reversed(thumbs):
            if isinstance(th, dict) and th.get("url"):
                poster = str(th["url"])
                break
    return {
        "id": ucid,
        "name": name[:200] or "YouTube Channel",
        "poster": poster,
    }


def fetch_followed_channels():
    """Every channel the account follows, as channel hits in
    the channels page's own order, deduped by UC id - or None
    when the fetch itself failed. Every outcome is recorded
    for subs-debug.log and the configure page (v1.8.4)."""
    data, cookies_path, reason = _channels_page_dump()
    if data is None:
        print("StreamSearch: channels page fetch failed",
              file=sys.stderr)
        _subs_record("page", cookies_path, False, reason=reason)
        return None
    entries = data.get("entries")
    if not isinstance(entries, list):
        print("StreamSearch: channels page had no entries",
              file=sys.stderr)
        _subs_record("page", cookies_path, False,
                     reason="channels page had no entries")
        return None
    channels = []
    seen = set()
    for e in entries:
        hit = _page_channel_of(e)
        if not hit or hit["id"] in seen:
            continue
        seen.add(hit["id"])
        channels.append(hit)
    if not channels:
        print("StreamSearch: channels page came back empty",
              file=sys.stderr)
    _subs_record("page", cookies_path, True, count=len(channels))
    return channels


def _store_followed(fresh):
    """Cache a fresh followed-channels list and register each
    channel in _channel_hits so its series meta keeps its name
    and poster."""
    with CACHE_LOCK:
        _subs_cache["page"] = (fresh, time.time())
        for ch in fresh:
            known = _channel_hits.get(ch["id"])
            if not known:
                _channel_hits[ch["id"]] = {
                    "name": ch["name"],
                    "poster": ch["poster"],
                }
            elif not known.get("poster") and ch["poster"]:
                known["poster"] = ch["poster"]
    for ch in fresh:
        # A channels-page entry's picture is the channel's
        # avatar itself - keep it as the channel's logo.
        note_channel_logo(ch["id"], ch["poster"])


def followed_channels():
    """Cache-aware list of every followed channel (the channels
    page), on the same SUBS_TTL as the feed. Stale-while-
    revalidate: once any list exists, a request serves it at
    once - even past the TTL - while a background thread
    refreshes it, so opening the catalog never waits on the
    yt-dlp dump; only the very first fetch (nothing cached yet)
    blocks. Since v1.8.4 a FAILED first fetch is cached too, as
    an empty list with the normal TTL: the old build cached
    only successes, so on a failing setup every single open
    re-paid the whole (minutes-long) ladder and Nuvio timed
    the row out of existence. Never raises."""
    try:
        with CACHE_LOCK:
            hit = _subs_cache.get("page")
        if hit and time.time() - hit[1] < SUBS_TTL:
            return hit[0]
        if hit:
            _subs_refresh_async("page")
            return hit[0]
        fresh = fetch_followed_channels()
        if fresh is not None:
            _store_followed(fresh)
            return fresh
        with CACHE_LOCK:
            _subs_cache["page"] = ([], time.time())
        return []
    except Exception:
        print("StreamSearch: followed channels lookup failed",
              file=sys.stderr)
        return []


def _feed_channel_id(e):
    """The UC id of the channel a subscriptions-feed entry came
    from, or None. Feed entries carry the channel in their
    channel_id/uploader_id fields or in channel_url/uploader_url
    - unlike search entries, whose own id is the video's, so
    _channel_id_of does not apply here."""
    for key in ("channel_id", "uploader_id"):
        cid = str(e.get(key) or "")
        if re.match(r"^UC[A-Za-z0-9_-]{22}$", cid):
            return cid
    for key in ("channel_url", "uploader_url"):
        m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})",
                      str(e.get(key) or ""))
        if m:
            return m.group(1)
    return None


def fetch_subscriptions():
    """The subscriptions feed as (channels, videos), or None when
    the fetch itself failed. Videos are hit dicts {"id", "title",
    "is_live"} in feed order (most recent activity first),
    deduped by video id. Channels are hit dicts {"id", "name",
    "poster"} deduped by channel id in the order the feed first
    surfaces them; the poster is the channel's first feed video's
    thumbnail - the feed carries no channel picture of its own.
    Every outcome is recorded for subs-debug.log and the
    configure page (v1.8.4)."""
    data, cookies_path, reason = _subs_dump()
    if data is None:
        print("StreamSearch: subscriptions feed fetch failed",
              file=sys.stderr)
        _subs_record("feed", cookies_path, False, reason=reason)
        return None
    entries = data.get("entries")
    if not isinstance(entries, list):
        print("StreamSearch: subscriptions feed had no entries",
              file=sys.stderr)
        _subs_record("feed", cookies_path, False,
                     reason="subscriptions feed had no entries")
        return None
    channels = []
    seen_channels = set()
    videos = []
    seen_videos = set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        vid = str(e.get("id") or "")
        if len(vid) != 11 or vid in seen_videos:
            continue
        seen_videos.add(vid)
        title = (e.get("title") or "").strip() or ("YouTube " + vid)
        videos.append({
            "id": vid,
            "title": title[:200],
            "is_live": e.get("live_status") == "is_live",
        })
        ucid = _feed_channel_id(e)
        if not ucid or ucid in seen_channels:
            continue
        seen_channels.add(ucid)
        name = (
            (e.get("channel") or e.get("uploader") or "").strip()
        )
        channels.append({
            "id": ucid,
            "name": name[:200] or "YouTube Channel",
            "poster": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
        })
    if not videos and not channels:
        print("StreamSearch: subscriptions feed came back empty",
              file=sys.stderr)
    _subs_record("feed", cookies_path, True, count=len(channels))
    return (channels, videos)


def _store_feed(fresh):
    """Cache a fresh subscriptions feed and register its
    channels in _channel_hits (so their series metas keep name
    and poster) and its videos in _yt_hits (so meta and stream
    answers for the feed's ids work at once, live flags
    included)."""
    channels, videos = fresh
    with CACHE_LOCK:
        _subs_cache["feed"] = (fresh, time.time())
        for ch in channels:
            known = _channel_hits.get(ch["id"])
            if not known:
                _channel_hits[ch["id"]] = {
                    "name": ch["name"],
                    "poster": ch["poster"],
                }
            elif not known.get("poster"):
                known["poster"] = ch["poster"]
        for v in videos:
            _yt_hits[v["id"]] = {
                "title": v["title"],
                "is_live": v["is_live"],
            }


def subscriptions_feed():
    """Cache-aware (channels, videos) from the subscriptions
    feed, stale-while-revalidate like followed_channels: a
    cached feed is served at once even past SUBS_TTL while a
    background thread refreshes it; only the very first fetch
    blocks. A FAILED first fetch is cached as an empty feed
    with the normal TTL too (v1.8.4, same fix as
    followed_channels), so repeat opens are instant instead of
    re-paying the ladder. Never raises."""
    try:
        with CACHE_LOCK:
            hit = _subs_cache.get("feed")
        if hit and time.time() - hit[1] < SUBS_TTL:
            return hit[0]
        if hit:
            _subs_refresh_async("feed")
            return hit[0]
        fresh = fetch_subscriptions()
        if fresh is not None:
            _store_feed(fresh)
            return fresh
        with CACHE_LOCK:
            _subs_cache["feed"] = (([], []), time.time())
        return ([], [])
    except Exception:
        print("StreamSearch: subscriptions lookup failed",
              file=sys.stderr)
        return ([], [])


def subscription_channels():
    """The channel hits for the "Subscriptions" catalog, in
    order: every followed channel from the channels page
    (complete, in page order), then any extra channel the
    subscriptions feed surfaced that the page missed, then any
    channel saved by hand (channels.json) neither source
    listed, in saved order. Deduped by UC id throughout.
    Saved channels without a known UC id cannot open as a
    series and are skipped. Never raises."""
    try:
        # Cold start: when neither source has ever been fetched,
        # the two yt-dlp dumps run side by side instead of one
        # after the other - the catalog's first open pays for one
        # dump's wait, not two. (Once anything is cached, the
        # getters below answer at once and refresh in the
        # background, so no fan-out is needed.)
        with CACHE_LOCK:
            _cold = [k for k in ("page", "feed")
                     if k not in _subs_cache]
        if len(_cold) == 2:
            _workers = [
                threading.Thread(target=followed_channels),
                threading.Thread(target=subscriptions_feed),
            ]
            for _w in _workers:
                _w.start()
            for _w in _workers:
                _w.join()
        result = []
        have = set()
        for ch in followed_channels():
            if ch["id"] in have:
                continue
            have.add(ch["id"])
            result.append(ch)
        feed_channels, _videos = subscriptions_feed()
        for ch in feed_channels:
            if ch["id"] in have:
                continue
            have.add(ch["id"])
            result.append(ch)
        with LOCK:
            saved = load_store(CHANNEL_STORE)
        for ch in saved:
            if not isinstance(ch, dict):
                continue
            ucid = str(ch.get("ucid") or "")
            if not re.match(r"^UC[A-Za-z0-9_-]{22}$", ucid):
                m = re.match(r"^channel:(UC[A-Za-z0-9_-]{22})$",
                             str(ch.get("id") or ""))
                ucid = m.group(1) if m else ""
            if not ucid or ucid in have:
                continue
            have.add(ucid)
            hit = {
                "id": ucid,
                "name": (ch.get("name") or "").strip()[:200]
                or "YouTube Channel",
                "poster": None,
            }
            result.append(hit)
            with CACHE_LOCK:
                if ucid not in _channel_hits:
                    _channel_hits[ucid] = {
                        "name": hit["name"],
                        "poster": None,
                    }
        return result
    except Exception:
        print("StreamSearch: subscriptions channel list failed",
              file=sys.stderr)
        return []


# Magnet search: searching the addon's catalog ALSO looks for
# magnet links on a public torrent index - apibay, the Pirate
# Bay's public JSON API (no key, no account). Hits live in memory
# only, exactly like YouTube search hits: a per-query list cached
# for SEARCH_TTL and every hit by info hash in _magnet_hits,
# which is what the meta and stream routes consult for magnet
# ids that were never saved. Nothing here is ever written to a
# store.
MAGNET_SEARCH_LIMIT = 10
MAGNET_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
]
_magnet_search_cache = {}  # normalized query -> ([hit, ...], timestamp)
_magnet_hits = {}  # info hash -> {"info_hash", "name", "magnet", "seeders", "size"}


def human_size(num):
    """Bytes as a short human size ("1.4 GB") for search-hit
    descriptions; "" when the value is not a number."""
    try:
        n = float(num)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            if unit == "B":
                return "%d B" % n
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return ""


def build_magnet_uri(info_hash, name):
    """The magnet URI for one search hit: the hash, the release
    name, and the usual public trackers - apibay rows carry no
    trackers of their own, and peers are found through these."""
    uri = "magnet:?xt=urn:btih:%s&dn=%s" % (info_hash, quote(name, safe=""))
    for tracker in MAGNET_TRACKERS:
        uri += "&tr=" + quote(tracker, safe="")
    return uri


def _apibay_search(query):
    """One apibay query: the raw result rows, or None on any
    failure - no network, timeout, bad JSON, unexpected shape."""
    url = "https://apibay.org/q.php?q=%s&cat=0" % quote(query, safe="")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    return data if isinstance(data, list) else None


def magnet_search(query):
    """Magnet links matching a catalog search, as hit dicts
    {"info_hash", "name", "magnet", "seeders", "size"} - best
    seeded first, at most MAGNET_SEARCH_LIMIT. Dead results are
    dropped: seeders under 1, malformed hashes, and apibay's
    all-zeros "no results" row. Results are cached per query for
    SEARCH_TTL, and every hit is remembered in _magnet_hits for
    the meta/stream routes. Any failure is an empty list - a
    failed magnet search never breaks the catalog."""
    key = " ".join((query or "").lower().split())
    if not key:
        return []
    with CACHE_LOCK:
        hit = _magnet_search_cache.get(key)
    if hit and time.time() - hit[1] < SEARCH_TTL:
        return hit[0]
    results = []
    for row in (_apibay_search(key) or []):
        if not isinstance(row, dict):
            continue
        info_hash = str(row.get("info_hash") or "").strip().lower()
        if not re.match(r"^[a-f0-9]{40}$", info_hash):
            continue
        if set(info_hash) == {"0"}:
            continue  # apibay's "no results" sentinel row
        try:
            seeders = int(row.get("seeders") or 0)
            size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if seeders < 1:
            continue
        name = (row.get("name") or "").strip()
        if not name:
            continue
        results.append({
            "info_hash": info_hash,
            "name": name[:300],
            "seeders": seeders,
            "size": size,
        })
    results.sort(key=lambda r: -r["seeders"])
    results = results[:MAGNET_SEARCH_LIMIT]
    for r in results:
        r["magnet"] = build_magnet_uri(r["info_hash"], r["name"])
    with CACHE_LOCK:
        _magnet_search_cache[key] = (results, time.time())
        for r in results:
            _magnet_hits[r["info_hash"]] = {
                "info_hash": r["info_hash"],
                "name": r["name"],
                "magnet": r["magnet"],
                "seeders": r["seeders"],
                "size": r["size"],
            }
    return results


def magnet_hit(info_hash):
    """The remembered magnet search hit for an info hash, or None."""
    with CACHE_LOCK:
        hit = _magnet_hits.get((info_hash or "").strip().lower())
    return dict(hit) if hit else None


def meta_of_magnet_hit(hit):
    """The catalog/meta entry for one magnet search hit: the
    release name, with its seeders and size as the description.
    A hit with no seeders is a magnet pasted into the search
    bar - it has no index row behind it, so its seeders and
    size are simply unknown and the description says so."""
    if not hit["seeders"]:
        desc = "Magnet link (P2P)."
    else:
        desc = "Seeders: %d" % hit["seeders"]
        size = human_size(hit["size"])
        desc += " · " + (size if size else "unknown size")
    return {
        "id": "magnet:" + hit["info_hash"],
        "type": "movie",
        "name": hit["name"],
        "description": desc,
    }


def magnet_hit_item(info_hash):
    """A playable item for a magnet search hit, parsed from the
    hit's magnet URI exactly as a pasted magnet is parsed - so
    the streams streams_for_item() builds for it are the same
    entries a saved copy of the same magnet gets. None when the
    hash is not a remembered hit."""
    hit = magnet_hit(info_hash)
    if not hit:
        return None
    return parse_magnet(hit["magnet"])


def save_pasted_magnet(item):
    """Save a magnet pasted into the search bar to the magnet
    store - the same entry, in the same place, that adding it
    by hand on the configure page makes, so it shows in the
    saved list there and in the main catalog and survives
    restarts. Deduped by info hash: a hash that is already
    saved is left exactly as it stands, so repeat pastes cost
    no disk write at all. A save that fails (an unwritable
    data folder, say) is swallowed - the paste must still
    play, saved or not."""
    try:
        with LOCK:
            items = load_store(STORE)
            if any(i.get("id") == item["id"] for i in items):
                return
            items.insert(0, item)
            save_store(items, STORE)
    except Exception:
        pass


def pasted_magnet_meta(query):
    """The one catalog meta for a magnet pasted into the search
    bar, or None when the text is not a usable magnet. The
    query is the search text as the catalog route decoded it:
    a magnet URI - on its own, or embedded in other text -
    whose btih hash is valid (40 hex characters, or 32 base32
    ones). The hit is remembered in _magnet_hits exactly like
    a magnet search hit, so the meta and stream routes resolve
    it from the full pasted URI and it plays like any unsaved
    hit - and the magnet is also saved to the store, exactly
    as if it had been added by hand on the configure page,
    so it is still there after a restart. Anything else -
    no magnet in the text, a magnet that does not parse, a
    malformed hash - is None, and the search carries on
    exactly as before."""
    m = re.search(r"magnet:\?\S+", (query or "").strip(), re.I)
    if not m:
        return None
    item = parse_magnet(m.group(0))
    if not item:
        return None
    info_hash = item["infoHash"]
    if not (re.match(r"^[a-f0-9]{40}$", info_hash)
            or re.match(r"^[a-z2-7]{32}$", info_hash)):
        return None
    name = item["name"]
    if name == "Magnet " + info_hash[:8]:
        name = "Magnet link"
    with CACHE_LOCK:
        _magnet_hits[info_hash] = {
            "info_hash": info_hash,
            "name": name[:300],
            "magnet": item["magnet"],
            "seeders": 0,
            "size": 0,
        }
    save_pasted_magnet(item)
    return {
        "id": "magnet:" + info_hash,
        "type": "movie",
        "name": name[:300],
        "description": "Magnet link (P2P).",
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


def pick_progressive_format(info, cap):
    """Pick the best single-file (progressive) MP4 format from a
    yt-dlp -j dump: one plain file carrying both picture (avc1)
    and sound (mp4a) - itag 22 at 720p, itag 18 at 360p - served
    directly, not as a playlist or a segment manifest, at or
    under the height cap. Returns None when YouTube offers no
    such file at this cap (at 1080p it never does: HD is split
    streams only, which is what the merge path is for)."""
    best = None
    for f in (info.get("formats") or []):
        if not f.get("url"):
            continue
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        if vcodec == "none" or acodec == "none":
            continue
        if not vcodec.startswith("avc1"):
            continue
        if not acodec.startswith("mp4a"):
            continue
        ext = (f.get("ext") or "").lower()
        if ext and ext != "mp4":
            continue
        proto = (f.get("protocol") or "").lower()
        if proto and proto not in ("http", "https"):
            continue
        if _is_hls_url(f["url"]):
            continue
        height = f.get("height") or 0
        if not height or height > cap:
            continue
        key = (height, f.get("tbr") or 0)
        if best is None or key > best[0]:
            best = (key, f)
    return best[1] if best else None


def _progressive_info(item):
    """A yt-dlp -j dump for a YouTube item from the android_vr
    client, or None on any failure. The default client's dump
    (see youtube_info) lists only split streams for most videos
    now; android_vr is the client that still offers the combined
    file - the same reason the -g ladder in _resolve_ladder
    walks it first. The cookies file rides along when there is
    one; if that fails, one bare attempt follows, since a stale
    export can fail this client while the bare fetch works."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        return None

    def attempt(cookies_path):
        base = [ytdlp, "-j", "--no-warnings"]
        if cookies_path:
            base += ["--cookies", cookies_path]
        base += ["--extractor-args",
                 "youtube:player_client=android_vr"]
        try:
            out = subprocess.run(
                base + ["--", item["url"]],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if out.returncode != 0:
                return None
            info = json.loads(out.stdout)
            if isinstance(info, dict):
                return info
        except Exception:
            pass
        return None

    cookies_path = find_cookies()
    info = attempt(cookies_path)
    if info is None and cookies_path:
        info = attempt(None)
    return info


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
    # Not saved: a magnet id remembered from search - above
    # all one pasted into the search bar - resolves from the
    # hit cache, which carries its full magnet URI. The only
    # route for a base32 hash, which the bare-hash shape above
    # does not cover.
    if item_id.startswith("magnet:"):
        return magnet_hit_item(item_id[len("magnet:"):])
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


def streams_for_item(item):
    """The stream list for one saved item - the same lists the
    stream route has always served for the item's own id. Reused
    for tagged lookups, where several saved items can answer for
    one show id."""
    if item.get("kind") == "link":
        return [
            {
                "name": "Web link",
                "title": item["name"] + "\nDirect web stream",
                "url": item["url"],
            },
        ]
    if item.get("kind") == "youtube":
        resolved = resolve_youtube(item)
        if resolved and _is_hls_url(resolved):
            # Live: only the relay. The addon follows the
            # live playlist itself and feeds the player one
            # continuous stream - a player handed the
            # playlist directly can run out of segments and
            # stop, so the direct stream is not listed at
            # all. The HD merge doesn't apply to live either.
            return [
                {
                    "name": "YouTube Live (relay)",
                    "title": item["name"]
                    + "\nLive relay — app feeds the stream, plays continuously",
                    "url": "http://127.0.0.1:%d/live/%s"
                    % (PORT, item["id"]),
                }
            ]
        # Everything else plays through the app too: the
        # direct stream links proved unreliable, so a
        # YouTube item lists exactly one stream - the
        # /play/ relay - at every quality setting. Inside
        # /play/ the addon merges the separate picture and
        # sound streams at the quality cap, and a video it
        # can't merge still plays through the fallback
        # there, so nothing is lost by not listing the
        # direct link.
        return [
            {
                "name": "YouTube (relay)",
                "title": item["name"]
                + "\nApp-served stream — the app feeds the video",
                "url": "http://127.0.0.1:%d/play/%s"
                % (PORT, item["id"]),
            }
        ]
    return [
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


def streams_for_attach(raw_id):
    """Streams of every saved item tagged with the requested
    show/movie id, combined in saved order - or an empty list when
    the id is not a usable tag or nothing is tagged with it. The
    requested id may arrive URL-encoded (%3A for the colons of an
    episode id); it is unquoted and normalized with the same
    parse_attach() the stored tags went through, then matched
    exactly against each item's "attach" field across the magnet,
    web-link and YouTube stores."""
    attach = parse_attach(unquote(raw_id or ""))
    if not attach:
        return []
    with LOCK:
        items = all_items()
    streams = []
    for it in items:
        if it.get("attach") == attach:
            streams.extend(streams_for_item(it))
    return streams


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
<title>StreamSearch</title>
<meta http-equiv="refresh" content="4;url=/configure">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{background:#111;color:#eee;font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:24px auto;padding:0 16px}</style>
</head><body>
<h2>Updated to version %s</h2>
<p>The app is restarting on the new version - this page will reload
to the configure page in a few seconds.</p>
<script>setTimeout(function(){ location.replace("/configure"); }, 4000);</script>
</body></html>""" % esc(version)




def esc(s):
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def lan_ip():
    """This machine's primary LAN address, for the phone line on
    the configure page, or None when it can't be determined. The
    UDP-connect trick asks the OS which local address it would
    use to reach the outside; no traffic is sent."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None
    if not ip or ip.startswith("127."):
        return None
    return ip


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
It can start a little slower. At 720p or lower, when
YouTube offers the video as one combined file, the app hands that
file straight to Nuvio instead, so you can fast-forward and rewind
<b>from the very first play</b>. At higher settings the first play
can't be scrubbed - but the app starts building a
<b>seekable copy</b> of the video the moment you open it, before
you press play, so the copy may already be ready for that first
play, and from the next play on you can always fast-forward and
jump to any position. Only the newest few
copies are kept.</p>
<p style="color:#999">Live items get a <b>Live relay</b> stream:
the app pulls the live feed itself and hands it to Nuvio as one
continuous stream.</p>
""" % options
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
    update_form = """<form method="post" action="/update" style="margin-top:28px">
<label>Updates</label>
<p style="color:#999;margin:4px 0 8px">You are running version %s.
Pressing Update fetches the newest version and installs it in place;
the app restarts itself and this page comes back on the new version.
Approving the app in macOS Settings is a one-time thing - updates
through this button never need it again.</p>
<button type="submit">Update</button>
</form>""" % MANIFEST["version"]
    def tag_note(it):
        a = (it.get("attach") or "").strip()
        if not a:
            return ""
        t = (it.get("attach_title") or "").strip()
        label = ("%s — %s" % (a, t)) if t else a
        return "<br><small style='color:#9c9'>tagged: %s</small>" % esc(label)

    rows = ""
    for it in links:
        rows += (
            '<div style="padding:10px 0;border-bottom:1px solid #333">'
            "<b>%s</b> <small style='color:#7ab'>(web link)</small><br>"
            "<small style='color:#999'>%s</small>%s<br>"
            '<a href="/delete?id=%s" style="color:#e66">Delete</a></div>'
            % (esc(it["name"]), esc(it["url"]), tag_note(it), esc(it["id"]))
        )
    for it in youtubes:
        rows += (
            '<div style="padding:10px 0;border-bottom:1px solid #333">'
            "<b>%s</b> <small style='color:#7ab'>(YouTube)</small><br>"
            "<small style='color:#999'>%s</small>%s<br>"
            '<a href="/delete?id=%s" style="color:#e66">Delete</a></div>'
            % (esc(it["name"]), esc(it["url"]), tag_note(it), esc(it["id"]))
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
            "<b>%s</b><br><small style='color:#999'>%s</small>%s<br>"
            '<a href="/delete?id=%s" style="color:#e66">Delete</a></div>'
            % (esc(it["name"]), esc(it["id"]), tag_note(it), esc(it["id"]))
        )
    if not magnets and not links and not youtubes and not channels:
        rows = "<p style='color:#999'>Nothing saved yet.</p>"
    note = ('<p style="color:#6f6">%s</p>' % esc(msg)) if msg else ""
    phone_line = ""
    _ip = lan_ip()
    if _ip:
        phone_line = (
            '<p style="color:#999">On your phone (same network): '
            '<code>http://%s:%d/configure</code> - open it in '
            'Safari, then Share, then Add to Home Screen, and '
            'this page works like an app.</p>' % (_ip, PORT))
    html = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>StreamSearch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{background:#111;color:#eee;font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:24px auto;padding:0 16px}
input,textarea,select{width:100%%;box-sizing:border-box;padding:10px;margin:6px 0;background:#222;color:#eee;border:1px solid #444;border-radius:6px}
button{background:#2b6cb0;color:#fff;border:0;padding:12px 20px;border-radius:6px;font-size:16px}
a{color:#7ab}</style></head><body>
<h2>StreamSearch</h2>
<p>Install in Nuvio with this URL:<br><code>http://127.0.0.1:%d/manifest.json</code></p>
%s
<p>Paste a magnet link or a web video link below. Both appear in Nuvio
under <b>StreamSearch</b>. Keep this program running while you watch.
Magnets play peer-to-peer, so have your VPN on for those. Web video
links play straight from the web - no VPN needed. A web link has to be
the video file/stream address itself (like .mp4, .mkv or .m3u8),
not a web page.</p>
<p>YouTube links resolve when you press play, so yt-dlp needs to be
installed on this Mac, and the first play can take a few seconds.</p>
<p style="color:#999">Searching the <b>StreamSearch</b> list
inside Nuvio also searches <b>YouTube</b> - matching videos appear
in the results next to your saved items and play the same way.
They are not saved; they last only while the app runs.</p>
<p style="color:#999">A <b>channel</b> found in a search opens as
its own item - open it to see that channel's videos and play any
of them; a live video sits at the top while the channel is live.</p>
<p style="color:#999">Searching also finds <b>magnet links</b> -
matching releases appear with their seeders and size and play
peer-to-peer (VPN on). They are not saved either.</p>
<p style="color:#999"><b>Tagging:</b> any link below can also carry
a <b>show or movie</b> - just type its title and the app looks the
code up by itself, or paste the IMDb code (like tt1234567), an IMDb
page address, tt1234567:1:2, or a title with 1:2 or S1E2 on the end
for one episode of a series. A tagged link also appears under that
show's <b>sources</b> in Nuvio: search the show on Nuvio's main
page, open it, and your saved link is listed with the rest. The
Saved list below shows what each link was tagged as, so you can
check the app picked the right show. Leave the box empty and the
link works as before, in the StreamSearch list only.</p>
%s
<form method="post" action="/addany" style="border:1px solid #2b6cb0;border-radius:6px;padding:12px">
<label><b>Paste any link</b> - magnet, web video address, YouTube
video, or YouTube channel. This box sorts out which kind it is.</label>
<input name="paste" placeholder="magnet:?... or https://...">
<label>Name (optional)</label>
<input name="name" placeholder="Name - YouTube titles fill in by themselves">
<label>Show/movie (optional) — type the title, or paste the tt code / IMDb link; add :season:episode or S1E2 for one episode</label>
<input name="attach" placeholder="Breaking Bad, tt1234567, or an IMDb link">
<button type="submit">Add it</button>
</form>
<p style="color:#999">The separate boxes below still work too - they
are the same thing, one kind at a time.</p>
<form method="post" action="/add">
<label>Name (optional)</label>
<input name="name" placeholder="Movie name">
<label>Magnet link</label>
<textarea name="magnet" rows="3" placeholder="magnet:?xt=urn:btih:..."></textarea>
<label>Show/movie (optional) — type the title, or paste the tt code / IMDb link; add :season:episode or S1E2 for one episode</label>
<input name="attach" placeholder="Breaking Bad, tt1234567, or an IMDb link">
<button type="submit">Add magnet</button>
</form>
<form method="post" action="/addlink" style="margin-top:28px">
<label>Name (optional)</label>
<input name="name" placeholder="Video name">
<label>Web video link</label>
<input name="link" placeholder="https://.../movie.mp4">
<label>Show/movie (optional) — type the title, or paste the tt code / IMDb link; add :season:episode or S1E2 for one episode</label>
<input name="attach" placeholder="Breaking Bad, tt1234567, or an IMDb link">
<button type="submit">Add web link</button>
</form>
<form method="post" action="/addyoutube" style="margin-top:28px">
<label>Name (optional)</label>
<input name="name" placeholder="Video name">
<label>YouTube link</label>
<input name="youtube" placeholder="https://www.youtube.com/watch?v=...">
<label>Show/movie (optional) — type the title, or paste the tt code / IMDb link; add :season:episode or S1E2 for one episode</label>
<input name="attach" placeholder="Breaking Bad, tt1234567, or an IMDb link">
<button type="submit">Add YouTube video</button>
</form>
%s
%s
%s
%s
<h3>Saved</h3>%s
</body></html>""" % (PORT, phone_line, note, quality_form,
                    channel_form, update_form, _subs_status_html(), rows)
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

    def _serve_file_range(self, path, head_only=False):
        """Serve a finished cache file with full seeking:
        Content-Length, Accept-Ranges, and Range requests answered
        with 206 (open-ended and suffix ranges included; a range
        starting beyond the end of the file gets a 416). A
        malformed Range header is ignored and the whole file is
        served, which the standard allows. Serving refreshes the
        file's mtime - that is the cache's "recently used" stamp,
        and it keeps the prune away from a file in use."""
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_json({"error": "not found"}, 404)
            return
        try:
            os.utime(path, None)
        except OSError:
            pass
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
        if m and (m.group(1) or m.group(2)):
            if not m.group(1):
                n = int(m.group(2))
                if n == 0:
                    status = 416
                else:
                    start = max(0, size - n)
                    status = 206
            else:
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start >= size or end < start:
                    status = 416
                else:
                    status = 206
        if status == 416:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header(
                "Content-Range", "bytes %d-%d/%d" % (start, end, size)
            )
        self.end_headers()
        if head_only:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_play(self, raw_id, head_only=False):
        """Serve a YouTube item at /play/<id>.

        When a finished seekable copy of the video exists in the
        relay cache (built during an earlier play - see
        start_cache_merge), the file itself is served with ranges,
        and the player can scrub it. Otherwise, at quality
        settings of 720p or lower, when YouTube offers the video
        as a single combined MP4 file, the player is redirected
        straight to that file: a plain MP4 fetched from its host
        with ranges, so seeking works from the very first play
        and no relay sits in the path. At higher settings (and
        whenever no combined file fits the cap) the HD merge
        runs as before - ffmpeg joining the separate picture and
        sound streams into a fragmented MP4 on the fly, no
        seeking, close-delimited body - and the same merge also
        runs into the cache file in the background for next time.
        Live items and items without a mergeable format pair
        fall back to a redirect to the plain combined stream.
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
        vid = youtube_video_id(item)
        quality = load_settings()["quality"]
        cached = relay_cache_ready(vid, quality)
        if cached:
            self._serve_file_range(cached, head_only=head_only)
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

        cap = int(quality) if quality.isdigit() else 1080
        pair = None
        info = None
        if cap <= PROGRESSIVE_MAX_HEIGHT:
            # Progressive first play: if YouTube offers this
            # video as one combined MP4 file at this cap, hand
            # the player that file and step aside - it scrubs
            # from the first play. A cached pick only ever
            # exists for a non-live item, so it can be served
            # without the yt-dlp -j call, exactly like a cached
            # pair below.
            prog = cache_prog_get(vid, cap)
            if prog:
                self.redirect_absolute(prog)
                return
            info = youtube_info(item)
            if info is not None and info.get("is_live"):
                fallback()
                return
            picked_prog = pick_progressive_format(info, cap) \
                if info else None
            if picked_prog is None:
                # The default client's dump lists only split
                # streams for most videos now; the android_vr
                # client still offers the combined file, so ask
                # it before settling for the merge relay.
                alt = _progressive_info(item)
                if alt is not None:
                    if alt.get("is_live"):
                        fallback()
                        return
                    picked_prog = pick_progressive_format(alt, cap)
            if picked_prog:
                cache_prog_put(vid, cap, picked_prog["url"])
                self.redirect_absolute(picked_prog["url"])
                return
            if info is not None:
                # No combined file at this cap: pick the merge
                # pair from the dump already in hand.
                picked = pick_merge_pair(info, cap)
                if picked:
                    pair = picked
                    cache_pair_put(
                        vid, cap, picked[0]["url"], picked[1]["url"])
        if pair is None:
            cached_pair = cache_pair_get(vid, cap)
            if cached_pair:
                # A cached pair only ever exists for a non-live item, so
                # the live check is already settled and the yt-dlp -j call
                # can be skipped entirely.
                pair = ({"url": cached_pair[0]},
                        {"url": cached_pair[1]})
            elif cap > PROGRESSIVE_MAX_HEIGHT:
                info = youtube_info(item)
                if info is not None and info.get("is_live"):
                    fallback()
                    return
                picked = pick_merge_pair(info, cap) if info else None
                if picked:
                    pair = picked
                    cache_pair_put(
                        vid, cap, picked[0]["url"], picked[1]["url"])
        ffmpeg = find_ffmpeg() if pair else None
        if not pair or not ffmpeg:
            fallback()
            return
        vfmt, afmt = pair
        # While this play goes out through the pipe, build the
        # seekable copy in the background - from the next play on,
        # this video is served as a file with ranges.
        start_cache_merge(vid, quality, vfmt["url"], afmt["url"])
        # Timestamp note (measured end-to-end on a real pair,
        # 2026-11-10): this pipe's video start_time reads one
        # frame late - the fragmented muxer shifts the video
        # track so its DTS starts at 0 and writes no edit list -
        # but the audio track shifts by the same amount, so
        # picture and sound stay in sync, and the plain-MP4 cache
        # copy (see _build_cache_file) is exact at 0.000/0.000,
        # matching YouTube's own combined mux. Do not try to
        # "fix" the offset with -itsoffset (measured: it leaves
        # the audio a frame late against the picture) or with
        # -avoid_negative_ts make_zero (measured: a no-op here).
        cmd = [
            ffmpeg,
            "-v",
            "error",
            *PROBE_TRIM,
            "-i",
            vfmt["url"],
            *PROBE_TRIM,
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
        # The ffmpeg form depends on the broadcast's playlist
        # layout (live_variant_layout on the picked variant):
        #
        # fMP4 layout (variant playlists carry an #EXT-X-MAP):
        # feed ffmpeg the master playlist as ONE input, mapping the
        # best H.264 variant and its audio rendition by their
        # positions in the playlist (pick_live_variant's ordinals).
        # The relay used to feed the variant and audio playlists as
        # TWO separate inputs - but on this layout each input then
        # runs its own live-playlist cycle, and the two can drift a
        # whole segment (~5s) apart in what they're actually
        # playing: the picture and the sound come out of sync
        # (measured +5.03s of skew on a 90s capture, and a 5.04s
        # hole at the start of the audio on another). One input
        # means one demuxer serving both renditions off the same
        # playlist snapshot, so they stay locked together.
        #
        # TS layout (plain .ts segments, no #EXT-X-MAP - the layout
        # LiveNOW's newer broadcasts use): the master-as-one-input
        # form is unusable - ffmpeg stalls enumerating the master's
        # many variants and produces nothing for tens of seconds, so
        # the player sits at "buffering" and gives up. The first TS
        # form fed the two PINNED playlists (variant, audio) as two
        # inputs - but that has the same disease as the old fMP4
        # two-input form: each demuxer picks its own start segment
        # off its own fetch of its playlist, and the two picks land
        # a segment apart (measured on LiveNOW at content level:
        # the sound came out a full segment - +5.01s - ahead of the
        # picture, even though both streams' containers stamped
        # 0.000, so container-level checks never saw it). So the TS
        # form now builds a MINI MASTER - a playlist naming only
        # the chosen variant and its audio rendition - serves it
        # from this very process (the /minimaster/ route; a local
        # file won't do, ffmpeg's playlist reader refuses https
        # sub-playlists under a file:// master), and feeds THAT to
        # ffmpeg as one input: one demuxer, one timeline, both
        # renditions locked (measured against a PROGRAM-DATE-TIME-
        # aligned reference of the same broadcast window: -0.008s
        # of skew). The two-input pinned form stays as the fallback
        # for when the mini master can't be built or started.
        #
        # If the layout can't be determined (or no variant
        # qualified in the first place), the master still goes in as
        # one input and ffmpeg picks by position or first streams.
        # The audio in these playlists is ADTS, which an MP4 can't
        # hold as-is - the aac_adtstoasc bitstream filter rewrites
        # the headers on the way through (still a copy, no
        # re-encode).
        reconnect = [
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ]
        picked_full = pick_live_variant_full(resolved)
        picked = picked_full[:4] if picked_full else None
        tail = [
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
        attempts = []
        if picked:
            vurl, aurl, v_ord, a_ord = picked
            if live_variant_layout(vurl) is False:
                # TS layout. First choice: the mini master as ONE
                # input (see the layout comment above).
                if picked_full[4]:
                    token = stash_mini_master(picked_full[4])
                    mcmd = [ffmpeg, "-v", "error"] + reconnect + [
                        "-i",
                        "http://127.0.0.1:%d/minimaster/%s.m3u8"
                        % (PORT, token),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0" if aurl else "0:a:0?",
                    ]
                    attempts.append(mcmd + tail)
                # Fallback: the two pinned playlists as two inputs.
                tcmd = [ffmpeg, "-v", "error"] + reconnect + ["-i", vurl]
                if aurl:
                    tcmd += reconnect + [
                        "-i",
                        aurl,
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                    ]
                else:
                    tcmd += ["-map", "0:v:0", "-map", "0:a:0?"]
                attempts.append(tcmd + tail)
        if not attempts:
            cmd = [ffmpeg, "-v", "error"] + reconnect
            if picked:
                cmd += ["-i", resolved, "-map", "0:v:%d" % v_ord]
                if a_ord is not None:
                    cmd += ["-map", "0:a:%d" % a_ord]
                else:
                    cmd += ["-map", "0:a:0?"]
            else:
                cmd += ["-i", resolved, "-map", "0:v:0", "-map", "0:a:0?"]
            attempts.append(cmd + tail)
        for cmd in attempts:
            if self._pump_ffmpeg(cmd):
                return
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
        if path.startswith("/minimaster/"):
            # A mini master playlist stashed by handle_live (see
            # stash_mini_master) - ffmpeg fetches it here so it can
            # run ONE demuxer over a TS broadcast's variant + audio
            # rendition. It has to come over http: a file:// master
            # makes ffmpeg's playlist reader refuse the https
            # sub-playlists it names.
            token = path[len("/minimaster/"):].split(".")[0]
            text = get_mini_master(token)
            if text is None:
                self.send_json({"error": "not found"}, 404)
                return
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
            raw_search = ""
            if extra.startswith("search="):
                raw_search = unquote(extra[len("search="):])
                search = raw_search.lower()
            # A magnet pasted into the search bar is itself the
            # result: it answers alone, ahead of the saved items
            # and both searches, and plays from the full pasted
            # URI (remembered in the hit cache) - and it is
            # saved to the store like a manual add, so it joins
            # his saved list from then on.
            pasted_meta = pasted_magnet_meta(raw_search)
            if pasted_meta is not None:
                self.send_json({"metas": [pasted_meta]})
                return
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
            if search:
                have = {mt["id"] for mt in metas}
                # A catalog search also looks for magnet links:
                # matching releases join the results after the
                # saved items (remembered in memory only, never
                # saved), skipping any already listed.
                for hit in magnet_search(search):
                    mid = "magnet:" + hit["info_hash"]
                    if mid in have:
                        continue
                    have.add(mid)
                    metas.append(meta_of_magnet_hit(hit))
                # A catalog search also searches YouTube: matching
                # videos join the results (remembered in memory
                # only, never saved), skipping any already listed.
                for hit in youtube_search(search):
                    mid = "youtube:" + hit["id"]
                    if mid in have:
                        continue
                    have.add(mid)
                    vitem = synth_youtube_item(hit["id"])
                    vitem["name"] = hit["title"]
                    metas.append(meta_of(vitem))
                # Channels the YouTube search found join last, as
                # series items that open into the channel's own
                # video list (memory only, never saved).
                for hit in youtube_channel_search(search):
                    mid = "ytchannel:" + hit["id"]
                    if mid in have:
                        continue
                    have.add(mid)
                    metas.append(meta_of_channel_hit(hit))
            self.send_json({"metas": metas})
            return
        # /catalog/series/subscriptions.json - the YouTube
        # channels he follows, as "ytchannel:" series items (the
        # same shape a channel search hit gets): opening one
        # browses the channel's uploads. Built from the channels
        # page (every followed channel), feed-only extras, and
        # his hand-saved channels. Any failure in the
        # subscriptions code answers empty.
        m = re.match(r"^/catalog/series/subscriptions(?:/(.*))?\.json$", path)
        if m:
            metas = []
            try:
                for hit in subscription_channels():
                    metas.append(meta_of_channel_hit(hit))
            except Exception:
                metas = []
            self.send_json({"metas": metas})
            return
        m = re.match(r"^/meta/movie/(.+)\.json$", path)
        if m:
            item = find_item(m.group(1))
            if item and item.get("kind") == "youtube":
                # Opening one video: start its seekable-copy
                # build now, ahead of the play press (v1.8.12).
                maybe_prebuild_cache(item)
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
                    else:
                        # A video found through catalog search
                        # carries its real title in the hit cache;
                        # any other unsaved id gets one title
                        # lookup, and keeps the placeholder name
                        # when even that fails.
                        hit = search_hit(youtube_video_id(item))
                        if hit:
                            item["name"] = hit["title"]
                        else:
                            title = fetch_youtube_title(item["url"])
                            if title:
                                item["name"] = title
            if item and not item.get("kind"):
                # Magnet item. A bare-hash id (the shape catalog
                # results carry) that is not saved answers from
                # the magnet search-hit cache when it can - the
                # release name, with seeders and size - and with
                # a minimal name-the-hash meta when it cannot.
                # A saved magnet, and a full magnet pasted into
                # Nuvio's search, keep their own meta as before.
                if re.match(r"^magnet:[a-f0-9]{40}$",
                            unquote(m.group(1) or "")):
                    with LOCK:
                        is_saved = any(
                            i["id"] == item["id"]
                            for i in load_store(STORE))
                    if not is_saved:
                        hit = magnet_hit(item["infoHash"])
                        if hit:
                            self.send_json(
                                {"meta": meta_of_magnet_hit(hit)})
                            return
                        self.send_json({"meta": {
                            "id": item["id"],
                            "type": "movie",
                            "name": item["infoHash"],
                            "description": "Magnet link (P2P).",
                        }})
                        return
            self.send_json({"meta": meta_of(item)} if item else {"meta": None})
            return
        # /meta/series/ytchannel:<UC...>.json - a YouTube channel
        # found through catalog search: a series whose videos are
        # the channel's uploads. Any other series id is not ours.
        m = re.match(r"^/meta/series/(.+)\.json$", path)
        if m:
            raw = unquote(m.group(1) or "")
            if raw.startswith("ytchannel:"):
                self.send_json(
                    {"meta": channel_series_meta(raw[len("ytchannel:"):])})
                return
            self.send_json({"meta": None})
            return
        # /stream/(movie|series)/<id>.json - the addon's own item
        # ids resolve exactly as before; an external show/movie id
        # (an IMDb tt id, episode form included) resolves through
        # the tagged lookup instead, and anything else gets no
        # streams.
        m = re.match(r"^/stream/(movie|series)/(.+)\.json$", path)
        if m:
            item = find_item(m.group(2))
            if item:
                if not item.get("kind"):
                    # Magnet item. An unsaved one that came from a
                    # catalog search answers from the hit cache, so
                    # its streams are built from the full magnet the
                    # search made (release name + trackers) - the
                    # same entries a saved copy of it gets.
                    with LOCK:
                        saved_magnet_ids = {
                            i["id"] for i in load_store(STORE)}
                    if item["id"] not in saved_magnet_ids:
                        hit_item = magnet_hit_item(
                            item.get("infoHash") or "")
                        if hit_item is not None:
                            self.send_json(
                                {"streams": streams_for_item(hit_item)})
                            return
                if item.get("kind") == "youtube":
                    # Asking for one video's streams is an open
                    # too: start its seekable-copy build now
                    # (v1.8.12) - a no-op when the meta request
                    # already did, or a build is under way.
                    maybe_prebuild_cache(item)
                    # A search hit that was never saved answers
                    # from the hit cache - one relay stream, its
                    # live flag already known from the search.
                    with LOCK:
                        saved_ids = {i["id"] for i in load_store(YT_STORE)}
                    if item["id"] not in saved_ids:
                        hit_streams = streams_for_search_hit(item)
                        if hit_streams is not None:
                            self.send_json({"streams": hit_streams})
                            return
                self.send_json({"streams": streams_for_item(item)})
                return
            self.send_json({"streams": streams_for_attach(m.group(2))})
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/addany":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            raw = (form.get("paste") or [""])[0].strip()
            name = (form.get("name") or [""])[0].strip()
            attach, attach_title = resolve_attach((form.get("attach") or [""])[0])
            if raw.lower().startswith("magnet:?"):
                item = parse_magnet(raw)
                if not item:
                    self.send_html(configure_page("That magnet link did not parse - nothing saved."))
                    return
                if name:
                    item["name"] = name
                if attach:
                    item["attach"] = attach
                    if attach_title:
                        item["attach_title"] = attach_title
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
                if attach:
                    yt_item["attach"] = attach
                    if attach_title:
                        yt_item["attach_title"] = attach_title
                with LOCK:
                    items = [i for i in load_store(YT_STORE) if i["id"] != yt_item["id"]]
                    items.insert(0, yt_item)
                    save_store(items, YT_STORE)
                self.redirect("/configure")
                return
            link_item = parse_link(raw, name)
            if link_item:
                if attach:
                    link_item["attach"] = attach
                    if attach_title:
                        link_item["attach_title"] = attach_title
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
            attach, attach_title = resolve_attach((form.get("attach") or [""])[0])
            item = parse_magnet(magnet)
            if not item:
                self.send_html(configure_page("That magnet link did not parse - nothing saved."))
                return
            if name:
                item["name"] = name
            if attach:
                item["attach"] = attach
                if attach_title:
                    item["attach_title"] = attach_title
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
            attach, attach_title = resolve_attach((form.get("attach") or [""])[0])
            item = parse_link(link, name)
            if not item:
                self.send_html(configure_page("That web link did not parse - it has to start with http:// or https://. Nothing saved."))
                return
            if attach:
                item["attach"] = attach
                if attach_title:
                    item["attach_title"] = attach_title
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
            attach, attach_title = resolve_attach((form.get("attach") or [""])[0])
            item = parse_youtube(yt, name)
            if not item:
                self.send_html(configure_page("That YouTube link did not parse - it has to be a youtube.com or youtu.be video link. Nothing saved."))
                return
            if not name:
                title = fetch_youtube_title(item["url"])
                if title:
                    item["name"] = title
            if attach:
                item["attach"] = attach
                if attach_title:
                    item["attach_title"] = attach_title
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
    # Bind all interfaces so Matthew's phone, on the same local
    # network, can open the configure page. Every URL the addon
    # generates still points at 127.0.0.1 - Nuvio runs on this
    # same machine; only the listening socket widened.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("StreamSearch addon is running.")
    print("Configure page:  http://127.0.0.1:%d/configure" % PORT)
    print("Install in Nuvio: http://127.0.0.1:%d/manifest.json" % PORT)
    print("Leave this window open while you watch. Ctrl+C stops it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
