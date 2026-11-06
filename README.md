# My Magnet Links — a magnet-link addon for Nuvio

A tiny addon for [Nuvio](https://nuvio.tv) (it speaks the Stremio addon protocol) that lets you paste your own **magnet links** into Nuvio and play them.

You run it on your own computer. It has a small web page where you paste magnet links; those links then show up inside Nuvio in a catalog called **My Magnet Links**, ready to play. You can also paste a full magnet link straight into Nuvio's search and play it from there.

Playback is peer-to-peer (direct torrent streaming), so **use a VPN while watching**.

No accounts, no sign-ups, no dependencies — it's a single Python file using only the standard library.

## What you need

- A computer (Mac or Windows) with **Python 3** installed
  - Mac: it may already be installed; typing `python3` in Terminal will tell you.
  - Windows: install Python from [python.org](https://www.python.org/downloads/) and tick "Add Python to PATH".
- The Nuvio app on the same computer.

## Setup (one time)

1. Download this project (green **Code** button → **Download ZIP**) and unzip it.
2. Start the addon:
   - **Mac:** double-click `Start Magnet Addon.command` — or, if macOS blocks it, open Terminal and run:
     ```
     python3 "$(find ~/Downloads -name magnet_addon.py | head -1)"
     ```
   - **Windows:** open a Command Prompt in the unzipped folder and run:
     ```
     python magnet_addon.py
     ```
   The window will print two addresses and must stay open while you use the addon.
3. Install it in Nuvio: **Nuvio → Addons → Install from URL** and paste:
   ```
   http://127.0.0.1:7777/manifest.json
   ```

## Mac app version

Prefer not to use Terminal? The `mac-app` folder holds the pieces of **My Magnet Links.app**, a small Mac app that runs the same addon — double-click it and it starts the addon in the background and opens the magnet page for you. No Terminal needed.

The app bundle is put together like this:

```
My Magnet Links.app/
  Contents/
    Info.plist            (from mac-app/Info.plist)
    MacOS/launcher        (from mac-app/launcher)
    Resources/
      magnet_addon.py     (from mac-app/magnet_addon.py)
```

Saved magnets live outside the app, in `~/Library/Application Support/My Magnet Links/magnets.json`, so they survive app updates — and the app adopts the magnets you saved with the script version the first time it runs. First launch: right-click the app and choose **Open** (it's unsigned), or allow it under **System Settings → Privacy & Security → Open Anyway**.

## Adding a magnet link

1. With the addon running, open the first address it printed (the one ending in `/configure`) in your browser.
2. Paste your magnet link, give it a name, and save.
3. In Nuvio, open the **My Magnet Links** row on the home screen, pick your item, and choose the **Magnet** stream.

Your saved magnets stay saved (in `magnets.json` next to the script) — you only paste each one once. You can delete them from the same configure page.

## Web video links

You can also play videos straight from a web address. On the same `/configure` page there's a second form, **Web video link**: paste a direct video address (for example an `.mp4`, `.mkv` or `.m3u8` link), give it a name if you like, and save. It appears in the same **My Magnet Links** catalog in Nuvio, and tapping it plays the file straight from the web — no torrent, so no VPN needed for these.

One catch: the address has to be the video file/stream address itself. A web *page* address (a page that has a player on it) won't work — the addon can only hand Nuvio a direct video address.

Saved web links live in their own file, `links.json`, next to `magnets.json`, and you can delete them from the configure page too.

## Notes

- The addon only hands Nuvio the magnet link; the video itself streams from the torrent through Nuvio.
- If you close the addon window, whatever is already playing keeps going, but starting anything new needs the addon running again.
- Port 7777 is used by default; set the `PORT` environment variable to change it.

## License

Free to use and share.
