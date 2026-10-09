#!/usr/bin/env python3
"""Give every album folder on the SD card a cover.jpg — the only art the Poly (MPD albumart) can serve.

    python3 covers.py "/Volumes/<SD card>"

Per folder, first hit wins:
  1. a cover.* already there                        -> keep
  2. an image in the folder (or its parent)         -> copy it to cover.jpg
  3. art embedded in one of the songs               -> extract it
  4. nothing at all                                 -> look the album up online (iTunes, then MusicBrainz/Cover Art Archive)
Everything is saved as an 800px JPEG. Music files are only read, never modified.
"""
import difflib, json, os, re, shutil, subprocess, sys, time, unicodedata, urllib.parse, urllib.request

if len(sys.argv) < 2:
    sys.exit('usage: python3 covers.py "/Volumes/<SD card>"')
ROOT = sys.argv[1]
AUDIO = (".flac", ".dsf", ".dff", ".m4a", ".wav", ".mp3", ".aiff", ".aif")
IMG = (".jpg", ".jpeg", ".png", ".webp")
UA = "MollyRemote/1.0 (personal music library cover fetcher)"


def nat(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def images(d):
    try:
        return sorted((f for f in os.listdir(d) if f.lower().endswith(IMG) and not f.startswith(".")),
                      key=lambda f: (not re.search(r"cover|folder|front", f, re.I), nat(f)))
    except OSError:
        return []


def to_jpeg(src, out):
    """Any image or audio-with-art -> 800px JPEG. True on success."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-an", "-map", "0:v:0", "-frames:v", "1",
                        "-vf", "scale='min(800,iw)':-2", "-q:v", "3", out], capture_output=True)
    if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
        return True
    if os.path.exists(out):
        os.remove(out)
    return False


def tags(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", path],
                       capture_output=True, text=True)
    try:
        t = {k.lower(): v for k, v in json.loads(r.stdout).get("format", {}).get("tags", {}).items()}
    except ValueError:
        t = {}
    return t.get("album_artist") or t.get("albumartist") or t.get("artist") or "", t.get("album") or ""


def fold(s):
    s = unicodedata.normalize("NFD", s or "").replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def clean(s):
    """Strip edition / quality / catalog noise so the album name matches store listings."""
    s = re.sub(r"\[[^\]]*(khz|bit|dsd|explicit)[^\]]*\]", "", s, flags=re.I)
    s = re.sub(r"\((explicit|deluxe[^)]*|remaster[^)]*|\d{4} remaster[^)]*)\)", "", s, flags=re.I)
    s = re.sub(r"\s-\s[A-Z0-9]{4,}$", "", s)        # " - FR718SACD" NativeDSD catalog codes
    s = re.sub(r"DSD \d+fs.*$", "", s, flags=re.I)
    s = s.replace("_", " ")
    return re.sub(r"\s+", " ", s).strip(" -")


def sim(a, b):
    a, b = fold(clean(a)), fold(clean(b))
    if not a or not b:
        return 0
    short, long_ = sorted((a, b), key=len)
    if short in long_ and len(short) >= 0.6 * len(long_):  # "hybrid theory" vs "hybrid theory (deluxe edition)"
        return 1
    return difflib.SequenceMatcher(None, a, b).ratio()


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
    return data if binary else json.loads(data)


def online(artist, album):
    """Return (image_bytes, source) or (None, reason)."""
    q_album, q_artist = clean(album), clean(artist)
    # iTunes: best quality, great for pop/rock/Vietnamese releases
    try:
        res = get("https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": f"{q_artist} {q_album}", "entity": "album", "limit": 10}))["results"]
        best = max(res, key=lambda r: sim(album, r.get("collectionName", "")) * 2 + sim(artist, r.get("artistName", "")), default=None)
        if best and sim(album, best["collectionName"]) >= 0.6 and (not artist or sim(artist, best["artistName"]) >= 0.5):
            url = best["artworkUrl100"].replace("100x100bb", "1000x1000bb")
            return get(url, True), f"iTunes: {best['artistName']} — {best['collectionName']}"
    except Exception as e:  # noqa: BLE001
        print("    iTunes lookup failed:", e)
    # MusicBrainz + Cover Art Archive: good for classical / audiophile / soundtracks
    try:
        time.sleep(1.1)  # MusicBrainz asks for <= 1 request/second
        query = f'release:"{q_album}"' + (f' AND artist:"{q_artist}"' if q_artist else "")
        rels = get("https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 10}))["releases"]
        for rel in sorted(rels, key=lambda r: -r.get("score", 0)):
            credit = " ".join(c.get("name", "") for c in rel.get("artist-credit", []))
            if rel.get("score", 0) < 80 or sim(album, rel["title"]) < 0.6:
                continue
            try:
                return get(f"https://coverartarchive.org/release/{rel['id']}/front-500", True), f"MusicBrainz: {credit} — {rel['title']}"
            except Exception:  # noqa: BLE001 — this release has no art; try the next
                continue
    except Exception as e:  # noqa: BLE001
        print("    MusicBrainz lookup failed:", e)
    return None, "no confident match online"


def main():
    dirs = {}
    for d, sub, files in os.walk(ROOT):
        sub[:] = [x for x in sub if not x.startswith(".")]
        songs = sorted((f for f in files if f.lower().endswith(AUDIO) and not f.startswith(".")), key=nat)
        if songs:
            dirs[d] = songs
    stats = {"had cover": 0, "from folder image": 0, "from embedded art": 0, "from online": 0, "still missing": 0}
    missing = []
    for d in sorted(dirs, key=nat):
        rel = os.path.relpath(d, ROOT)
        out = os.path.join(d, "cover.jpg")
        if any(f.lower().startswith("cover.") for f in images(d)):
            stats["had cover"] += 1
            continue
        if images(d) and to_jpeg(os.path.join(d, images(d)[0]), out):
            stats["from folder image"] += 1
            print(f"  folder image  {rel}")
            continue
        if any(to_jpeg(os.path.join(d, s), out) for s in dirs[d][:4]):
            stats["from embedded art"] += 1
            print(f"  embedded      {rel}")
            continue
        # e.g. NativeDSD/<Album>/DSD 128fs - 2ch/ with the artwork one level up — only when that parent holds just this album
        parent = os.path.dirname(d)
        only_child = parent != ROOT and sum(os.path.isdir(os.path.join(parent, x)) and not x.startswith(".") for x in os.listdir(parent)) == 1
        if only_child and images(parent) and to_jpeg(os.path.join(parent, images(parent)[0]), out):
            stats["from folder image"] += 1
            print(f"  parent image  {rel}")
            continue
        artist, album = tags(os.path.join(d, dirs[d][0]))
        if not album:
            # Untagged loose songs in an artist folder (e.g. Music/Rock-Metal/<Artist>/*.flac): borrow one of its albums' covers
            sub = sorted(x for x in os.listdir(d) if os.path.isfile(os.path.join(d, x, "cover.jpg")))
            if sub:
                shutil.copyfile(os.path.join(d, sub[0], "cover.jpg"), out)
                stats["from folder image"] += 1
                print(f"  sibling album {rel}   <- {sub[0]}")
                continue
            # No tags means no reliable artist to check a match against, so don't guess
            stats["still missing"] += 1
            missing.append(f"{rel}   (songs have no tags — add a cover.jpg by hand)")
            continue
        data, how = online(artist, album)
        if data:
            tmp = out + ".dl"
            with open(tmp, "wb") as fh:
                fh.write(data)
            ok = to_jpeg(tmp, out)
            os.remove(tmp)
            if ok:
                stats["from online"] += 1
                print(f"  online        {rel}   <- {how}")
                continue
        stats["still missing"] += 1
        missing.append(f"{rel}   ({artist} — {album}: {how})")
    print("\nCover art summary:", ", ".join(f"{k}: {v}" for k, v in stats.items()))
    for m in missing:
        print("  no cover:", m)
    subprocess.run(["dot_clean", "-m", ROOT], stderr=subprocess.DEVNULL)
    # dot_clean can't pair up some accented names; any "._" file left over is still junk to the Poly
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            if f.startswith("._"):
                for name in {f, unicodedata.normalize("NFC", f), unicodedata.normalize("NFD", f)}:
                    try:
                        os.remove(os.path.join(d, name))
                        break
                    except FileNotFoundError:   # exFAT on macOS may list a name in a form it won't open
                        continue


if __name__ == "__main__":
    main()
