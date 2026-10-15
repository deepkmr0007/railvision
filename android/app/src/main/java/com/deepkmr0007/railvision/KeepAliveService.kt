package com.deepkmr0007.railvision

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.content.ContextCompat

/**
 * "Keep running" in Settings: checks continue every few minutes with the app closed, instead of only every
 * 15 minutes. Android requires the small permanent notification while this is on.
 */
class KeepAliveService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Notifier.channels(this)
        val n = Notifier.ongoing(this, "Checks one page at a time. Turn off in Settings → Phone app.")
        if (Build.VERSION.SDK_INT >= 34) startForeground(Notifier.ID_ONGOING, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else startForeground(Notifier.ID_ONGOING, n)
        val app = applicationContext
        Thread({
            if (Engine.ensureStarted(app)) {
                AssistantAgent.startLoop(app)
                Notifier.startPolling(app)
            }
        }, "rv-keepalive-start").start()
        return START_STICKY
    }

    override fun onDestroy() {
        if (!MainActivity.visible) {
            AssistantAgent.stopLoop()
            Notifier.stopPolling()
        }
        super.onDestroy()
    }

    companion object {
        fun start(ctx: Context): Boolean = try {
            ContextCompat.startForegroundService(ctx, Intent(ctx, KeepAliveService::class.java)); true
        } catch (e: Exception) {
            false     // Android refused to start it from the background; the 15-minute job still runs
        }

        fun stop(ctx: Context) { ctx.stopService(Intent(ctx, KeepAliveService::class.java)) }
    }
}
