package local.ban;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.util.Log;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

/**
 * Updates from GitHub. downloads/latest.json in the repo says which version is newest and where
 * its APK is; if it's newer than this one, BAN downloads it, checks its SHA-256, and hands it to
 * Android's installer, which asks you to confirm. Updates install over the old app because every
 * version is signed with the same key.
 */
final class Updater {
    static final String LATEST = "https://raw.githubusercontent.com/userxx09-afk/Arlo/main/downloads/latest.json";
    static volatile String state = "";

    private Updater() {}

    static long myCode(Context c) {
        try {
            if (Build.VERSION.SDK_INT >= 28) return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).getLongVersionCode();
            return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    /** Check in the background; if there's a newer version, offer it. quiet = no "up to date" toast. */
    static void check(Activity a, boolean quiet) {
        state = "Checking for updates…";
        new Thread(() -> {
            try {
                JSONObject j = new JSONObject(new String(get(LATEST, 12000, 64 * 1024)));
                Prefs.setLastUpdateCheck(a, System.currentTimeMillis());
                long code = j.optLong("versionCode");
                String name = j.optString("versionName", "?");
                if (code <= myCode(a)) {
                    state = "Up to date (checked " + android.text.format.DateFormat.getTimeFormat(a).format(new java.util.Date()) + ")";
                    if (!quiet) a.runOnUiThread(() -> Toast.makeText(a, "BAN is up to date.", Toast.LENGTH_SHORT).show());
                    return;
                }
                state = "BAN " + name + " is available.";
                a.runOnUiThread(() -> {
                    if (a.isFinishing()) return;
                    new AlertDialog.Builder(a)
                            .setTitle("BAN " + name + " is available")
                            .setMessage((j.optString("notes", "").isEmpty() ? "" : j.optString("notes") + "\n\n") + "Download and install it now?")
                            .setPositiveButton("Update", (d, w) -> install(a, j))
                            .setNegativeButton("Later", null)
                            .show();
                });
            } catch (Exception e) {
                state = "Couldn't check for updates (" + e.getMessage() + ")";
                if (!quiet) a.runOnUiThread(() -> Toast.makeText(a, state, Toast.LENGTH_LONG).show());
            }
        }, "ban-update-check").start();
    }

    /** Check on its own at most twice a day, when the app opens. */
    static void maybeCheck(Activity a) {
        if (System.currentTimeMillis() - Prefs.lastUpdateCheck(a) > 12 * 3600_000L) check(a, true);
    }

    private static void install(Activity a, JSONObject j) {
        if (Build.VERSION.SDK_INT >= 26 && !a.getPackageManager().canRequestPackageInstalls()) {
            new AlertDialog.Builder(a)
                    .setTitle("Allow BAN to update itself")
                    .setMessage("Android needs your OK for BAN to install its updates. Turn on \"Allow from this source\" on the next screen, come back, and tap Update again.")
                    .setPositiveButton("Open settings", (d, w) -> a.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                            Uri.parse("package:" + a.getPackageName()))))
                    .setNegativeButton("Cancel", null)
                    .show();
            return;
        }
        Toast.makeText(a, "Downloading the update…", Toast.LENGTH_SHORT).show();
        state = "Downloading the update…";
        new Thread(() -> {
            try {
                byte[] apk = get(j.getString("apk"), 60000, 50 * 1024 * 1024);
                String want = j.optString("sha256", "").toLowerCase();
                if (!want.isEmpty() && !want.equals(sha256(apk))) throw new Exception("the download was damaged; try again");
                PackageInstaller pi = a.getPackageManager().getPackageInstaller();
                PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
                params.setAppPackageName(a.getPackageName());
                int id = pi.createSession(params);
                try (PackageInstaller.Session s = pi.openSession(id)) {
                    try (OutputStream out = s.openWrite("ban.apk", 0, apk.length)) {
                        out.write(apk);
                        s.fsync(out);
                    }
                    Intent done = new Intent(a, Result.class);
                    int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
                    s.commit(PendingIntent.getBroadcast(a, id, done, flags).getIntentSender());
                }
                state = "Waiting for you to confirm the update…";
            } catch (Exception e) {
                state = "Update failed: " + e.getMessage();
                a.runOnUiThread(() -> Toast.makeText(a, state, Toast.LENGTH_LONG).show());
            }
        }, "ban-update").start();
    }

    /** Android's installer reports back here: show its confirmation screen, or the result. */
    public static class Result extends BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent intent) {
            int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
            if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                Intent confirm = intent.getParcelableExtra(Intent.EXTRA_INTENT);
                if (confirm != null) {
                    confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    c.startActivity(confirm);
                }
            } else if (status == PackageInstaller.STATUS_SUCCESS) {
                state = "Updated.";
            } else {
                String msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
                state = "Update didn't install" + (msg == null ? "" : ": " + msg);
                Log.w(MotionService.TAG, state);
                Toast.makeText(c, state, Toast.LENGTH_LONG).show();
            }
        }
    }

    private static byte[] get(String url, int timeout, int max) throws Exception {
        for (int hops = 0; hops < 5; hops++) {
            HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(timeout);
            c.setReadTimeout(timeout);
            c.setInstanceFollowRedirects(false);
            c.setRequestProperty("Cache-Control", "no-cache");
            int code = c.getResponseCode();
            if (code >= 300 && code < 400 && c.getHeaderField("Location") != null) {
                url = new URL(new URL(url), c.getHeaderField("Location")).toString();
                c.disconnect();
                continue;
            }
            if (code != 200) throw new Exception("GitHub answered " + code);
            try (InputStream in = c.getInputStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
                byte[] buf = new byte[16384];
                int n;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                    if (out.size() > max) throw new Exception("file too big");
                }
                return out.toByteArray();
            } finally {
                c.disconnect();
            }
        }
        throw new Exception("too many redirects");
    }

    private static String sha256(byte[] b) throws Exception {
        StringBuilder sb = new StringBuilder();
        for (byte x : MessageDigest.getInstance("SHA-256").digest(b)) sb.append(String.format("%02x", x));
        return sb.toString();
    }
}
