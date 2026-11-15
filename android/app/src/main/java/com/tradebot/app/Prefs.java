package com.tradebot.app;

import android.content.Context;
import android.content.SharedPreferences;

import java.net.URI;
import java.util.Locale;

/** The app's two settings: which server to open, and whether the fingerprint/PIN lock is on. */
final class Prefs {
    private static final String FILE = "tradebot";
    private static final String KEY_SERVER = "server";
    private static final String KEY_LOCK = "lock";

    private Prefs() {}

    private static SharedPreferences sp(Context c) {
        return c.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    static String server(Context c) {
        return sp(c).getString(KEY_SERVER, null);
    }

    static void setServer(Context c, String url) {
        sp(c).edit().putString(KEY_SERVER, url).apply();
    }

    static boolean lockEnabled(Context c) {
        return sp(c).getBoolean(KEY_LOCK, true);
    }

    static void setLockEnabled(Context c, boolean on) {
        sp(c).edit().putBoolean(KEY_LOCK, on).apply();
    }

    /** "bot.example.com" -> "https://bot.example.com"; strips paths and trailing slashes. Null if unusable. */
    static String normalize(String raw) {
        if (raw == null) return null;
        String s = raw.trim();
        if (s.isEmpty()) return null;
        if (!s.contains("://")) s = "https://" + s;
        try {
            URI u = new URI(s);
            String scheme = u.getScheme() == null ? "" : u.getScheme().toLowerCase(Locale.ROOT);
            if (!scheme.equals("https") && !scheme.equals("http")) return null;
            if (u.getHost() == null || u.getHost().isEmpty()) return null;
            return scheme + "://" + u.getHost().toLowerCase(Locale.ROOT) + (u.getPort() == -1 ? "" : ":" + u.getPort());
        } catch (Exception e) {
            return null;
        }
    }

    /** True for plain-http addresses that are not on a home network, where a password would travel unencrypted. */
    static boolean insecureRemote(String url) {
        try {
            URI u = new URI(url);
            if (!"http".equals(u.getScheme())) return false;
            String h = u.getHost();
            return !(h.equals("localhost") || h.startsWith("10.") || h.startsWith("192.168.") || h.endsWith(".local")
                    || h.matches("172\\.(1[6-9]|2[0-9]|3[01])\\..*") || h.startsWith("127."));
        } catch (Exception e) {
            return true;
        }
    }

    /** Same scheme, host and port as the saved server. */
    static boolean sameOrigin(String server, String url) {
        try {
            URI a = new URI(server), b = new URI(url);
            return a.getScheme().equalsIgnoreCase(String.valueOf(b.getScheme()))
                    && a.getHost().equalsIgnoreCase(String.valueOf(b.getHost()))
                    && port(a) == port(b);
        } catch (Exception e) {
            return false;
        }
    }

    private static int port(URI u) {
        if (u.getPort() != -1) return u.getPort();
        return "http".equalsIgnoreCase(u.getScheme()) ? 80 : 443;
    }
}
