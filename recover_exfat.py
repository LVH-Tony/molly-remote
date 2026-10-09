#!/usr/bin/env python3
"""Undelete files from an exFAT card (e.g. after a Finder "Replace" wiped a folder). READ-ONLY on the card.

    diskutil unmount "/Volumes/<card>"                      # stop macOS writing to it (don't eject)
    sudo python3 recover_exfat.py /dev/rdisk6s1 "Music/Pop/Taylor Swift" ~/Recovered

exFAT only marks deleted directory entries as unused; names, sizes and first clusters stay until overwritten.
Files written in one go are usually contiguous ("NoFatChain"), so they come back byte-for-byte.
Every recovered file is checked against the card's allocation bitmap: if any of its clusters is in use again,
it's flagged as possibly overwritten. FLACs can then be verified with `flac -t` (built-in MD5).
"""
import os, struct, sys

dev, target, out_root = sys.argv[1], sys.argv[2].strip("/"), os.path.expanduser(sys.argv[3])
fd = os.open(dev, os.O_RDONLY)


def pread(off, n):
    """Raw devices need sector-aligned reads."""
    a = off - off % 512
    b = off + n
    b += (-b) % 512
    data = os.pread(fd, b - a, a)
    return data[off - a:off - a + n]


bs = pread(0, 512)
if bs[3:11] != b"EXFAT   ":
    sys.exit("Not an exFAT partition: %s" % dev)
fat_off, fat_len, heap_off, clus_count, root_clus = struct.unpack_from("<IIIII", bs, 0x50)
bps = 1 << bs[0x6C]
spc = 1 << bs[0x6D]
CS = bps * spc
print(f"exFAT: cluster {CS // 1024} KiB, {clus_count} clusters")


def coff(c):
    return heap_off * bps + (c - 2) * CS


def fat(c):
    return struct.unpack("<I", pread(fat_off * bps + c * 4, 4))[0]


def chain(first, nofat, length):
    n = max(1, -(-length // CS)) if length else 1
    if nofat:
        return list(range(first, first + n))
    out, c = [], first
    while 2 <= c < clus_count + 2 and len(out) < n and c not in out:
        out.append(c)
        c = fat(c)
    # deleting a file can zero its FAT chain; then the best guess is that it was contiguous
    return out if len(out) >= n else list(range(first, first + n))


def read_chain(clusters, length):
    buf = bytearray()
    for c in clusters:
        buf += pread(coff(c), CS)
    return bytes(buf[:length])


def entries(data):
    """Yield entry sets (live and deleted) from directory bytes."""
    i = 0
    while i + 32 <= len(data):
        t = data[i]
        if t == 0x00:
            break
        if t in (0x85, 0x05):
            n = data[i + 1]
            attr = struct.unpack_from("<H", data, i + 4)[0]
            st = data[i + 32:i + 64]
            if len(st) == 32 and st[0] in (0xC0, 0x40):
                flags, nlen = st[1], st[3]
                first, dlen = struct.unpack_from("<IQ", st, 20)
                name = b""
                for k in range(2, n + 1):
                    e = data[i + 32 * k:i + 32 * k + 32]
                    if len(e) == 32 and e[0] in (0xC1, 0x41):
                        name += e[2:32]
                name = name[:nlen * 2].decode("utf-16-le", "replace")
                yield {"name": name, "deleted": t == 0x05, "dir": bool(attr & 0x10),
                       "first": first, "len": dlen, "nofat": bool(flags & 2)}
            i += 32 * (n + 1)
            continue
        i += 32


def read_dir(e):
    return read_chain(chain(e["first"], e["nofat"], e["len"]), e["len"] or CS)


# allocation bitmap from the root directory
root = {"first": root_clus, "nofat": False, "len": 0}
root_data = read_chain(chain(root_clus, False, CS * 64), CS * 64)
bitmap = None
for i in range(0, len(root_data), 32):
    if root_data[i] == 0x81:
        bfirst, blen = struct.unpack_from("<IQ", root_data, i + 20)
        bitmap = read_chain(chain(bfirst, False, blen), blen)
        break
in_use = (lambda c: bool(bitmap[(c - 2) >> 3] >> ((c - 2) & 7) & 1)) if bitmap else (lambda c: False)

# walk to the parent of the target, then take every entry (live or deleted) with the target's name
parts = target.split("/")
cur = {"first": root_clus, "nofat": False, "len": len(root_data)}
cur_data = root_data
for p in parts[:-1]:
    nxt = next((e for e in entries(cur_data) if e["name"] == p and e["dir"] and not e["deleted"]), None)
    if not nxt:
        sys.exit(f"Folder not found: {p}")
    cur, cur_data = nxt, read_dir(nxt)
cands = [e for e in entries(cur_data) if e["name"] == parts[-1] and e["dir"]]
print(f"'{parts[-1]}' entries in '{'/'.join(parts[:-1])}': " +
      ", ".join(("DELETED" if e["deleted"] else "live") + f"@{e['first']}" for e in cands))
keyword = os.environ.get("KEYWORD", parts[-1]).lower()
os.makedirs(out_root, exist_ok=True)
report, seen = [], set()


def recover_file(f, path):
    if not f["deleted"] or f["name"].startswith("._") or f["first"] in seen:
        return
    seen.add(f["first"])
    cl = chain(f["first"], f["nofat"], f["len"])
    busy = sum(1 for c in cl if in_use(c))
    dest = os.path.join(out_root, path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(read_chain(cl, f["len"]))
    status = "ok" if busy == 0 and f["nofat"] else ("OVERWRITTEN?" if busy else "fragmented?")
    report.append(f"{status:13} {f['len'] / 1e6:8.1f} MB  {path}")
    print(report[-1], flush=True)


def recover_dir(e, rel, depth=0):
    for f in entries(read_dir(e)):
        path = os.path.join(rel, f["name"])
        if f["dir"]:
            if depth < 4:
                recover_dir(f, path, depth + 1)
        else:
            recover_file(f, path)


# 1. the folder's own (deleted) entry is still there: walk it
for c in cands:
    recover_dir(c, "")

# 2. Finder reused the deleted folder's slot for the new folder, so nothing points at the old albums any more.
#    Their directory clusters still exist: find free clusters that start like a directory (file entry set)
#    and hold deleted files whose names contain the keyword.
if not any(c["deleted"] for c in cands):
    print(f"\nOld folder entry was reused — scanning free clusters for deleted folders mentioning '{keyword}'…", flush=True)
    album_dirs, parent_names = {}, {}
    step = max(1, clus_count // 50)
    for c in range(2, clus_count + 2):
        if (c - 2) % step == 0:
            print(f"  {100 * (c - 2) // clus_count:3d}%  ({len(album_dirs)} folders found)", flush=True)
        if in_use(c):
            continue
        h = pread(coff(c), 96)
        if h[0] not in (0x85, 0x05) or h[32] not in (0xC0, 0x40) or h[64] not in (0xC1, 0x41):
            continue
        ents = list(entries(pread(coff(c), CS)))
        for e in ents:
            if e["deleted"] and e["dir"]:
                parent_names[e["first"]] = e["name"]   # an old parent listing: remembers album folder names
        if any(e["deleted"] and not e["dir"] and keyword in e["name"].lower() for e in ents):
            album_dirs[c] = ents
    print(f"  100%  ({len(album_dirs)} folders found)\n", flush=True)
    for c, ents in sorted(album_dirs.items()):
        album = parent_names.get(c, f"Recovered folder {c}")
        for f in ents:
            if not f["dir"]:
                recover_file(f, os.path.join(album, f["name"]))

with open(os.path.join(out_root, "_recovery_report.txt"), "w") as fh:
    fh.write("\n".join(report) + "\n")
ok = sum(r.startswith("ok") for r in report)
print(f"\nRecovered {len(report)} files ({ok} clean, {len(report) - ok} need checking) into {out_root}")
uid, gid = int(os.environ.get("SUDO_UID", os.getuid())), int(os.environ.get("SUDO_GID", os.getgid()))
for d, _, fs in os.walk(out_root):
    os.chown(d, uid, gid)
    for f in fs:
        os.chown(os.path.join(d, f), uid, gid)
