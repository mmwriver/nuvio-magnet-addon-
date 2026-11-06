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

## The one paste box

The `/configure` page has one main box at the top: **Paste any link**. Paste a magnet link, a web video address, or a YouTube link — the addon works out which kind it is and files it in the right place. The three separate boxes further down the page still work too; they're the same thing, one kind at a time.

## Adding a magnet link

1. With the addon running, open the first address it printed (the one ending in `/configure`) in your browser.
2. Paste your magnet link, give it a name, and save.
3. In Nuvio, open the **My Magnet Links** row on the home screen, pick your item, and choose the **Magnet** stream.

Your saved magnets stay saved (in `magnets.json` next to the script) — you only paste each one once. You can delete them from the same configure page.

## Web video links

You can also play videos straight from a web address. On the same `/configure` page there's a second form, **Web video link**: paste a direct video address (for example an `.mp4`, `.mkv` or `.m3u8` link), give it a name if you like, and save. It appears in the same **My Magnet Links** catalog in Nuvio, and tapping it plays the file straight from the web — no torrent, so no VPN needed for these.

One catch: the address has to be the video file/stream address itself. A web *page* address (a page that has a player on it) won't work — the addon can only hand Nuvio a direct video address.

Saved web links live in their own file, `links.json`, next to `magnets.json`, and you can delete them from the configure page too.

## YouTube videos

You can also add YouTube videos — regular videos and live streams. On the same `/configure` page there's a third form, **YouTube link**: paste a YouTube address (`youtube.com/watch?v=...`, `youtu.be/...`, `youtube.com/live/...` and similar), give it a name if you like, and save. It appears in the same **My Magnet Links** catalog.

When you press play in Nuvio, the addon asks [yt-dlp](https://github.com/yt-dlp/yt-dlp) for the video's stream address at that moment and then serves the video itself — so yt-dlp must be installed on the same computer, and the first play can take a few seconds. Live streams work the same way, through the live relay.

If you add a YouTube video without a name, the addon fills in the video's **real title** automatically (it asks yt-dlp when you save). YouTube items also show the **video's picture** in Nuvio — the catalog and detail pages use the video's own thumbnail as the poster.

Repeat plays start fast: the addon remembers each resolved stream address in memory — for a few hours for regular videos, and for a minute for live streams (their addresses rotate quickly) — so pressing play again skips the lookup. It also asks the fastest-answering source first, which cuts the first-play wait down a lot on networks where only one source answers.

### New in 1.6.4

- **Approve once, update forever.** Until now every new app version had to be approved again under System Settings → Privacy & Security → Open Anyway, because each rebuild was a brand-new app bundle in macOS's eyes. The app now copies `magnet_addon.py` into the Application Support folder (`~/Library/Application Support/My Magnet Links/`) on its first run and runs it from there — the bundle itself never changes again, so the one approval covers it for good. (Your saved lists were already kept in that folder; now the script lives there too.)
- **Update button.** The `/configure` page shows the running version and an **Update** button. Pressing it fetches the newest `magnet_addon.py`, checks that it's valid and actually newer, swaps it in, and restarts the app on the new version — this page comes back by itself. Terminal-version users get the same button: it updates their script in place the same way. From here on, new versions install themselves; nobody has to re-approve anything.

### New in 1.6.3

- **Delete items from inside Nuvio.** The addon protocol has no buttons or menus, so deleting rides in as a stream: open any saved item in Nuvio and the **last** entry in its stream list is a **Delete** option. Picking it removes the item and plays a one-second blank clip. For a video that came from a saved channel, the entry instead removes the **whole channel** and its videos. There is no undo — paste the link again to bring something back. A deleted item can still show in Nuvio's list until that list refreshes; reopening Nuvio, or searching, shows the updated list.

### Fix in 1.6.2

- **Live lip-sync fixed.** On live streams the picture and the sound could drift apart — the relay was feeding the video playlist and the sound playlist to ffmpeg as two separate inputs, and the two sides could end up a whole segment (about 5 seconds) apart in what they were playing, so voices and lips didn't match. The relay now reads the live stream as one single input, so picture and sound stay locked together.

### Fix in 1.6.1

- **Channel videos are labeled with their channel.** In the catalog, each of a channel's uploads shows as "Channel Name — Video Title", so you can tell which channel it came from.
- **All YouTube playback goes through the addon.** Regular YouTube videos used to list a direct stream link (with the HD merge entry in front of it at 1080p/Best); the direct link didn't play reliably, so every YouTube item — regular videos and live streams alike — now lists a single app-served **relay** stream, and the addon itself fetches and serves the video. (Matthew's call.)

### Fix in 1.5.3

- **Live items list only the relay.** The direct stream that 1.5.2 kept listed second for live items is gone — the relay is the stream that actually plays through, so live items now show just the one **YouTube Live (relay)** entry. (Matthew's call.)

### Fix in 1.5.2

- **Live relay.** A YouTube live playlist only hands out a short window of video at a time (about 30 seconds) and expects the player to keep re-asking for the next window — and Nuvio's player doesn't keep up, so live playback stopped after a few seconds even on a fast connection. Live items now list a **YouTube Live (relay)** stream first: the addon follows the live playlist itself with ffmpeg and hands Nuvio one continuous stream, so there's no window to run out of. The direct stream is still listed second, unchanged, if you prefer it.

### Fixes in 1.5.1

- **Cookies are found automatically.** YouTube fetches need a browser cookies file on some networks. The addon used to look in exactly one place (`~/Downloads/cookies.txt`); it now also finds the file Opera's "Get cookies.txt LOCALLY" extension saves (`www.youtube.com_cookies.txt` in `~/Downloads/Temp`) and any other cookies export in Downloads. If your exported cookies have gone stale — browsers rotate them — the addon now retries without cookies instead of failing every video.
- **Live streams behave better.** A live stream's address is re-fetched every minute instead of being remembered for ten, so it can't go stale mid-session. Live items no longer list the HD merge entry (it never applied to live), and when YouTube offers an adaptive master playlist for a live stream, the addon hands Nuvio that — the player can drop to a lower quality on a slow connection instead of stalling on one fixed quality.

### YouTube quality setting

The `/configure` page has a **YouTube quality** setting (Best available, 1080p, 720p, 480p, 360p; 720p is the default). It caps the height of the stream the addon asks yt-dlp for, and it lives in `settings.json` next to `magnets.json`.

One limit to know: YouTube hands this player a single combined file (picture and sound in one stream — the player can't use separate picture-only and sound-only streams). So the addon takes the best combined stream at or under your pick — and on some networks/accounts the only combined stream YouTube offers is a low one (360p), even if you pick higher. The setting still does its job; there just isn't a higher combined stream to give you for that video.

### HD merge (inside the relay stream)

Every YouTube item lists a single app-served **relay** stream, and the merge happens inside it. YouTube's higher resolutions only come as separate picture-only and sound-only streams, so the addon joins them together live with [ffmpeg](https://ffmpeg.org) (which must be installed on the same computer) and serves the combined video itself, at up to the quality setting (up to 1080p) with the best H.264 picture and AAC sound the video offers.

Trade-offs to know: the relay starts slower than a direct link would (the addon has to look the video up and start joining before the first picture shows), and seeking/scrubbing is limited because the video is being built as it plays. If a merge can't be built for a video, the addon sends the player to the plain combined stream instead, so it still plays. Live streams are unaffected by the merge: they use the live relay.

Saved YouTube videos live in their own file, `youtube.json`, next to `magnets.json`, and you can delete them from the configure page too.

## New in 1.6.0 — YouTube channels

You can now follow whole YouTube channels, not just single videos. On the `/configure` page there's a fourth form, **YouTube channel link**: paste a channel address (`youtube.com/@channelname`, `youtube.com/channel/UC...`, `youtube.com/c/...` or `youtube.com/user/...` — a tab suffix like `/videos` is fine), give it a name if you like, and save. The one paste box at the top also recognises channel links and files them the same way. A channel's real name fills in automatically when you don't type one.

The channel's **newest 10 uploads** then appear in the same **My Magnet Links** catalog, after your own saved items. In the list, each channel video is labeled with the channel's name — "Channel Name — Video Title" — so it's clear which channel it came from. The addon re-checks each channel about **every 30 minutes** while it runs, so new uploads show up on their own — you don't paste anything else. If a re-check fails, the last good list stays in place rather than the channel's videos vanishing.

Channel videos play exactly like individually pasted YouTube videos — same quality setting, same HD merge, and if an upload is a live stream it gets the live relay. (The catalog lists a channel's uploads; a channel's *current* live stream lives on its streams tab, so to pin a live channel, add its live video link as a regular YouTube item.)

One caveat, on Nuvio's side: Nuvio holds the addon list it already fetched, so freshly appeared uploads show when Nuvio's list refreshes — reopening Nuvio, or searching the title (search asks the addon live).

Saved channels live in their own file, `channels.json`, next to `magnets.json`. Deleting a channel from the configure page removes its videos from the catalog; the videos themselves were never stored individually.

## Notes

- The addon only hands Nuvio the magnet link; the video itself streams from the torrent through Nuvio.
- Newly added items should now show up in Nuvio without restarting it — the addon tells Nuvio not to cache its lists. If the app still shows an old list, that last bit of caching is on Nuvio's side.
- If you close the addon window, whatever is already playing keeps going, but starting anything new needs the addon running again.
- Port 7777 is used by default; set the `PORT` environment variable to change it.

## License

Free to use and share.
