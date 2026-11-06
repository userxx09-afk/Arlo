package local.ban;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.DateFormat;
import java.util.Date;

/**
 * Keeps one connection open to Arlo Keeper's /api/events. When a camera sees motion it shows a
 * notification at once ("Motion: Front Door", tap to watch live), and when the clip is saved
 * it updates that notification with the picture (tap to play the clip).
 *
 * It runs as a foreground service (Android shows a quiet "Watching for motion" notification),
 * because that's the only way an app without Google's push service can hear about motion the
 * moment it happens. It needs the phone to reach the server: home Wi-Fi, or TapperVPN.
 */
public class MotionService extends Service {
    static final String TAG = "BAN";
    static final String CH_MOTION = "motion";
    static final String CH_WATCH = "watching";
    static final int ID_WATCH = 1;

    static volatile String state = "Starting…";

    private volatile boolean running;
    private volatile boolean signInNeeded;
    private Thread worker;
    private volatile HttpURLConnection conn;
    private ConnectivityManager.NetworkCallback netCallback;

    static void start(Context c) {
        Intent i = new Intent(c, MotionService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
    }

    /** Reconnect now (e.g. right after signing in). */
    static void kick(Context c) {
        if (Prefs.notify(c)) start(c);
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, MotionService.class));
        state = "Notifications are off on this phone.";
    }

    static void channels(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        NotificationChannel motion = new NotificationChannel(CH_MOTION, "Motion alerts", NotificationManager.IMPORTANCE_HIGH);
        motion.setDescription("When a camera sees motion");
        motion.enableVibration(true);
        motion.setLightColor(0xFFFF7A1A);
        motion.enableLights(true);
        nm.createNotificationChannel(motion);
        NotificationChannel watch = new NotificationChannel(CH_WATCH, "Watching for motion", NotificationManager.IMPORTANCE_MIN);
        watch.setDescription("The quiet notification Android needs while BAN listens for motion. You can hide it here.");
        watch.setShowBadge(false);
        nm.createNotificationChannel(watch);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        channels(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = watchingNotification("Connecting to " + Prefs.bare(server()) + "…");
        if (Build.VERSION.SDK_INT >= 34) startForeground(ID_WATCH, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(ID_WATCH, n);
        if (!Prefs.notify(this)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (worker != null && worker.isAlive()) {
            dropConnection();
            worker.interrupt();   // skip the wait before the next try
        }
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(this::loop, "ban-events");
            worker.start();
            watchNetwork();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        dropConnection();
        if (worker != null) worker.interrupt();
        if (netCallback != null) {
            try { getSystemService(ConnectivityManager.class).unregisterNetworkCallback(netCallback); } catch (Exception ignored) {}
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private String server() {
        String s = Prefs.server(this);
        return s == null ? Prefs.DEFAULT_SERVER : s;
    }

    // When the phone changes network (Wi-Fi <-> mobile, VPN up), reconnect at once.
    private void watchNetwork() {
        ConnectivityManager cm = getSystemService(ConnectivityManager.class);
        netCallback = new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network network) { dropConnection(); }
        };
        try { cm.registerDefaultNetworkCallback(netCallback); } catch (Exception ignored) {}
    }

    private void dropConnection() {
        HttpURLConnection c = conn;
        if (c != null) new Thread(c::disconnect).start();
    }

    private void setState(String s) {
        if (s.equals(state)) return;
        state = s;
        getSystemService(NotificationManager.class).notify(ID_WATCH, watchingNotification(s));
    }

    private void loop() {
        long backoff = 2000;
        while (running) {
            String base = server();
            try {
                long since = Prefs.lastEvent(this);
                URL url = new URL(Prefs.bare(base) + "/api/events" + (since > 0 ? "?since=" + since : ""));
                HttpURLConnection c = (HttpURLConnection) url.openConnection();
                conn = c;
                c.setConnectTimeout(10000);
                c.setReadTimeout(65000);       // the server pings every 20 s
                c.setRequestProperty("Accept", "text/event-stream");
                String auth = Prefs.authHeader(base);
                if (auth != null) c.setRequestProperty("Authorization", auth);
                String cookie = Prefs.cookie(base);
                if (cookie != null) c.setRequestProperty("Cookie", cookie);
                int code = c.getResponseCode();
                if (code == 401) {
                    setState("Open BAN and sign in, so it can watch for motion away from home.");
                    signInNeeded = true;
                    throw new Exception("needs sign-in");
                }
                signInNeeded = false;
                if (code != 200) throw new Exception("server answered " + code);
                setState("Watching for motion · " + Prefs.bare(base));
                backoff = 2000;
                read(c.getInputStream());
                throw new Exception("connection closed");
            } catch (Exception e) {
                if (!running) break;
                Log.i(TAG, "events: " + e);
                if (!signInNeeded) setState("Can't reach " + Prefs.bare(base) + " right now. Retrying…");
                try { Thread.sleep(backoff); } catch (InterruptedException ie) { if (!running) break; }
                backoff = Math.min(backoff * 2, 60000);
            } finally {
                HttpURLConnection c = conn;
                conn = null;
                if (c != null) c.disconnect();
            }
        }
    }

    private void read(InputStream in) throws Exception {
        BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
        String line, event = null;
        StringBuilder data = new StringBuilder();
        while (running && (line = r.readLine()) != null) {
            if (line.isEmpty()) {
                if (event != null && data.length() > 0) handle(event, data.toString());
                event = null;
                data.setLength(0);
            } else if (line.startsWith("event:")) {
                event = line.substring(6).trim();
            } else if (line.startsWith("data:")) {
                data.append(line.substring(5).trim());
            }
        }
    }

    private void handle(String event, String json) {
        try {
            JSONObject ev = new JSONObject(json);
            long time = ev.optLong("time");
            if (time <= Prefs.lastEvent(this)) return;      // already shown before a reconnect
            Prefs.setLastEvent(this, time);
            String reason = ev.optString("reason");
            if ("clip".equals(event) && "manual".equals(reason)) return;  // "Record clip" button
            show(event, ev);
        } catch (Exception e) {
            Log.w(TAG, "bad event: " + e);
        }
    }

    private void show(String event, JSONObject ev) {
        String camera = ev.optString("camera", "Camera");
        String camId = ev.optString("camera_id", camera);
        boolean test = "test".equals(ev.optString("reason"));
        long started = ev.optLong("started", ev.optLong("time"));
        String when = DateFormat.getTimeInstance(DateFormat.SHORT).format(new Date(started));
        int id = 1000 + Math.abs(camId.hashCode() % 100000);

        Notification.Builder b = new Notification.Builder(this, CH_MOTION)
                .setSmallIcon(R.drawable.ic_stat_ban)
                .setColor(0xFFFF7A1A)
                .setContentTitle((test ? "Test: " : "Motion: ") + camera)
                .setWhen(started).setShowWhen(true)
                .setCategory(Notification.CATEGORY_EVENT)
                .setAutoCancel(true);

        if ("motion".equals(event)) {
            b.setContentText(when + " · recording a clip. Tap to watch live.")
             .setContentIntent(open("/?live=" + enc(camId), id));
        } else {
            String key = ev.optString("key", "");
            b.setContentText(when + " · clip saved. Tap to watch it.")
             .setContentIntent(open(key.isEmpty() ? "/#recordings" : "/?clip=" + enc(key), id))
             .setOnlyAlertOnce(false);
            Bitmap pic = fetchPicture(ev.optString("thumb", ""));
            if (pic != null) {
                b.setLargeIcon(pic).setStyle(new Notification.BigPictureStyle().bigPicture(pic).bigLargeIcon((Bitmap) null)
                        .setSummaryText(when + " · clip saved. Tap to watch it."));
            }
        }
        getSystemService(NotificationManager.class).notify(id, b.build());
    }

    private static String enc(String s) {
        return android.net.Uri.encode(s);
    }

    private PendingIntent open(String path, int id) {
        Intent i = new Intent(this, MainActivity.class).putExtra("path", path)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(this, id, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private Bitmap fetchPicture(String path) {
        if (path == null || path.isEmpty()) return null;
        HttpURLConnection c = null;
        try {
            String base = server();
            c = (HttpURLConnection) new URL(Prefs.bare(base) + path).openConnection();
            c.setConnectTimeout(8000);
            c.setReadTimeout(10000);
            String auth = Prefs.authHeader(base);
            if (auth != null) c.setRequestProperty("Authorization", auth);
            String cookie = Prefs.cookie(base);
            if (cookie != null) c.setRequestProperty("Cookie", cookie);
            if (c.getResponseCode() != 200) return null;
            try (InputStream in = c.getInputStream()) { return BitmapFactory.decodeStream(in); }
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private Notification watchingNotification(String text) {
        Intent i = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        PendingIntent pi = PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CH_WATCH)
                .setSmallIcon(R.drawable.ic_stat_ban)
                .setColor(0xFFFF7A1A)
                .setContentTitle("BAN is watching for motion")
                .setContentText(text)
                .setOngoing(true)
                .setShowWhen(false)
                .setContentIntent(pi)
                .build();
    }
}
