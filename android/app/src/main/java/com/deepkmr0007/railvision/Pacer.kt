package com.deepkmr0007.railvision

import android.net.Uri
import java.time.LocalDate
import java.time.ZoneId
import kotlin.random.Random

/**
 * Politeness limits, the same as the desktop assistant, and kept across app restarts:
 *  - at least 20 s (+0–10 s random) between page loads; the engine can raise this, never lower it below 15 s
 *  - at most 400 page loads a day
 *  - a CAPTCHA / login / block pauses that site for 30 min, doubling up to 4 h. Never solved or bypassed.
 */
class Pacer(private val prefs: Prefs) {
    var minGapS = 20.0
    val floorS = 15.0
    var jitterS = 10.0
    var maxPerDay = 400
    // Fast lane: a small data request on a page that is already open (no reload) may go sooner, never under 5 s.
    var fastGapS = 6.0
    val fastFloorS = 5.0
    var fastJitterS = 3.0
    var maxFastPerDay = 1500
    private var last = 0L

    private fun host(url: String) = try { Uri.parse(url).host ?: "" } catch (e: Exception) { "" }
    private fun today() = LocalDate.now(ZoneId.of("Asia/Kolkata")).toString()

    fun pagesToday(): Int = if (prefs.sp.getString("pacer_day", "") == today()) prefs.sp.getInt("pacer_count", 0) else 0

    /** Sleeps until the next page load is allowed. Returns false if stopped or the daily limit is reached. */
    fun fastToday(): Int = if (prefs.sp.getString("pacer_day", "") == today()) prefs.sp.getInt("pacer_fast", 0) else 0

    fun waitTurn(fast: Boolean = false, stop: () -> Boolean): Boolean {
        val gapMs = (((if (fast) fastGapS else minGapS) + Random.nextDouble(0.0, maxOf(0.1, if (fast) fastJitterS else jitterS))) * 1000).toLong()
        while (System.currentTimeMillis() < last + gapMs) {
            if (stop()) return false
            Thread.sleep(300)
        }
        val pages = pagesToday(); val fasts = fastToday()
        if (if (fast) fasts >= maxFastPerDay else pages >= maxPerDay) return false
        prefs.sp.edit().putString("pacer_day", today())
            .putInt("pacer_count", if (fast) pages else pages + 1).putInt("pacer_fast", if (fast) fasts + 1 else fasts).apply()
        last = System.currentTimeMillis()
        return true
    }

    fun hostPausedMs(url: String): Long = maxOf(0L, prefs.sp.getLong("pause_" + host(url), 0L) - System.currentTimeMillis())

    fun strike(url: String): Long {
        val h = host(url)
        val n = prefs.sp.getInt("strike_$h", 0) + 1
        val waitMs = minOf(4L * 3600_000L, 1800_000L * (1L shl (n - 1).coerceAtMost(4)))
        prefs.sp.edit().putInt("strike_$h", n).putLong("pause_$h", System.currentTimeMillis() + waitMs).apply()
        return waitMs
    }

    fun clear(url: String) {
        val h = host(url)
        prefs.sp.edit().remove("strike_$h").apply()
    }
}
