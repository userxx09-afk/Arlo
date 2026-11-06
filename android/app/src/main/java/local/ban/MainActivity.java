package local.ban;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.HttpAuthHandler;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;

/**
 * BAN on a phone: the Arlo Keeper web page (which lays itself out for phones), plus motion
 * notifications from MotionService. The page talks to the app through window.BANApp.
 */
public class MainActivity extends Activity {
    private static final int REQ_FILE = 2, REQ_NOTIFY = 3;
    private WebView web;
    private ValueCallback<Uri[]> fileCallback;
    private boolean offline;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        MotionService.channels(this);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#04070D"));
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#04070D"));
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);
        // Android 15 draws apps under the status and navigation bars: keep the page clear of them.
        root.setOnApplyWindowInsetsListener((v, in) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets s = in.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                v.setPadding(s.left, s.top, s.right, s.bottom);
            } else {
                v.setPadding(in.getSystemWindowInsetLeft(), in.getSystemWindowInsetTop(),
                        in.getSystemWindowInsetRight(), in.getSystemWindowInsetBottom());
            }
            return in;
        });

        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setUserAgentString(ws.getUserAgentString() + " BAN-Android/" + version());
        CookieManager.getInstance().setAcceptCookie(true);
        web.addJavascriptInterface(new Bridge(), "BANApp");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
        web.setDownloadListener((url, ua, disposition, mime, length) -> download(url, disposition, mime));

        if (Build.VERSION.SDK_INT >= 33) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(0, this::goBack);
        }

        if (Prefs.server(this) == null) {
            askServer(true);
        } else {
            load(getIntent());
            startNotifications();
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        load(intent);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        goBack();
    }

    private void goBack() {
        if (web.canGoBack() && !offline) web.goBack(); else moveTaskToBack(true);
    }

    private String server() {
        String s = Prefs.server(this);
        return s == null ? Prefs.DEFAULT_SERVER : s;
    }

    /** Open the page, or a clip / live view from a notification ("path" extra). */
    private void load(Intent intent) {
        String path = intent == null ? null : intent.getStringExtra("path");
        if (intent != null) intent.removeExtra("path");
        offline = false;
        web.loadUrl(Prefs.bare(server()) + (path == null ? "/" : path));
    }

    private void startNotifications() {
        if (!Prefs.notify(this)) return;
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFY);
        }
        try { MotionService.start(this); } catch (Exception e) { MotionService.state = "Couldn't start: " + e.getMessage(); }
    }

    private void askServer(boolean first) {
        EditText box = new EditText(this);
        box.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        box.setText(Prefs.server(this) == null ? Prefs.DEFAULT_SERVER : Prefs.server(this));
        box.setSelectAllOnFocus(true);
        FrameLayout pad = new FrameLayout(this);
        int dp = (int) getResources().getDisplayMetrics().density;
        pad.setPadding(22 * dp, 8 * dp, 22 * dp, 0);
        pad.addView(box);
        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle(first ? "Welcome to BAN" : "Server address")
                .setMessage("The address of Arlo Keeper on your home server. On the home Wi-Fi it's usually " + Prefs.DEFAULT_SERVER + ".")
                .setView(pad)
                .setCancelable(!first)
                .setPositiveButton("Connect", (d, w) -> {
                    Prefs.setServer(this, box.getText().toString());
                    Prefs.setLastEvent(this, 0);
                    load(null);
                    if (Prefs.notify(this)) { MotionService.stop(this); startNotifications(); }
                });
        if (!first) b.setNegativeButton("Cancel", null);
        b.show();
    }

    private void download(String url, String disposition, String mime) {
        try {
            DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
            String name = URLUtil.guessFileName(url, disposition, mime);
            r.setMimeType(mime);
            r.addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url));
            String auth = Prefs.authHeader(server());
            if (auth != null) r.addRequestHeader("Authorization", auth);
            r.setTitle(name);
            r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            r.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            getSystemService(DownloadManager.class).enqueue(r);
            Toast.makeText(this, "Saving " + name + " to Downloads", Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Toast.makeText(this, "Couldn't download: " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onActivityResult(int req, int result, Intent data) {
        if (req == REQ_FILE && fileCallback != null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data));
            fileCallback = null;
            return;
        }
        super.onActivityResult(req, result, data);
    }

    private String version() {
        try { return getPackageManager().getPackageInfo(getPackageName(), 0).versionName; } catch (Exception e) { return "?"; }
    }

    private void showOffline(String why) {
        offline = true;
        String html = "<html><head><meta name=viewport content='width=device-width,initial-scale=1'><style>"
                + "body{background:#04070d;color:#eef3fb;font-family:sans-serif;padding:32px 22px;text-align:center}"
                + "h2{margin:18px 0 6px}p{color:#93a6c4;line-height:1.5}"
                + "a{display:block;margin:14px auto;max-width:280px;padding:13px;border-radius:999px;font-weight:700;text-decoration:none;"
                + "background:#ff7a1a;color:#1a0d00}a.s{background:#0e1a2e;color:#eef3fb;border:1px solid #22385c}</style></head><body>"
                + "<div style='font-size:54px'>🔔</div><h2>Can't reach BAN</h2>"
                + "<p>" + escape(Prefs.bare(server())) + " isn't answering.<br>Is the phone on the home Wi-Fi, or is TapperVPN on?</p>"
                + "<p style='font-size:12px'>" + escape(why) + "</p>"
                + "<a href='#' onclick='BANApp.retry();return false'>Try again</a>"
                + "<a class=s href='#' onclick='BANApp.changeServer();return false'>Server address…</a></body></html>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    private static String escape(String s) {
        return s == null ? "" : s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
            Uri u = req.getUrl();
            Uri home = Uri.parse(Prefs.bare(server()));
            if (u.getHost() != null && u.getHost().equals(home.getHost())) return false;
            try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) {}
            return true;
        }

        @Override
        public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
            if (req.isForMainFrame()) showOffline(String.valueOf(err.getDescription()));
        }

        @Override
        public void onReceivedHttpAuthRequest(WebView v, HttpAuthHandler h, String host, String realm) {
            String[] up = Prefs.userPass(server());
            if (up != null) h.proceed(up[0], up[1]); else h.cancel();
        }
    }

    private class Chrome extends WebChromeClient {
        @Override
        public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams params) {
            if (fileCallback != null) fileCallback.onReceiveValue(null);
            fileCallback = cb;
            try {
                startActivityForResult(params.createIntent(), REQ_FILE);
            } catch (Exception e) {
                fileCallback = null;
                return false;
            }
            return true;
        }
    }

    /** window.BANApp, used by the Alerts page's "This phone" panel. */
    private class Bridge {
        @JavascriptInterface public boolean notificationsEnabled() { return Prefs.notify(MainActivity.this); }

        @JavascriptInterface public void setNotifications(boolean on) {
            Prefs.setNotify(MainActivity.this, on);
            runOnUiThread(() -> { if (on) startNotifications(); else MotionService.stop(MainActivity.this); });
        }

        @JavascriptInterface public String connectionState() {
            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                return "Android is blocking BAN's notifications. Allow them in Settings > Apps > BAN > Notifications.";
            return MotionService.state;
        }

        @JavascriptInterface public String server() { return Prefs.bare(MainActivity.this.server()); }

        @JavascriptInterface public String version() { return MainActivity.this.version(); }

        @JavascriptInterface public void changeServer() { runOnUiThread(() -> askServer(false)); }

        @JavascriptInterface public void retry() { runOnUiThread(() -> load(null)); }

        @JavascriptInterface public boolean batteryOk() {
            PowerManager pm = getSystemService(PowerManager.class);
            return pm.isIgnoringBatteryOptimizations(getPackageName());
        }

        @SuppressLint("BatteryLife")
        @JavascriptInterface public void fixBattery() {
            runOnUiThread(() -> {
                try {
                    startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName())));
                } catch (Exception e) {
                    startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
                }
            });
        }
    }
}
