package com.deepkmr0007.railvision

import android.content.Context
import android.content.res.AssetManager
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * The RAILVISION engine (the same Python code as the desktop version) running inside the app,
 * listening on 127.0.0.1 only. Everything is stored in the app's private folder.
 */
object Engine {
    private const val TAG = "RVEngine"
    private val lock = Any()

    @Volatile var port = 0; private set
    @Volatile var token = ""; private set
    @Volatile var version = ""; private set
    @Volatile var started = false; private set
    @Volatile var lastError: String? = null; private set

    val baseUrl: String get() = "http://127.0.0.1:$port"
    val dashboardUrl: String get() = "$baseUrl/#token=$token"

    /** Blocking; call from a background thread. Safe to call many times. */
    fun ensureStarted(ctx: Context): Boolean {
        if (started) return true
        synchronized(lock) {
            if (started) return true
            return try {
                val app = ctx.applicationContext
                if (!Python.isStarted()) Python.start(AndroidPlatform(app))
                val web = copyWeb(app)
                val data = File(app.filesDir, "railvision").apply { mkdirs() }
                val res = Python.getInstance().getModule("railvision.mobile")
                    .callAttr("start", data.absolutePath, web.absolutePath).toString()
                val j = JSONObject(res)
                port = j.getInt("port")
                token = j.getString("token")
                version = j.optString("version")
                started = true
                lastError = null
                Log.i(TAG, "engine $version on port $port")
                true
            } catch (t: Throwable) {
                lastError = t.toString()
                Log.e(TAG, "engine failed to start", t)
                false
            }
        }
    }

    /** One pass of tracking + announcements + reminders (the background job calls this). */
    fun runOnce(): String = try {
        Python.getInstance().getModule("railvision.mobile").callAttr("run_once").toString()
    } catch (t: Throwable) {
        "{\"error\": \"${t.javaClass.simpleName}\"}"
    }

    fun api(method: String, path: String, body: JSONObject? = null, timeoutMs: Int = 30_000): JSONObject {
        val c = URL(baseUrl + path).openConnection() as HttpURLConnection
        try {
            c.requestMethod = method
            c.connectTimeout = 10_000
            c.readTimeout = timeoutMs
            c.setRequestProperty("Authorization", "Bearer $token")
            c.setRequestProperty("Content-Type", "application/json")
            c.setRequestProperty("X-RV-Agent", "railvision-android/" + BuildConfig.VERSION_NAME)
            if (body != null) {
                c.doOutput = true
                c.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val code = c.responseCode
            val stream = if (code >= 400) c.errorStream else c.inputStream
            val txt = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            if (code >= 400) throw IOException("HTTP $code: ${txt.take(300)}")
            return if (txt.isBlank()) JSONObject() else JSONObject(txt)
        } finally {
            c.disconnect()
        }
    }

    /** The dashboard files are served by the Python engine, so they are unpacked once per app version. */
    private fun copyWeb(ctx: Context): File {
        val out = File(ctx.filesDir, "web")
        val stamp = File(out, ".stamp")
        val info = ctx.packageManager.getPackageInfo(ctx.packageName, 0)
        val want = "${info.lastUpdateTime}"
        if (stamp.exists() && stamp.readText() == want) return out
        out.deleteRecursively()
        out.mkdirs()
        copyAssets(ctx.assets, "web", out)
        stamp.writeText(want)
        return out
    }

    private fun copyAssets(am: AssetManager, path: String, dest: File) {
        val names = am.list(path) ?: return
        for (n in names) {
            val sub = "$path/$n"
            val children = am.list(sub)
            if (children != null && children.isNotEmpty()) {
                copyAssets(am, sub, File(dest, n).apply { mkdirs() })
            } else {
                try {
                    am.open(sub).use { inp -> File(dest, n).outputStream().use { inp.copyTo(it) } }
                } catch (e: IOException) {
                    File(dest, n).mkdirs()          // an empty folder
                }
            }
        }
    }
}
