package local.ban;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.util.Base64;

/** The few things BAN remembers: where Arlo Keeper is, and whether to notify. */
final class Prefs {
    static final String DEFAULT_SERVER = "http://10.0.0.156:8090";

    private Prefs() {}

    private static SharedPreferences p(Context c) {
        return c.getSharedPreferences("ban", Context.MODE_PRIVATE);
    }

    static String server(Context c) {
        return p(c).getString("server", null);
    }

    static void setServer(Context c, String url) {
        p(c).edit().putString("server", clean(url)).apply();
    }

    static boolean notify(Context c) {
        return p(c).getBoolean("notify", true);
    }

    static void setNotify(Context c, boolean on) {
        p(c).edit().putBoolean("notify", on).apply();
    }

    static long lastEvent(Context c) {
        return p(c).getLong("last_event", 0);
    }

    static void setLastEvent(Context c, long t) {
        p(c).edit().putLong("last_event", t).apply();
    }

    /** "10.0.0.156:8090" -> "http://10.0.0.156:8090"; no trailing slash. */
    static String clean(String url) {
        url = url == null ? "" : url.trim();
        if (url.isEmpty()) return DEFAULT_SERVER;
        if (!url.matches("(?i)^https?://.*")) url = "http://" + url;
        while (url.endsWith("/")) url = url.substring(0, url.length() - 1);
        return url;
    }

    /** The server address without any user:password@ part (for showing and for WebView). */
    static String bare(String url) {
        Uri u = Uri.parse(url);
        if (u.getUserInfo() == null) return url;
        return url.replaceFirst("://[^@/]*@", "://");
    }

    /** "Basic ..." header if the address has user:password@ in it, else null. */
    static String authHeader(String url) {
        String info = Uri.parse(url).getUserInfo();
        if (info == null || info.isEmpty()) return null;
        return "Basic " + Base64.encodeToString(Uri.decode(info).getBytes(), Base64.NO_WRAP);
    }

    static String[] userPass(String url) {
        String info = Uri.parse(url).getUserInfo();
        if (info == null) return null;
        info = Uri.decode(info);
        int i = info.indexOf(':');
        return i < 0 ? new String[] {info, ""} : new String[] {info.substring(0, i), info.substring(i + 1)};
    }
}
