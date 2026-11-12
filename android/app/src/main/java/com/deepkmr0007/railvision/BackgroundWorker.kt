package com.deepkmr0007.railvision

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/**
 * Every 15 minutes (Android's shortest interval), even when the app is closed: update tracked trains,
 * announcements and reminders, let the assistant read what is due, and show new alerts.
 */
class BackgroundWorker(ctx: Context, params: WorkerParameters) : Worker(ctx, params) {
    override fun doWork(): Result {
        val ctx = applicationContext
        if (!Engine.ensureStarted(ctx)) return Result.retry()
        Engine.runOnce()
        Notifier.pollOnce(ctx)
        if (!AssistantAgent.loopRunning) {
            try { AssistantAgent.round(ctx, budgetMs = 7 * 60_000L) } catch (t: Throwable) { /* next time */ }
            Engine.runOnce()
        }
        Notifier.pollOnce(ctx)
        return Result.success()
    }

    companion object {
        fun schedule(ctx: Context) {
            val req = PeriodicWorkRequestBuilder<BackgroundWorker>(15, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork("railvision-check", ExistingPeriodicWorkPolicy.KEEP, req)
        }
    }
}
