#!/usr/bin/env python3
"""Rebuild the Poly's playlists from what is actually on the SD card.

    python3 make_playlists.py "/Volumes/<SD card>"                       # uses playlists.json next to this script
    python3 make_playlists.py "/Volumes/<SD card>" --config my.json
    python3 make_playlists.py "/Volumes/<SD card>" --no-covers           # skip cover.jpg generation

Without a config it makes "Everything" plus one playlist per top-level folder.
See playlists.example.json for mixes, per-artist and per-album playlists.

Only playlists whose names start with "<digit>. " are replaced, so ones you make in Molly Remote are kept.
"""
import argparse, json, os, re, subprocess, sys, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIO = (".flac", ".dsf", ".dff", ".m4a", ".wav", ".mp3", ".aiff", ".aif")
DEFAULT = {"playlists": {"1. All - Everything": ["*"]},
           "per_folder": [{"folder": "", "prefix": "2. Folder - "}],
           "min_tracks": 5, "rename": {}}


def nfc(s):
    # macOS hands back accented names (Vietnamese, é, á) decomposed (NFD); the Poly and our config use NFC.
    return unicodedata.normalize("NFC", s)


def nat(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", nfc(s))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="SD card folder, e.g. /Volumes/POLY")
    ap.add_argument("--config", default=os.path.join(HERE, "playlists.json"))
    ap.add_argument("--no-covers", action="store_true")
    a = ap.parse_args()
    root = a.root
    cfg = DEFAULT
    if os.path.exists(a.config):
        with open(a.config, encoding="utf-8") as fh:
            cfg = {**DEFAULT, **json.load(fh)}
    else:
        print(f"(no {os.path.basename(a.config)} found — using defaults; see playlists.example.json)")

    tracks = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            if f.lower().endswith(AUDIO) and not f.startswith("."):
                tracks.append(nfc(os.path.relpath(os.path.join(d, f), root)))
    tracks.sort(key=nat)

    if not a.no_covers:
        # Cover art for every album folder (keeps what's there, extracts embedded art, fetches online only if none)
        subprocess.run([sys.executable, "-I", os.path.join(HERE, "covers.py"), root])

    def under(prefixes):
        """Tracks inside any of these folders. "*" = everything; a trailing "*" matches folder-name prefixes."""
        if "*" in prefixes:
            return list(tracks)
        pats = [nfc(p[:-1] if p.endswith("*") else p.rstrip("/") + "/") for p in prefixes]
        return [t for t in tracks if any(t.startswith(p) for p in pats)]

    def clean(name):
        for old, new in cfg.get("rename", {}).items():
            name = name.replace(old, new)
        return name

    lists = {}
    for name, prefixes in cfg.get("playlists", {}).items():
        if not name.startswith("_"):
            lists[name] = under(prefixes)
    for rule in cfg.get("per_folder", []):
        base = rule["folder"].strip("/")
        path = os.path.join(root, base)
        if not os.path.isdir(path):
            continue
        for sub in sorted(os.listdir(path), key=nat):
            if sub.startswith(".") or not os.path.isdir(os.path.join(path, sub)):
                continue
            t = under([f"{base}/{sub}" if base else sub])
            if len(t) >= rule.get("min_tracks", cfg.get("min_tracks", 5)):
                lists.setdefault(rule["prefix"] + clean(nfc(sub)), t)

    # drop previously generated playlists ("1. Mix - …" etc.), keep ones you made yourself, then write fresh
    for f in os.listdir(root):
        if f.lower().endswith(".m3u") and re.match(r"\d\. ", f):
            os.remove(os.path.join(root, f))
    for name, t in lists.items():
        if not t:
            print("EMPTY, skipped:", name)
            continue
        with open(os.path.join(root, name + ".m3u"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(t) + "\n")
        print(f"{len(t):5}  {name}")

    # macOS drops "._" junk files next to everything it writes on exFAT; the Poly reads them as broken tracks.
    subprocess.run(["dot_clean", "-m", root], stderr=subprocess.DEVNULL)
    # dot_clean can't pair up some accented names; any "._" file left over is still junk to the Poly
    for d, dirs, files in os.walk(root):
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
