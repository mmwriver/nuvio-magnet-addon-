#!/usr/bin/env python3
"""
My Magnet Links - a tiny addon for Nuvio (Stremio-compatible).

Paste magnet links on its web page and they appear inside Nuvio as a
catalog called "My Magnet Links". Pick one and it plays as a P2P
torrent stream. You can also paste a magnet link straight into Nuvio's
search bar - it shows up as an item and this addon gives it a Play
stream the same way.

No dependencies - Python 3 standard library only.
Run it on the same Mac as Nuvio:   python3 magnet_addon.py
Then add  http://127.0.0.1:7777/manifest.json  in Nuvio (Addons ->
Install by URL). Keep this window open while you watch.
Playback is P2P torrent streaming - have your VPN on.
"""

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

PORT = int(os.environ.get("PORT", "7777"))
HERE = os.path.dirname(os.path.abspath(__file__))
_home = os.environ.get("MAGNET_ADDON_HOME")
DATA_DIR = _home if _home else HERE
if _home:
    os.makedirs(DATA_DIR, exist_ok=True)
STORE = os.path.join(DATA_DIR, "magnets.json")
LOCK = threading.Lock()

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
]

MANIFEST = {
    "id": "community.mymagnetlinks",
    "version": "1.0.0",
    "name": "My Magnet Links",
    "description": "Your own pasted magnet links, playable in Nuvio.",
    "logo": "",
    "resources": ["catalog", "meta", "stream"],
    "types": ["movie"],
    "idPrefixes": ["magnet:"],
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


def load_store():
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_store(items):
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)


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


def find_item(item_id):
    item_id = unquote(item_id or "")
    if item_id.lower().startswith("magnet:?"):
        return parse_magnet(item_id)
    with LOCK:
        for it in load_store():
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
    return {
        "id": item["id"],
        "type": "movie",
        "name": item["name"],
        "description": "Your pasted magnet link (P2P).",
    }


def esc(s):
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def configure_page(msg=""):
    with LOCK:
        items = load_store()
    rows = ""
    for it in items:
        rows += (
            '<div style="padding:10px 0;border-bottom:1px solid #333">'
            "<b>%s</b><br><small style='color:#999'>%s</small><br>"
            '<a href="/delete?id=%s" style="color:#e66">Delete</a></div>'
            % (esc(it["name"]), esc(it["id"]), esc(it["id"]))
        )
    if not items:
        rows = "<p style='color:#999'>No magnets saved yet.</p>"
    note = ('<p style="color:#6f6">%s</p>' % esc(msg)) if msg else ""
    html = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>My Magnet Links</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{background:#111;color:#eee;font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:24px auto;padding:0 16px}
input,textarea{width:100%%;box-sizing:border-box;padding:10px;margin:6px 0;background:#222;color:#eee;border:1px solid #444;border-radius:6px}
button{background:#2b6cb0;color:#fff;border:0;padding:12px 20px;border-radius:6px;font-size:16px}
a{color:#7ab}</style></head><body>
<h2>My Magnet Links</h2>
<p>Install in Nuvio with this URL:<br><code>http://127.0.0.1:%d/manifest.json</code></p>
<p>Paste a magnet link below. It appears in Nuvio under
<b>My Magnet Links</b>. Keep this program running while you watch,
and have your VPN on (playback is P2P).</p>
%s
<form method="post" action="/add">
<label>Name (optional)</label>
<input name="name" placeholder="Movie name">
<label>Magnet link</label>
<textarea name="magnet" rows="3" placeholder="magnet:?xt=urn:btih:..."></textarea>
<button type="submit">Add magnet</button>
</form>
<h3>Saved</h3>%s
</body></html>""" % (PORT, note, rows)
    return html


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/configure"):
            self.send_html(configure_page())
            return
        if path == "/manifest.json":
            self.send_json(MANIFEST)
            return
        if path == "/delete":
            q = parse_qs(parsed.query)
            did = (q.get("id") or [""])[0]
            with LOCK:
                items = [i for i in load_store() if i["id"] != did]
                save_store(items)
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
                items = load_store()
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
                items = [i for i in load_store() if i["id"] != item["id"]]
                items.insert(0, item)
                save_store(items)
            self.redirect("/configure")
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
