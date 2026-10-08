#!/usr/bin/env python3
"""Molly Remote — a web remote for the Chord Poly (MPD) on the local network.

    python3 server.py                     # finds the Poly on the network; UI on http://localhost:8686
    python3 server.py --mpd 192.168.1.50 --port 8686

Stdlib only. The browser can't speak MPD's raw TCP protocol, so this bridges it:
  POST /api/mpd     {"commands": [["status"], ["add", "Music/x.flac"]]} -> [[[key, value], ...], ...]
  GET  /api/events  Server-Sent Events; one "change" event per MPD idle wake-up
  GET  /api/art     ?uri=<song uri> -> cover image (MPD albumart), cached
"""
import argparse, json, os, queue, re, shutil, socket, subprocess, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
BLOCKED = {"kill", "config", "mount", "unmount", "password", "idle", "noidle", "close",
           "albumart", "readpicture", "partition", "newpartition", "sendmessage", "subscribe"}


class MPDError(Exception):
    pass


def quote(arg):
    return '"' + str(arg).replace("\\", "\\\\").replace('"', '\\"') + '"'


class MPD:
    """One blocking MPD connection, reconnecting on failure."""

    def __init__(self, host, port, timeout=15):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = self.f = None
        self.lock = threading.Lock()

    def _connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.f = self.sock.makefile("rb")
        hello = self.f.readline().decode()
        if not hello.startswith("OK MPD"):
            raise MPDError("not an MPD server: " + hello.strip())

    def _close(self):
        try:
            self.sock and self.sock.close()
        except OSError:
            pass
        self.sock = self.f = None

    def _send(self, line):
        self.sock.sendall((line + "\n").encode())

    def _read_pairs(self):
        out = []
        while True:
            line = self.f.readline()
            if not line:
                raise ConnectionError("MPD closed the connection")
            line = line.decode("utf-8", "replace").rstrip("\n")
            if line == "OK":
                return out
            if line.startswith("ACK"):
                raise MPDError(line)
            if line == "list_OK":
                out.append(["list_OK", ""])
                continue
            k, _, v = line.partition(": ")
            out.append([k, v])

    def _with_retry(self, fn):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if not self.sock:
                        self._connect()
                    return fn()
                except MPDError:
                    raise
                except (OSError, ConnectionError):
                    self._close()
                    if attempt:
                        raise

    def run(self, commands):
        """Run a batch of [name, *args]; returns one list of pairs per command."""
        for c in commands:
            if not c or c[0] in BLOCKED:
                raise MPDError("command not allowed: %s" % (c[0] if c else ""))
        lines = [" ".join([c[0]] + [quote(a) for a in c[1:]]) for c in commands]

        def go():
            if len(lines) == 1:
                self._send(lines[0])
                return [self._read_pairs()]
            self._send("\n".join(["command_list_ok_begin"] + lines + ["command_list_end"]))
            results, cur = [], []
            for k, v in self._read_pairs():
                if k == "list_OK":
                    results.append(cur)
                    cur = []
                else:
                    cur.append([k, v])
            return results
        return self._with_retry(go)

    def albumart(self, uri):
        def go():
            data, offset, size = b"", 0, None
            while size is None or offset < size:
                self._send("albumart %s %d" % (quote(uri), offset))
                head = {}
                while True:
                    line = self.f.readline().decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("ACK"):
                        return None
                    k, _, v = line.partition(": ")
                    head[k] = v
                    if k == "binary":
                        break
                n = int(head["binary"])
                size = int(head["size"])
                chunk = self.f.read(n)
                self.f.readline()           # newline after binary
                self.f.readline()           # OK
                data += chunk
                offset += n
                if n == 0:
                    break
            return data
        return self._with_retry(go)


class Hub:
    """Single MPD idle loop fanned out to every SSE client."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.clients = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def subscribe(self):
        q = queue.Queue(maxsize=50)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def _publish(self, msg):
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    def _loop(self):
        while True:
            try:
                conn = MPD(self.host, self.port, timeout=None)
                conn._connect()
                self._publish({"connected": True})
                while True:
                    conn._send("idle")
                    changed = [v for k, v in conn._read_pairs() if k == "changed"]
                    self._publish({"changed": changed})
            except Exception as e:  # noqa: BLE001 — keep the loop alive whatever happens
                self._publish({"connected": False, "error": str(e)})
                time.sleep(3)


def make_handler(mpd, art_mpd, hub, info):
    art_cache = {}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
            if u.path == "/MollyRemote.apk":  # phone install: open http://<mac>:8686/MollyRemote.apk
                apk = os.path.join(HERE, "..", "app", "MollyRemote.apk")
                if os.path.exists(apk):
                    with open(apk, "rb") as fh:
                        return self._send(200, fh.read(), "application/vnd.android.package-archive",
                                          {"Content-Disposition": 'attachment; filename="MollyRemote.apk"'})
            if u.path == "/api/info":
                return self._send(200, info)
            if u.path == "/api/events":
                return self._events()
            if u.path == "/api/art":
                uri = parse_qs(u.query).get("uri", [""])[0]
                key = os.path.dirname(uri)
                data, at = art_cache.get(key, (None, 0))
                if data is None and time.time() - at > 300:  # re-check folders without a cover every 5 min
                    try:
                        data = art_mpd.albumart(uri)
                    except Exception:  # noqa: BLE001
                        data = None
                    art_cache[key] = (data, time.time())
                if not data:
                    return self._send(404, b"", "text/plain")
                ctype = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
                return self._send(200, data, ctype, {"Cache-Control": "max-age=86400"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/mpd":
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                self._send(200, {"results": mpd.run(body["commands"])})
            except MPDError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._send(502, {"error": "Can't reach the Poly: %s" % e})

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.subscribe()
            try:
                self.wfile.write(b": hello\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=20)
                        self.wfile.write(("data: %s\n\n" % json.dumps(msg)).encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                hub.unsubscribe(q)
                self.close_connection = True

    return H


def find_poly(timeout=3):
    """Look for an MPD server via mDNS ("Music Player @ <name>" -> <name>.local). macOS dns-sd or Linux avahi."""
    try:
        if shutil.which("avahi-browse"):
            out = subprocess.run(["avahi-browse", "-rtp", "_mpd._tcp"], capture_output=True, text=True, timeout=timeout + 5).stdout
            for line in out.splitlines():
                f = line.split(";")
                if f[0] == "=" and f[2] == "IPv4":
                    return f[7], f[3].replace("Music Player @ ", "")
        elif shutil.which("dns-sd"):
            p = subprocess.Popen(["dns-sd", "-B", "_mpd._tcp", "local."], stdout=subprocess.PIPE, text=True)
            time.sleep(timeout)
            p.terminate()
            for line in p.stdout.read().splitlines():
                m = re.search(r"\sAdd\s.*_mpd\._tcp\.\s+(.+)$", line)
                if m:
                    name = m.group(1).strip().replace("Music Player @ ", "")
                    return name + ".local", name
    except Exception:  # noqa: BLE001 — fall back to the default name
        pass
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mpd", default=os.environ.get("MOLLY_HOST"), help="Poly hostname or IP (default: find it via mDNS)")
    ap.add_argument("--mpd-port", type=int, default=6600)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8686)))
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()
    if not a.mpd:
        found, name = find_poly()
        a.mpd = found or "Poly.local"
        print("Found %s on the network" % name if found else "No Poly found by mDNS — trying Poly.local (use --mpd <ip> to set it)")
    host = a.mpd
    try:  # resolve mDNS once; .local lookups on every reconnect are slow
        host = socket.gethostbyname(a.mpd)
    except OSError:
        pass
    lan = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, a.mpd_port))
        lan = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    info = {"mpd": "%s:%d" % (host, a.mpd_port), "name": a.mpd,
            "lan_url": "http://%s:%d" % (lan, a.port) if lan else ""}
    mpd, art_mpd = MPD(host, a.mpd_port), MPD(host, a.mpd_port)
    srv = ThreadingHTTPServer((a.bind, a.port), make_handler(mpd, art_mpd, Hub(host, a.mpd_port), info))
    srv.daemon_threads = True
    print("Molly Remote → Poly at %s:%d" % (host, a.mpd_port))
    print("  this Mac:  http://localhost:%d" % a.port)
    if lan:
        print("  phone/LAN: http://%s:%d" % (lan, a.port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
