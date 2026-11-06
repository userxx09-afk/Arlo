package local.ban;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** After a restart (or an app update), start listening for motion again if it was on. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent intent) {
        if (Prefs.server(c) != null && Prefs.notify(c)) {
            try { MotionService.start(c); } catch (Exception ignored) {}
        }
    }
}
