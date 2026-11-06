package local.ban;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.util.Base64;

/** The few things BAN remembers: where Arlo Keeper is, and whether to notify. */
final class Prefs {
    static final String REMOTE_SERVER = "https://ban.tapper.co";
    static final String DEFAULT_SERVER = REMOTE_SERVER;
    /** Arlo Keeper on the home network, for when Cloudflare can't be reached. */
    static final String HOME_SERVER = "http://10.0.0.156:8090";

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

    /** The last address BAN answered on at home. */
    static String localServer(Context c) {
        return p(c).getString("local_server", null);
    }

    static void setLocalServer(Context c, String url) {
        p(c).edit().putString("local_server", url).apply();
    }

    /** An address typed in by hand (tried after home and ban.tapper.co). */
    static String manualServer(Context c) {
        return p(c).getString("manual_server", null);
    }

    static void setManualServer(Context c, String url) {
        p(c).edit().putString("manual_server", url == null ? null : clean(url)).apply();
    }

    static long lastUpdateCheck(Context c) {
        return p(c).getLong("update_check", 0);
    }

    static void setLastUpdateCheck(Context c, long t) {
        p(c).edit().putLong("update_check", t).apply();
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

    /** The sign-in cookie the page set (away from home), for the app's own requests. */
    static String cookie(String url) {
        try {
            android.webkit.CookieManager cm = android.webkit.CookieManager.getInstance();
            return cm.getCookie(bare(url));
        } catch (Exception e) {
            return null;
        }
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
