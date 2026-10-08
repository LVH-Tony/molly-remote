# Molly Remote

A better remote for the **Chord Poly** (with Mojo 2), replacing GoFigure for playing music from the Poly's SD card. You can use it in any browser on the home network, or install it as an Android app that works with no internet, including with the Poly in hotspot mode.

The Poly runs a standard music server (MPD) on port 6600. Molly Remote talks to it directly.

> **Not affiliated with or endorsed by Chord Electronics.** "Chord", "Poly", "Mojo" and "GoFigure" are their trademarks and are named here only to say what this works with. A Poly firmware update could break it, and there is no warranty (see [LICENSE](LICENSE)).

## What it does
- Instant search across the whole SD card, with or without accents (`tuan` finds *Hà Anh Tuấn*)
- Tap any song to play the list from that song
- Browse playlists (grouped), artists, albums, genres and folders, all with album covers
- Per-song menu: play now, play next, add to queue, add to playlist, go to album or artist, move up or down
- Tick several songs for bulk actions
- Manage playlists: create, rename, duplicate, delete, reorder, sort, and remove duplicate or missing songs
- Manage the queue: reorder, shuffle, remove duplicates, save as a playlist
- Library scan with a progress indicator (GoFigure's "Update SD card" button sometimes does nothing)
- Live updates when something changes on the Poly; works on phones, foldables, tablets and desktop

## Install on Android
1. Make sure the Poly is on your home Wi-Fi. If you've only ever used it in hotspot mode, join it to your Wi-Fi once in GoFigure.
2. On your phone, download `MollyRemote.apk` from the **Releases** page and open it. Android will ask you to allow installs from your browser, and Play Protect may warn about an unknown developer; tap **Install anyway**.
3. Connect the phone to the same Wi-Fi as the Poly and open the app. It finds the Poly by itself.
4. First time only: if songs or playlists are missing, tap **More → Scan for new music**.

**Outside the house:** switch the Poly to **Hotspot mode** in GoFigure, then join the Poly's Wi-Fi on your phone. If Android says the network has no internet, choose **Keep connection**. No internet is needed.

You still need GoFigure for Poly settings: hotspot or network mode, and the DSD / bit-perfect switch.

**Copying music from a Mac?** A Mac leaves hidden `._*` files on the SD card that the Poly shows as **corrupted tracks**. Run `dot_clean -m "/Volumes/<SD card name>"` before ejecting, or use the SD card tools below. Album covers only show if the album folder has a `cover.jpg`; `covers.py` adds them.

## Use it in a browser (Mac or PC)
The browser needs a small bridge to talk to the Poly. It's Python 3 with nothing else to install.
```bash
python3 remote/server.py                          # finds the Poly on the network by itself
python3 remote/server.py --mpd 192.168.1.50       # or give its IP address
```
Then open http://localhost:8686. Any phone on the same Wi-Fi can use `http://<computer-ip>:8686` too. On a Mac, you can double-click `remote/Molly Remote.command` instead.

## SD card tools
Run these with the card in your computer (they need `ffmpeg` installed: `brew install ffmpeg`):
```bash
python3 make_playlists.py "/Volumes/<SD card name>"   # rebuilds the playlists, adds covers, cleans macOS junk files
python3 covers.py "/Volumes/<SD card name>"           # covers only
```
- **Covers:** the Poly can only show a `cover.jpg` sitting next to the songs. `covers.py` uses an image already in the folder, else the art embedded in the songs. If an album has neither, it looks it up online (iTunes, then MusicBrainz). It never changes your music files.
- **Playlists:** `make_playlists.py` only replaces the numbered playlists it creates ("1. Mix - …"). Playlists you make yourself are kept. Which playlists it makes comes from `playlists.json`: copy `playlists.example.json` and edit it for your card. Without it you get "Everything" plus one playlist per top-level folder.
- **macOS junk files:** a Mac writes hidden `._*` files onto SD cards, and the Poly shows them as **corrupted tracks**. Both scripts clean them up. You can also run `dot_clean -m "/Volumes/<SD card name>"` after copying music.

## Security: there's no password
The Poly's music server has no login, so **anyone on the same Wi-Fi can control playback and edit or delete playlists**, whether they use this app, GoFigure or any MPD client. That's normal for home audio gear, but keep it in mind on shared networks.

The browser bridge (`server.py`) has no login either, and by default it listens on your whole network so phones can use it. To use it only on the computer it runs on, start it with `--bind 127.0.0.1`. Don't expose port 8686 or 6600 to the internet.

The app and bridge never send anything off your network. The only exception is `covers.py`, which looks up missing album covers by artist and album name on iTunes and MusicBrainz.

## Troubleshooting
| Problem | Fix |
|---|---|
| "Playlist malformed" / songs "corrupted" | Run `make_playlists.py` to remove the `._` files, then **Settings → Scan for new music** |
| App can't find the Poly | Check it's on the same Wi-Fi or hotspot, then **More → Change… → Search again**, or type its IP address |
| No covers | Run `covers.py`, put the card back in the Poly, then **Scan for new music** |

## Build the Android app yourself
You need Android Studio, for its bundled Java and the Android SDK, and Node.js.
```bash
cd app && npm install && npm run apk          # debug build -> app/MollyRemote.apk
```
`npm run release` builds a signed release. It needs your own signing key, set up in `~/.gradle/gradle.properties` as `MOLLY_RELEASE_STORE_FILE`, `MOLLY_RELEASE_STORE_PASSWORD`, `MOLLY_RELEASE_KEY_ALIAS` and `MOLLY_RELEASE_KEY_PASSWORD`. Never commit a keystore. An APK you build yourself won't install over the one from Releases, because the signing keys differ; uninstall that one first.
`remote/index.html` is the one shared web app; `npm run apk` copies it into the Android project. The native part is `app/android/app/src/main/java/io/github/lvhtony/mollyremote/MpdPlugin.java`. It connects over Wi-Fi even when the hotspot has no internet, finds the Poly by mDNS or as the hotspot router, and passes live updates to the app.
