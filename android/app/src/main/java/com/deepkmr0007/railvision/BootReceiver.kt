package com.deepkmr0007.railvision

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** After a restart or an app update: schedule the 15-minute checks again and restore "Keep running". */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        BackgroundWorker.schedule(ctx)
        if (Prefs(ctx).keepRunning) KeepAliveService.start(ctx)
    }
}
