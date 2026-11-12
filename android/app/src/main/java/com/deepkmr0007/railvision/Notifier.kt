package com.deepkmr0007.railvision

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject

/** Turns the engine's alerts (delays, platform, chart, berth, PNR, announcements…) into phone notifications. */
object Notifier {
    const val CH_ALERTS = "alerts"
    const val CH_ASK = "needs_you"
    const val CH_STATUS = "status"
    const val ID_ONGOING = 1
    const val ID_ASK = 2
    private val lock = Any()
    @Volatile private var polling: Thread? = null
    @Volatile private var stopPolling = false

    fun channels(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_ALERTS, "Train alerts", NotificationManager.IMPORTANCE_HIGH)
            .apply { description = "Delays, platform, chart, berths, PNR and announcements for your trains" })
        nm.createNotificationChannel(NotificationChannel(CH_ASK, "Assistant needs you", NotificationManager.IMPORTANCE_HIGH)
            .apply { description = "A site is showing a CAPTCHA or login that only you can complete" })
        nm.createNotificationChannel(NotificationChannel(CH_STATUS, "Keep running", NotificationManager.IMPORTANCE_LOW)
            .apply { description = "Shown while RAILVISION keeps checking in the background" })
    }

    fun openApp(ctx: Context, assistant: Boolean = false, req: Int = 0): PendingIntent {
        val i = Intent(ctx, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        if (assistant) i.putExtra(MainActivity.EXTRA_ASSISTANT, true)
        return PendingIntent.getActivity(ctx, req, i, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    fun ongoing(ctx: Context, text: String): Notification =
        NotificationCompat.Builder(ctx, CH_STATUS)
            .setSmallIcon(R.drawable.ic_stat_train)
            .setContentTitle("RAILVISION is watching your trains")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(openApp(ctx))
            .build()

    private fun canPost(ctx: Context): Boolean =
        (Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) &&
            NotificationManagerCompat.from(ctx).areNotificationsEnabled()

    @SuppressLint("MissingPermission")
    private fun post(ctx: Context, id: Int, n: Notification) {
        if (!canPost(ctx)) return
        try { NotificationManagerCompat.from(ctx).notify(id, n) } catch (e: SecurityException) { /* permission revoked */ }
    }

    fun needsYou(ctx: Context, reason: String) {
        post(ctx, ID_ASK, NotificationCompat.Builder(ctx, CH_ASK)
            .setSmallIcon(R.drawable.ic_stat_train)
            .setContentTitle("The assistant needs you")
            .setContentText(reason)
            .setStyle(NotificationCompat.BigTextStyle().bigText(reason + "\nOpen it to finish this step yourself (5 minutes)."))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(openApp(ctx, assistant = true, req = 2))
            .build())
    }

    fun clearNeedsYou(ctx: Context) { NotificationManagerCompat.from(ctx).cancel(ID_ASK) }

    /** Shows new alerts from the engine. The first run only records where to start (no flood of old alerts). */
    fun pollOnce(ctx: Context): Int = synchronized(lock) {
        if (!Engine.started) return 0
        val prefs = Prefs(ctx)
        val after = prefs.lastNotificationId
        val res: JSONObject = try { Engine.api("GET", "/api/notifications?after=${maxOf(after, 0L)}") } catch (e: Exception) { return 0 }
        val items = res.optJSONArray("items") ?: return 0
        var maxId = maxOf(after, 0L)
        val fresh = mutableListOf<JSONObject>()
        for (i in 0 until items.length()) {
            val it = items.getJSONObject(i)
            val id = it.optLong("id")
            if (id > maxId) maxId = id
            fresh += it
        }
        prefs.lastNotificationId = maxId
        if (after < 0) return 0
        var shown = 0
        for (it in fresh.sortedBy { it.optLong("id") }) {
            if (it.optString("type") == "ASSIST_NEEDS_YOU") continue        // the assistant shows its own, with a button
            val title = it.optString("title").ifEmpty { "RAILVISION" }
            val body = it.optString("body")
            post(ctx, 1000 + (it.optLong("id") % 100_000).toInt(), NotificationCompat.Builder(ctx, CH_ALERTS)
                .setSmallIcon(R.drawable.ic_stat_train)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(NotificationCompat.BigTextStyle().bigText(body))
                .setAutoCancel(true)
                .setContentIntent(openApp(ctx, req = 3))
                .build())
            shown++
        }
        shown
    }

    fun startPolling(ctx: Context) {
        val app = ctx.applicationContext
        if (polling?.isAlive == true) return
        stopPolling = false
        polling = Thread({
            while (!stopPolling) {
                try { pollOnce(app) } catch (t: Throwable) { /* try again */ }
                try { Thread.sleep(30_000) } catch (e: InterruptedException) { break }
            }
        }, "rv-notifier").apply { isDaemon = true; start() }
    }

    fun stopPolling() { stopPolling = true; polling?.interrupt() }
}
