package io.github.lvhtony.mollyremote;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * In-app updates from GitHub Releases: the web side finds the newer release, this downloads the APK
 * into the app's cache and hands it to Android's installer (which always asks the user to confirm).
 * Downloads use the default network, so they still work while Wi-Fi is the Poly's internet-less hotspot.
 */
@CapacitorPlugin(name = "Update")
public class UpdatePlugin extends Plugin {

    private volatile boolean busy;

    /** appInfo() -> {versionName, versionCode} */
    @PluginMethod
    public void appInfo(PluginCall call) {
        try {
            Context ctx = getContext();
            PackageInfo p = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
            JSObject ret = new JSObject();
            ret.put("versionName", p.versionName);
            ret.put("versionCode", Build.VERSION.SDK_INT >= 28 ? p.getLongVersionCode() : p.versionCode);
            call.resolve(ret);
        } catch (Exception e) {
            call.reject(e.getMessage());
        }
    }

    /** canInstall() -> {allowed}; Android 8+ needs "Install unknown apps" for this app. */
    @PluginMethod
    public void canInstall(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("allowed", Build.VERSION.SDK_INT < 26 || getContext().getPackageManager().canRequestPackageInstalls());
        call.resolve(ret);
    }

    /** openInstallSettings() — the system screen where the user allows Molly Remote to install updates. */
    @PluginMethod
    public void openInstallSettings(PluginCall call) {
        Intent i = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + getContext().getPackageName()));
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        getContext().startActivity(i);
        call.resolve();
    }

    /** downloadAndInstall({url}) — emits "progress" {received, total}; resolves once the installer is shown. */
    @PluginMethod
    public void downloadAndInstall(PluginCall call) {
        String url = call.getString("url");
        if (url == null || !url.startsWith("https://github.com/") && !url.startsWith("https://objects.githubusercontent.com/")) {
            call.reject("Updates only come from GitHub releases");
            return;
        }
        if (busy) { call.reject("An update is already downloading"); return; }
        busy = true;
        new Thread(() -> {
            File dir = new File(getContext().getCacheDir(), "updates");
            File apk = new File(dir, "MollyRemote.apk");
            try {
                dir.mkdirs();
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setInstanceFollowRedirects(true);
                c.setConnectTimeout(15000);
                c.setReadTimeout(30000);
                c.setRequestProperty("User-Agent", "MollyRemote");
                if (c.getResponseCode() != 200) throw new Exception("Download failed (HTTP " + c.getResponseCode() + ")");
                long total = c.getContentLengthLong(), got = 0, lastEmit = 0;
                try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(apk)) {
                    byte[] buf = new byte[64 * 1024];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n);
                        got += n;
                        if (got - lastEmit > 256 * 1024 || got == total) {
                            lastEmit = got;
                            JSObject p = new JSObject();
                            p.put("received", got);
                            p.put("total", total);
                            notifyListeners("progress", p);
                        }
                    }
                }
                if (total > 0 && got != total) throw new Exception("Download was cut off — try again");
                Uri uri = FileProvider.getUriForFile(getContext(), getContext().getPackageName() + ".fileprovider", apk);
                Intent i = new Intent(Intent.ACTION_VIEW);
                i.setDataAndType(uri, "application/vnd.android.package-archive");
                i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
                getContext().startActivity(i);
                call.resolve();
            } catch (Exception e) {
                call.reject(e.getMessage() != null ? e.getMessage() : "Download failed");
            } finally {
                busy = false;
            }
        }, "app-update").start();
    }
}
