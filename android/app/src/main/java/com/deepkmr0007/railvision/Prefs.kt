package com.deepkmr0007.railvision

import android.content.Context
import android.content.SharedPreferences

class Prefs(ctx: Context) {
    val sp: SharedPreferences = ctx.applicationContext.getSharedPreferences("railvision", Context.MODE_PRIVATE)

    /** Foreground service: keeps checking with the app closed (shows a small permanent notification). */
    var keepRunning: Boolean
        get() = sp.getBoolean("keep_running", false)
        set(v) = sp.edit().putBoolean("keep_running", v).apply()

    /** The in-app browser assistant (reads NTES / IRCTC / official pages for you). */
    var assistantOn: Boolean
        get() = sp.getBoolean("assistant_on", true)
        set(v) = sp.edit().putBoolean("assistant_on", v).apply()

    var lastNotificationId: Long
        get() = sp.getLong("last_notification_id", -1L)
        set(v) = sp.edit().putLong("last_notification_id", v).apply()
}
