package com.tradebot.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.InstallSourceInfo;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * Keeps the app up to date from your own bot server (GET /app/version.json and /app/TradeBot.apk).
 *
 * - Checks when the app opens (at most every 6 hours) or when you tap "Check for updates".
 * - Downloads the new APK in the background and checks its SHA-256 against version.json.
 * - Android itself refuses any update not signed with the same key as the installed app.
 * - After the first update, later ones install silently the next time you leave the app
 *   (Android 12+). The first one needs one tap on Android's "Update" screen.
 */
final class Updater {
    private static final long CHECK_EVERY_MS = 6 * 60 * 60 * 1000L;
    private static final String PREFS = "updater";

    private final Activity activity;
    private volatile int readySession = -1;
    private volatile String readyName;
    private volatile boolean busy;

    Updater(Activity activity) {
        this.activity = activity;
    }

    static long installedVersion(Context c) {
        try {
            return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).getLongVersionCode();
        } catch (Exception e) {
            return 0;
        }
    }

    static String installedName(Context c) {
        try {
            return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "?";
        }
    }

    /** Called when the app opens. Quietly does nothing if checked recently or nothing is new. */
    void checkInBackground(String server) {
        SharedPreferences p = activity.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        if (System.currentTimeMillis() - p.getLong("last", 0) < CHECK_EVERY_MS) return;
        check(server, false);
    }

    /** manual = the user tapped "Check for updates", so always report the outcome. */
    void check(String server, boolean manual) {
        if (busy) { if (manual) toast("Already checking for an update…"); return; }
        if (readySession != -1) { if (manual) askToInstall(); return; }
        busy = true;
        new Thread(() -> {
            try {
                run(server, manual);
            } catch (Exception e) {
                if (manual) toast("Update check failed: " + e.getMessage());
            } finally {
                busy = false;
            }
        }).start();
    }

    private void run(String server, boolean manual) throws Exception {
        JSONObject v = new JSONObject(get(server + "/app/version.json"));
        activity.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putLong("last", System.currentTimeMillis()).apply();
        long latest = v.getLong("versionCode");
        String name = v.optString("versionName", String.valueOf(latest));
        String sha = v.getString("sha256");
        if (latest <= installedVersion(activity)) {
            if (manual) toast("TradeBot is up to date (" + installedName(activity) + ")");
            return;
        }
        if (!activity.getPackageManager().canRequestPackageInstalls()) {
            activity.runOnUiThread(this::askForInstallPermission);
            return;
        }
        if (manual) toast("Downloading update " + name + "…");
        int session = download(server + "/app/TradeBot.apk", sha);
        readySession = session;
        readyName = name;
        if (canInstallSilently()) {
            if (manual) askToInstall();   // tapped by hand: offer to install right away
            // otherwise it installs when the app goes to the background (see onAppHidden)
        } else {
            activity.runOnUiThread(this::askToInstall);
        }
    }

    /** Call from Activity.onStop: installs a downloaded update while you're not looking at the app. */
    void onAppHidden() {
        if (readySession != -1 && canInstallSilently()) commit();
    }

    private boolean canInstallSilently() {
        if (Build.VERSION.SDK_INT < 31) return false;
        try {
            InstallSourceInfo info = activity.getPackageManager().getInstallSourceInfo(activity.getPackageName());
            // Android lets an app update itself without a prompt only once it installed the current version.
            return activity.getPackageName().equals(info.getInstallingPackageName());
        } catch (Exception e) {
            return false;
        }
    }

    private void askToInstall() {
        activity.runOnUiThread(() -> {
            if (activity.isFinishing() || readySession == -1) return;
            new AlertDialog.Builder(activity, android.R.style.Theme_Material_Dialog_Alert)
                    .setTitle("Update ready")
                    .setMessage("TradeBot " + readyName + " is downloaded. The app closes while it installs; "
                            + "open it again afterwards. Your bot keeps trading on the server meanwhile.")
                    .setPositiveButton("Install", (d, w) -> commit())
                    .setNegativeButton("Later", null)
                    .show();
        });
    }

    private void askForInstallPermission() {
        if (activity.isFinishing()) return;
        new AlertDialog.Builder(activity, android.R.style.Theme_Material_Dialog_Alert)
                .setTitle("Allow automatic updates")
                .setMessage("A new version of TradeBot is available. To update itself, TradeBot needs permission to "
                        + "install apps. Turn on \"Allow from this source\" on the next screen, then come back.")
                .setPositiveButton("Open settings", (d, w) -> {
                    activity.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putLong("last", 0).apply();
                    activity.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                            Uri.parse("package:" + activity.getPackageName())));
                })
                .setNegativeButton("Not now", null)
                .show();
    }

    /** Streams the APK into an Android install session, checking its SHA-256 on the way. */
    private int download(String url, String expectedSha) throws Exception {
        PackageInstaller pi = activity.getPackageManager().getPackageInstaller();
        for (PackageInstaller.SessionInfo old : pi.getMySessions()) {
            try { pi.abandonSession(old.getSessionId()); } catch (Exception ignored) {}
        }
        PackageInstaller.SessionParams params =
                new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(activity.getPackageName());
        if (Build.VERSION.SDK_INT >= 31) {
            params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
        }
        int id = pi.createSession(params);
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(10000);
        c.setReadTimeout(30000);
        try (PackageInstaller.Session s = pi.openSession(id)) {
            if (c.getResponseCode() != 200) throw new IllegalStateException("server answered " + c.getResponseCode());
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            try (InputStream in = c.getInputStream();
                 OutputStream out = s.openWrite("base.apk", 0, c.getContentLengthLong())) {
                byte[] buf = new byte[65536];
                int n;
                while ((n = in.read(buf)) > 0) { md.update(buf, 0, n); out.write(buf, 0, n); }
                s.fsync(out);
            }
            String got = hex(md.digest());
            if (!got.equalsIgnoreCase(expectedSha)) {
                s.abandon();
                throw new SecurityException("the download didn't match its checksum, so it was discarded");
            }
        } catch (Exception e) {
            try { pi.abandonSession(id); } catch (Exception ignored) {}
            throw e;
        } finally {
            c.disconnect();
        }
        return id;
    }

    private void commit() {
        int id = readySession;
        if (id == -1) return;
        readySession = -1;
        try {
            PackageInstaller.Session s = activity.getPackageManager().getPackageInstaller().openSession(id);
            Intent i = new Intent(activity, UpdateReceiver.class);
            PendingIntent pending = PendingIntent.getBroadcast(activity, id, i,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);
            s.commit(pending.getIntentSender());
            s.close();
        } catch (Exception e) {
            toast("Couldn't install the update: " + e.getMessage());
        }
    }

    private static String get(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(8000);
        try {
            if (c.getResponseCode() == 404) throw new IllegalStateException("no update is published on your server yet");
            if (c.getResponseCode() != 200) throw new IllegalStateException("server answered " + c.getResponseCode());
            try (InputStream in = c.getInputStream()) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[4096];
                int n;
                while ((n = in.read(buf)) > 0 && out.size() < 65536) out.write(buf, 0, n);
                return new String(out.toByteArray(), StandardCharsets.UTF_8);
            }
        } finally {
            c.disconnect();
        }
    }

    private static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format("%02x", x));
        return sb.toString();
    }

    private void toast(String s) {
        activity.runOnUiThread(() -> Toast.makeText(activity, s, Toast.LENGTH_LONG).show());
    }
}
