package local.ban;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.util.Log;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.URL;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.concurrent.ExecutorCompletionService;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * Finds Arlo Keeper: first on the home network (the last address that worked, then the usual
 * one, then a quick look around the Wi-Fi), then away from home at ban.tapper.co, then any
 * address typed in by hand. Returns null if none answers; the app then asks for an address.
 */
final class Discovery {
    static final int PORT = 8090;
    private static long lastRun;
    private static String lastResult;

    private Discovery() {}

    /** Does this address answer as BAN? (The manifest needs no sign-in, even away from home.) */
    static boolean isBan(String base, int timeoutMs) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(Prefs.bare(base) + "/manifest.webmanifest").openConnection();
            c.setConnectTimeout(timeoutMs);
            c.setReadTimeout(timeoutMs);
            c.setInstanceFollowRedirects(false);
            if (c.getResponseCode() != 200) return false;
            byte[] buf = new byte[2048];
            int n;
            StringBuilder sb = new StringBuilder();
            try (InputStream in = c.getInputStream()) {
                while ((n = in.read(buf)) > 0 && sb.length() < 4096) sb.append(new String(buf, 0, n));
            }
            return sb.indexOf("Arlo Notifications") >= 0;
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** The best address right now. Slow (a few seconds at most): call it off the main thread. */
    static synchronized String find(Context ctx) {
        LinkedHashSet<String> home = new LinkedHashSet<>();
        if (Prefs.localServer(ctx) != null) home.add(Prefs.localServer(ctx));
        home.add(Prefs.HOME_SERVER);
        for (String s : home) {
            if (isBan(s, 1500)) return remember(ctx, s, true);
        }
        String found = scanWifi(ctx);
        if (found != null) return remember(ctx, found, true);
        if (isBan(Prefs.REMOTE_SERVER, 8000)) return remember(ctx, Prefs.REMOTE_SERVER, false);
        String manual = Prefs.manualServer(ctx);
        if (manual != null && isBan(manual, 6000)) return remember(ctx, manual, false);
        lastRun = System.currentTimeMillis();
        lastResult = null;
        return null;
    }

    /** Same as find(), but reuses an answer from the last two minutes (for the background service). */
    static String findCached(Context ctx) {
        if (System.currentTimeMillis() - lastRun < 120_000 && lastResult != null) return lastResult;
        return find(ctx);
    }

    static void forget() {
        lastRun = 0;
        lastResult = null;
    }

    private static String remember(Context ctx, String s, boolean local) {
        if (local) Prefs.setLocalServer(ctx, s);
        Prefs.setServer(ctx, s);
        lastRun = System.currentTimeMillis();
        lastResult = s;
        Log.i(MotionService.TAG, "BAN found at " + s);
        return s;
    }

    /** Ask every address on the phone's Wi-Fi (/24) for BAN on port 8090, many at once. */
    private static String scanWifi(Context ctx) {
        try {
            ConnectivityManager cm = ctx.getSystemService(ConnectivityManager.class);
            Network net = cm.getActiveNetwork();
            NetworkCapabilities caps = net == null ? null : cm.getNetworkCapabilities(net);
            if (caps == null || !(caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                    || caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET))) return null;
            LinkProperties lp = cm.getLinkProperties(net);
            if (lp == null) return null;
            byte[] mine = null;
            for (LinkAddress la : lp.getLinkAddresses()) {
                InetAddress a = la.getAddress();
                if (a instanceof Inet4Address && a.isSiteLocalAddress()) { mine = a.getAddress(); break; }
            }
            if (mine == null) return null;
            String prefix = (mine[0] & 255) + "." + (mine[1] & 255) + "." + (mine[2] & 255) + ".";
            int me = mine[3] & 255;
            List<String> hosts = new ArrayList<>();
            for (int i = 1; i < 255; i++) if (i != me) hosts.add("http://" + prefix + i + ":" + PORT);

            ExecutorService pool = Executors.newFixedThreadPool(48);
            ExecutorCompletionService<String> done = new ExecutorCompletionService<>(pool);
            for (String h : hosts) done.submit(() -> isBan(h, 700) ? h : null);
            try {
                long until = System.currentTimeMillis() + 8000;
                for (int i = 0; i < hosts.size(); i++) {
                    long left = until - System.currentTimeMillis();
                    if (left <= 0) break;
                    java.util.concurrent.Future<String> f = done.poll(left, TimeUnit.MILLISECONDS);
                    if (f == null) break;
                    String r = f.get();
                    if (r != null) return r;
                }
            } finally {
                pool.shutdownNow();
            }
        } catch (Exception e) {
            Log.i(MotionService.TAG, "wifi scan: " + e);
        }
        return null;
    }
}
