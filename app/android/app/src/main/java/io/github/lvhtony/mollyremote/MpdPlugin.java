package io.github.lvhtony.mollyremote;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.RouteInfo;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.net.wifi.WifiManager;
import android.util.Base64;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import org.json.JSONArray;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Talks MPD's text protocol straight to the Chord Poly over Wi-Fi — the native twin of remote/server.py.
 *
 * Sockets are always opened on the Wi-Fi network: when the phone joins the Poly's hotspot (no internet),
 * Android keeps mobile data as the default route and a plain socket would never reach the Poly.
 */
@CapacitorPlugin(name = "Mpd")
public class MpdPlugin extends Plugin {

    private static final Set<String> BLOCKED = new HashSet<>(Arrays.asList(
        "kill", "config", "mount", "unmount", "password", "idle", "noidle", "close",
        "albumart", "readpicture", "partition", "newpartition", "sendmessage", "subscribe"));

    private final ExecutorService cmdExec = Executors.newSingleThreadExecutor();
    private final ExecutorService artExec = Executors.newSingleThreadExecutor();
    private final ExecutorService miscExec = Executors.newCachedThreadPool();

    private volatile String host;
    private volatile int port = 6600;
    private Conn cmd, art;
    private volatile Thread idleThread;
    private volatile Conn idleConn;

    // ------------------------------------------------------------------ connection

    static class MpdError extends Exception {
        MpdError(String m) { super(m); }
    }

    /** One blocking MPD connection. Not thread-safe; each executor owns one. */
    class Conn {
        final String host; final int port; final int readTimeout;
        Socket sock; InputStream in; OutputStream out; String version;

        Conn(String host, int port, int readTimeout) { this.host = host; this.port = port; this.readTimeout = readTimeout; }

        void open() throws IOException, MpdError {
            Network wifi = wifiNetwork();
            sock = wifi != null ? wifi.getSocketFactory().createSocket() : new Socket();
            sock.connect(new InetSocketAddress(InetAddress.getByName(host), port), 4000);
            sock.setSoTimeout(readTimeout);
            sock.setTcpNoDelay(true);
            in = new BufferedInputStream(sock.getInputStream(), 65536);
            out = sock.getOutputStream();
            String hello = line();
            if (!hello.startsWith("OK MPD")) throw new MpdError("Not an MPD server: " + hello);
            version = hello.substring(7);
        }

        void close() {
            try { if (sock != null) sock.close(); } catch (IOException ignored) { }
            sock = null;
        }

        boolean isOpen() { return sock != null && sock.isConnected() && !sock.isClosed(); }

        void send(String s) throws IOException {
            out.write((s + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();
        }

        String line() throws IOException {
            ByteArrayOutputStream b = new ByteArrayOutputStream(128);
            int c;
            while ((c = in.read()) != -1 && c != '\n') b.write(c);
            if (c == -1) throw new IOException("MPD closed the connection");
            return b.toString("UTF-8");
        }

        byte[] bytes(int n) throws IOException {
            byte[] buf = new byte[n];
            int off = 0;
            while (off < n) {
                int r = in.read(buf, off, n - off);
                if (r < 0) throw new IOException("MPD closed the connection");
                off += r;
            }
            return buf;
        }

        /** Read "key: value" lines until OK; list_OK becomes a ["list_OK",""] marker. */
        List<String[]> pairs() throws IOException, MpdError {
            List<String[]> outList = new ArrayList<>();
            while (true) {
                String l = line();
                if (l.equals("OK")) return outList;
                if (l.startsWith("ACK")) throw new MpdError(l);
                if (l.equals("list_OK")) { outList.add(new String[] { "list_OK", "" }); continue; }
                int i = l.indexOf(": ");
                outList.add(i < 0 ? new String[] { l, "" } : new String[] { l.substring(0, i), l.substring(i + 2) });
            }
        }
    }

    private Network wifiNetwork() {
        ConnectivityManager cm = (ConnectivityManager) getContext().getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) return null;
        for (Network n : cm.getAllNetworks()) {
            NetworkCapabilities caps = cm.getNetworkCapabilities(n);
            if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return n;
        }
        return null;
    }

    private static String quote(String a) {
        return "\"" + a.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    private interface Job<T> { T run(Conn c) throws IOException, MpdError; }

    /** Run on a lazily (re)opened connection, retrying once on a dropped socket. Each kind is only used from its own executor. */
    private <T> T withConn(boolean forArt, Job<T> job) throws IOException, MpdError {
        if (host == null) throw new MpdError("No Poly selected yet");
        for (int attempt = 0; ; attempt++) {
            Conn c = forArt ? art : cmd;
            try {
                if (c == null || !c.isOpen() || !c.host.equals(host)) {
                    if (c != null) c.close();
                    c = new Conn(host, port, 20000);
                    c.open();
                    if (forArt) art = c; else cmd = c;
                }
                return job.run(c);
            } catch (MpdError e) {
                throw e;
            } catch (IOException e) {
                if (c != null) c.close();
                if (forArt) art = null; else cmd = null;
                if (attempt >= 1) throw e;
            }
        }
    }

    private List<List<String[]>> runCommands(Conn c, List<List<String>> commands) throws IOException, MpdError {
        List<String> lines = new ArrayList<>();
        for (List<String> cmdParts : commands) {
            StringBuilder sb = new StringBuilder(cmdParts.get(0));
            for (int i = 1; i < cmdParts.size(); i++) sb.append(' ').append(quote(cmdParts.get(i)));
            lines.add(sb.toString());
        }
        List<List<String[]>> results = new ArrayList<>();
        if (lines.size() == 1) {
            c.send(lines.get(0));
            results.add(c.pairs());
            return results;
        }
        StringBuilder all = new StringBuilder("command_list_ok_begin\n");
        for (String l : lines) all.append(l).append('\n');
        all.append("command_list_end");
        c.send(all.toString());
        List<String[]> cur = new ArrayList<>();
        for (String[] p : c.pairs()) {
            if (p[0].equals("list_OK")) { results.add(cur); cur = new ArrayList<>(); }
            else cur.add(p);
        }
        return results;
    }

    // ------------------------------------------------------------------ plugin API

    /** setHost({host, port}) — choose which Poly to talk to and (re)start the change listener. */
    @PluginMethod
    public void setHost(PluginCall call) {
        String h = call.getString("host");
        if (h == null || h.isEmpty()) { call.reject("host required"); return; }
        synchronized (this) {
            host = h;
            port = call.getInt("port", 6600);
            if (cmd != null) cmd.close();
            if (art != null) art.close();
            cmd = art = null;
        }
        startIdle();
        call.resolve();
    }

    /** run({commands: [["status"], ["add", "x.flac"]]}) -> {results: [[[k, v], ...], ...]} */
    @PluginMethod
    public void run(PluginCall call) {
        JSArray arr = call.getArray("commands");
        cmdExec.execute(() -> {
            try {
                List<List<String>> commands = new ArrayList<>();
                for (int i = 0; i < arr.length(); i++) {
                    JSONArray c = arr.getJSONArray(i);
                    List<String> parts = new ArrayList<>();
                    for (int j = 0; j < c.length(); j++) parts.add(c.getString(j));
                    if (parts.isEmpty() || BLOCKED.contains(parts.get(0))) throw new MpdError("command not allowed");
                    commands.add(parts);
                }
                List<List<String[]>> res = withConn(false, c -> runCommands(c, commands));
                JSArray outer = new JSArray();
                for (List<String[]> r : res) {
                    JSArray inner = new JSArray();
                    for (String[] p : r) inner.put(new JSONArray(Arrays.asList(p[0], p[1])));
                    outer.put(inner);
                }
                JSObject ret = new JSObject();
                ret.put("results", outer);
                call.resolve(ret);
            } catch (MpdError e) {
                call.reject(e.getMessage(), "MPD");
            } catch (Exception e) {
                call.reject("Can't reach the Poly: " + e.getMessage(), "NETWORK");
            }
        });
    }

    /** albumart({uri}) -> {data: base64 | null} */
    @PluginMethod
    public void albumart(PluginCall call) {
        String uri = call.getString("uri", "");
        artExec.execute(() -> {
            JSObject ret = new JSObject();
            try {
                byte[] data = withConn(true, c -> {
                    ByteArrayOutputStream all = new ByteArrayOutputStream();
                    int offset = 0, size = -1;
                    while (size < 0 || offset < size) {
                        c.send("albumart " + quote(uri) + " " + offset);
                        Map<String, String> head = new LinkedHashMap<>();
                        while (true) {
                            String l = c.line();
                            if (l.startsWith("ACK")) return null;
                            int i = l.indexOf(": ");
                            if (i > 0) head.put(l.substring(0, i), l.substring(i + 2));
                            if (l.startsWith("binary: ")) break;
                        }
                        int n = Integer.parseInt(head.get("binary"));
                        size = Integer.parseInt(head.get("size"));
                        all.write(c.bytes(n));
                        c.line();   // newline after the binary chunk
                        c.line();   // OK
                        offset += n;
                        if (n == 0) break;
                    }
                    return all.toByteArray();
                });
                ret.put("data", data == null ? null : Base64.encodeToString(data, Base64.NO_WRAP));
            } catch (Exception e) {
                ret.put("data", null);
            }
            call.resolve(ret);
        });
    }

    /** probe({host, port}) -> {ok, version} — quick "is a Poly answering here?" */
    @PluginMethod
    public void probe(PluginCall call) {
        String h = call.getString("host");
        int p = call.getInt("port", 6600);
        miscExec.execute(() -> {
            JSObject ret = new JSObject();
            Conn c = new Conn(h, p, 3000);
            try { c.open(); ret.put("ok", true); ret.put("version", c.version); }
            catch (Exception e) { ret.put("ok", false); ret.put("error", e.getMessage()); }
            finally { c.close(); }
            call.resolve(ret);
        });
    }

    /**
     * discover({timeout}) -> {devices: [{name, host, port, via}], wifi: bool}
     * mDNS (_mpd._tcp) finds the Poly on any Wi-Fi; the Wi-Fi gateway check finds it in hotspot mode,
     * where the Poly *is* the router.
     */
    @PluginMethod
    public void discover(PluginCall call) {
        int timeout = call.getInt("timeout", 3500);
        miscExec.execute(() -> {
            Map<String, JSObject> found = new LinkedHashMap<>();
            Network wifi = wifiNetwork();

            // 1. hotspot: try the Wi-Fi gateway
            String gw = gatewayIp(wifi);
            if (gw != null) {
                Conn c = new Conn(gw, 6600, 2500);
                try {
                    c.open();
                    JSObject d = new JSObject();
                    d.put("name", "Poly hotspot"); d.put("host", gw); d.put("port", 6600); d.put("via", "hotspot");
                    found.put(gw, d);
                } catch (Exception ignored) {
                } finally { c.close(); }
            }

            // 2. mDNS
            WifiManager wm = (WifiManager) getContext().getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            WifiManager.MulticastLock lock = wm != null ? wm.createMulticastLock("molly-mdns") : null;
            NsdManager nsd = (NsdManager) getContext().getSystemService(Context.NSD_SERVICE);
            List<NsdServiceInfo> pending = new ArrayList<>();
            NsdManager.DiscoveryListener listener = new NsdManager.DiscoveryListener() {
                public void onDiscoveryStarted(String t) { }
                public void onDiscoveryStopped(String t) { }
                public void onStartDiscoveryFailed(String t, int e) { }
                public void onStopDiscoveryFailed(String t, int e) { }
                public void onServiceLost(NsdServiceInfo s) { }
                public void onServiceFound(NsdServiceInfo s) { synchronized (pending) { pending.add(s); } }
            };
            try {
                if (lock != null) { lock.setReferenceCounted(false); lock.acquire(); }
                nsd.discoverServices("_mpd._tcp", NsdManager.PROTOCOL_DNS_SD, listener);
                Thread.sleep(timeout);
                try { nsd.stopServiceDiscovery(listener); } catch (Exception ignored) { }
                List<NsdServiceInfo> todo;
                synchronized (pending) { todo = new ArrayList<>(pending); }
                for (NsdServiceInfo s : todo) {                       // resolve one at a time (older Androids allow only one)
                    final Object done = new Object();
                    final NsdServiceInfo[] out = new NsdServiceInfo[1];
                    nsd.resolveService(s, new NsdManager.ResolveListener() {
                        public void onResolveFailed(NsdServiceInfo si, int e) { synchronized (done) { done.notify(); } }
                        public void onServiceResolved(NsdServiceInfo si) { out[0] = si; synchronized (done) { done.notify(); } }
                    });
                    synchronized (done) { done.wait(2500); }
                    if (out[0] != null && out[0].getHost() != null) {
                        String ip = out[0].getHost().getHostAddress();
                        if (ip == null || ip.contains(":")) continue; // skip IPv6 link-local
                        String name = out[0].getServiceName().replaceFirst("^Music Player @ ", "");
                        JSObject d = found.containsKey(ip) ? found.get(ip) : new JSObject();
                        d.put("name", name); d.put("host", ip); d.put("port", out[0].getPort());
                        d.put("via", found.containsKey(ip) ? "hotspot" : "wifi");
                        found.put(ip, d);
                    }
                }
            } catch (Exception ignored) {
            } finally {
                if (lock != null && lock.isHeld()) lock.release();
            }
            JSArray devices = new JSArray();
            for (JSObject d : found.values()) devices.put(d);
            JSObject ret = new JSObject();
            ret.put("devices", devices);
            ret.put("wifi", wifi != null);
            call.resolve(ret);
        });
    }

    private String gatewayIp(Network wifi) {
        if (wifi == null) return null;
        ConnectivityManager cm = (ConnectivityManager) getContext().getSystemService(Context.CONNECTIVITY_SERVICE);
        LinkProperties lp = cm.getLinkProperties(wifi);
        if (lp == null) return null;
        for (RouteInfo r : lp.getRoutes()) {
            if (r.isDefaultRoute() && r.getGateway() != null && r.getGateway().getAddress().length == 4) {
                return r.getGateway().getHostAddress();
            }
        }
        return null;
    }

    // ------------------------------------------------------------------ live updates

    private void emitConnection(boolean ok, String err) {
        JSObject o = new JSObject();
        o.put("connected", ok);
        if (err != null) o.put("error", err);
        notifyListeners("connection", o, true);
    }

    /** One idle loop per selected host; emits "changed" {changed:[...]} like the bridge's SSE. */
    private synchronized void startIdle() {
        if (idleThread != null) idleThread.interrupt();
        if (idleConn != null) idleConn.close();
        final String h = host; final int p = port;
        Thread t = new Thread(() -> {
            while (!Thread.currentThread().isInterrupted() && h.equals(host)) {
                Conn c = new Conn(h, p, 25000);
                idleConn = c;
                try {
                    c.open();
                    emitConnection(true, null);
                    while (!Thread.currentThread().isInterrupted()) {
                        c.send("idle");
                        List<String[]> res;
                        try {
                            res = c.pairs();
                        } catch (SocketTimeoutException quiet) {
                            c.send("noidle");                         // heartbeat: proves the Poly is still there
                            res = c.pairs();
                        }
                        JSArray changed = new JSArray();
                        for (String[] pr : res) if (pr[0].equals("changed")) changed.put(pr[1]);
                        if (changed.length() > 0) {
                            JSObject o = new JSObject();
                            o.put("changed", changed);
                            notifyListeners("changed", o);
                        }
                    }
                } catch (Exception e) {
                    if (Thread.currentThread().isInterrupted()) break;
                    emitConnection(false, e.getMessage());
                    try { Thread.sleep(3000); } catch (InterruptedException ie) { break; }
                } finally {
                    c.close();
                }
            }
        }, "mpd-idle");
        t.setDaemon(true);
        idleThread = t;
        t.start();
    }

    @Override
    protected void handleOnDestroy() {
        if (idleThread != null) idleThread.interrupt();
        if (idleConn != null) idleConn.close();
        if (cmd != null) cmd.close();
        if (art != null) art.close();
    }
}
