package com.deepkmr0007.railvision

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.util.Base64
import android.util.Log
import android.view.View
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.JsResult
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.temporal.ChronoUnit
import java.util.UUID
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock

/**
 * The browser assistant inside the app. It opens the official pages (NTES, IRCTC, the PNR page, zonal
 * railway notices) in its own WebView — strictly one page at a time — fills in the train number and date
 * like a person would, reads what the page shows and gives it to the engine.
 *
 * It never solves a CAPTCHA, never logs in and never works around a block: it stops, notifies you and,
 * if you open the assistant panel, lets you finish that step yourself.
 *
 * The job loop mirrors tests/phone_harness.py, which is tested against the mock sites.
 */
object AssistantAgent {
    private const val TAG = "RVAgent"
    private const val HUMAN_TIMEOUT_MS = 5 * 60_000L
    private const val VIEW_W = 1080
    private const val VIEW_H = 2160

    private val main = Handler(Looper.getMainLooper())
    private val pending = ConcurrentHashMap<String, CompletableFuture<String>>()
    private val roundLock = ReentrantLock()
    private val wake = Object()

    @Volatile var webView: WebView? = null; private set
    @Volatile var status: String = "Idle"; private set
    @Volatile var needsYou: String? = null; private set
    @Volatile var onStatus: ((String) -> Unit)? = null
    @Volatile var onNeedsYou: ((String?) -> Unit)? = null

    @Volatile private var loading = false
    @Volatile private var mainStatus = 0
    @Volatile private var lastDownload: String? = null
    @Volatile private var loopThread: Thread? = null
    @Volatile private var stopLoop = false
    @Volatile private var abortRound = false
    private var driverJs: String? = null
    private var appCtx: Context? = null
    private var pacer: Pacer? = null

    // ------------------------------------------------------------------ setup
    private fun init(ctx: Context) {
        val app = ctx.applicationContext
        if (appCtx == null) appCtx = app
        if (driverJs == null) driverJs = app.assets.open("rv_driver.js").bufferedReader().use { it.readText() }
        if (pacer == null) pacer = Pacer(Prefs(app))
    }

    private fun <T> onMain(timeoutMs: Long = 20_000, block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        val f = CompletableFuture<T>()
        main.post {
            try { f.complete(block()) } catch (t: Throwable) { f.completeExceptionally(t) }
        }
        return f.get(timeoutMs, TimeUnit.MILLISECONDS)
    }

    fun webView(ctx: Context): WebView {
        init(ctx)
        return onMain { webView ?: create(ctx.applicationContext).also { webView = it } }
    }

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    private fun create(ctx: Context): WebView {
        val wv = WebView(ctx)
        val s = wv.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true
        s.useWideViewPort = true
        s.loadWithOverviewMode = true
        s.builtInZoomControls = true
        s.displayZoomControls = false
        s.setSupportMultipleWindows(false)
        s.javaScriptCanOpenWindowsAutomatically = false
        s.allowFileAccess = false
        s.allowContentAccess = false
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(wv, true)
        wv.addJavascriptInterface(Bridge(), "RVBridge")
        wv.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) { loading = true }
            override fun onPageFinished(view: WebView, url: String?) { loading = false }
            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, errorResponse: WebResourceResponse) {
                if (request.isForMainFrame) mainStatus = errorResponse.statusCode
            }
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val sch = request.url.scheme ?: ""
                return !(sch == "http" || sch == "https")        // no app links, intents, tel: etc.
            }
        }
        wv.webChromeClient = object : WebChromeClient() {
            override fun onJsAlert(view: WebView, url: String?, message: String?, result: JsResult): Boolean {
                result.confirm(); return true
            }
            override fun onJsConfirm(view: WebView, url: String?, message: String?, result: JsResult): Boolean {
                result.cancel(); return true                    // never agree to anything on the user's behalf
            }
        }
        wv.setDownloadListener { url, _, _, _, _ -> lastDownload = url; loading = false }
        layoutOffscreen(wv)
        return wv
    }

    /** Off screen the WebView still needs a size, or every element would measure 0 × 0. */
    private fun layoutOffscreen(wv: WebView) {
        if (wv.parent != null) return
        wv.measure(View.MeasureSpec.makeMeasureSpec(VIEW_W, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(VIEW_H, View.MeasureSpec.EXACTLY))
        wv.layout(0, 0, VIEW_W, VIEW_H)
    }

    /** Called by the screen after it takes the WebView out of its layout. */
    fun detached() { webView?.let { wv -> main.post { layoutOffscreen(wv) } } }

    private class Bridge {
        @JavascriptInterface
        fun post(id: String, json: String) { pending.remove(id)?.complete(json) }
    }

    // ------------------------------------------------------------------ status
    private fun setStatus(s: String) {
        status = s
        Log.i(TAG, s)
        onStatus?.invoke(s)
    }

    private fun heartbeat(state: String, reason: String = "") {
        try {
            Engine.api("POST", "/api/assist/heartbeat", JSONObject().put("state", state).put("reason", reason)
                .put("info", JSONObject().put("agent", "android").put("version", BuildConfig.VERSION_NAME)))
        } catch (e: Exception) { /* engine busy; next time */ }
    }

    private fun askPerson(reason: String) {
        needsYou = reason
        setStatus("Needs you: $reason")
        heartbeat("needs_you", reason)
        appCtx?.let { Notifier.needsYou(it, reason) }
        onNeedsYou?.invoke(reason)
    }

    private fun clearAsk() {
        if (needsYou == null) return
        needsYou = null
        appCtx?.let { Notifier.clearNeedsYou(it) }
        onNeedsYou?.invoke(null)
    }

    // ------------------------------------------------------------------ page helpers
    private fun evalSync(js: String, timeoutMs: Long = 10_000): String? {
        val f = CompletableFuture<String?>()
        main.post {
            val wv = webView
            if (wv == null) f.complete(null) else wv.evaluateJavascript(js) { f.complete(it) }
        }
        return try { f.get(timeoutMs, TimeUnit.MILLISECONDS) } catch (e: Exception) { null }
    }

    private fun ensureDriver() {
        if (evalSync("!!(window.rvDriver && window.rvDriver.v === 3)") != "true") evalSync(driverJs + "\n;true")
    }

    /** One driver step. null = the page changed under us (re-inject and retry). */
    private fun step(job: JSONObject, timeoutMs: Long = 30_000): JSONObject? {
        ensureDriver()
        val id = UUID.randomUUID().toString()
        val f = CompletableFuture<String>()
        pending[id] = f
        val js = "(function(){var id='$id';try{window.rvDriver.step($job).then(function(r){RVBridge.post(id,JSON.stringify(r))}," +
            "function(e){RVBridge.post(id,JSON.stringify({status:'failed',reason:'driver: '+e}))})}" +
            "catch(e){RVBridge.post(id,JSON.stringify({status:'retry',reason:String(e)}))}})();"
        main.post { webView?.evaluateJavascript(js, null) }
        return try {
            val r = JSONObject(f.get(timeoutMs, TimeUnit.MILLISECONDS))
            if (r.optString("status") == "retry") null else r
        } catch (e: Exception) {
            pending.remove(id); null
        }
    }

    private fun waitLoaded(timeoutMs: Long) {
        val end = System.currentTimeMillis() + timeoutMs
        Thread.sleep(300)
        while (loading && System.currentTimeMillis() < end) Thread.sleep(200)
    }

    private fun goto(url: String) {
        mainStatus = 0
        lastDownload = null
        loading = true
        onMain { webView!!.loadUrl(url) }
        waitLoaded(45_000)
    }

    private fun isPdf(url: String) = Regex("\\.pdf(\\?|$)", RegexOption.IGNORE_CASE).containsMatchIn(Uri.parse(url).path ?: url) ||
        Regex("\\.pdf(\\?|$)", RegexOption.IGNORE_CASE).containsMatchIn(url)

    /** A notice PDF, fetched with the same cookies and browser name as the assistant's WebView. */
    private fun fetchBytes(url: String, max: Int = 4_000_000): Pair<Int, ByteArray> {
        val c = URL(url).openConnection() as HttpURLConnection
        try {
            c.connectTimeout = 20_000
            c.readTimeout = 45_000
            appCtx?.let { c.setRequestProperty("User-Agent", WebSettings.getDefaultUserAgent(it)) }
            CookieManager.getInstance().getCookie(url)?.let { c.setRequestProperty("Cookie", it) }
            val code = c.responseCode
            if (code != 200) return code to ByteArray(0)
            val out = ByteArrayOutputStream()
            c.inputStream.use { inp ->
                val buf = ByteArray(16_384)
                while (out.size() < max) {
                    val n = inp.read(buf); if (n < 0) break
                    out.write(buf, 0, n)
                }
            }
            return code to out.toByteArray()
        } finally { c.disconnect() }
    }

    private fun isoNow(): String =
        OffsetDateTime.now(ZoneOffset.ofHoursMinutes(5, 30)).truncatedTo(ChronoUnit.SECONDS).toString()

    // ------------------------------------------------------------------ one job
    private fun runJob(job: JSONObject, recipe: JSONObject, watchId: String?): JSONObject {
        val kind = job.getString("kind")
        val vars = job.optJSONObject("vars") ?: JSONObject()
        val out = JSONObject()
            .put("recipe", recipe.optString("name", job.optString("recipe"))).put("kind", kind).put("vars", vars)
            .put("status", "ok").put("reason", "").put("text", "").put("tables", JSONArray()).put("links", JSONArray())
            .put("title", "").put("url", "").put("json", JSONArray()).put("started_at", isoNow())
        val notes = JSONArray()
        try {
            if (kind == "page") runPage(recipe, vars, out, notes) else runSteps(recipe, vars, out, notes)
        } catch (t: Throwable) {
            out.put("status", "error").put("reason", "${t.javaClass.simpleName}: ${(t.message ?: "").take(200)}")
        } finally {
            clearAsk()
        }
        out.put("steps", notes).put("finished_at", isoNow())
        if (watchId != null) out.put("watch_id", watchId)
        if (job.has("job_id") && !job.isNull("job_id")) out.put("job_id", job.get("job_id"))
        return out
    }

    private fun takeCapture(r: JSONObject, out: JSONObject) {
        val cap = r.optJSONObject("capture") ?: JSONObject()
        out.put("status", r.optString("result", "ok"))
        if (r.optString("note").isNotEmpty() && r.optString("result") != "ok") out.put("reason", r.optString("note"))
        for (k in listOf("text", "tables", "links", "title", "url")) if (cap.has(k)) out.put(k, cap.get(k))
    }

    private fun refused(out: JSONObject): Boolean {
        if (mainStatus == 401 || mainStatus == 403 || mainStatus == 429) {
            out.put("status", "blocked").put("reason", "The site refused the request (HTTP $mainStatus).")
            return true
        }
        return false
    }

    private fun runSteps(recipe: JSONObject, vars: JSONObject, out: JSONObject, notes: JSONArray) {
        val url = recipe.getString("start_url")
        require(url.startsWith("https://") || url.startsWith("http://")) { "recipe start address is not a web address" }
        notes.put("open $url")
        goto(url)
        if (refused(out)) return
        Thread.sleep(1000)
        var phase = "open"
        var after: Any = JSONObject.NULL
        var deadline = System.currentTimeMillis() + 90_000
        var humanDeadline = 0L
        var retries = 0
        for (i in 0 until 250) {
            if (abortRound) { out.put("status", "error").put("reason", "stopped"); return }
            val human = phase == "human"
            val j = JSONObject().put("recipe", recipe).put("vars", vars).put("phase", phase).put("handover", true)
                .put("after", after).put("deadline", (if (human) humanDeadline else deadline).toDouble())
            val r = step(j)
            if (r == null) {
                if (++retries > 8) { out.put("status", "recipe_failed").put("reason", "The page kept changing while it was being read."); return }
                waitLoaded(20_000); Thread.sleep(800); continue
            }
            r.optString("note").takeIf { it.isNotEmpty() }?.let { notes.put(it) }
            when (r.optString("status")) {
                "continue" -> {
                    phase = r.getString("next")
                    if (r.optBoolean("expectNav")) { Thread.sleep(1200); waitLoaded(20_000) } else Thread.sleep(250)
                }
                "waiting" -> Thread.sleep(1500)
                "needs_you" -> {
                    askPerson(r.optString("reason"))
                    phase = "human"
                    after = r.opt("after") ?: JSONObject.NULL
                    humanDeadline = System.currentTimeMillis() + HUMAN_TIMEOUT_MS
                }
                "done" -> { takeCapture(r, out); return }
                "blocked" -> { out.put("status", r.optString("kind", "blocked")).put("reason", r.optString("reason")); return }
                else -> { out.put("status", "recipe_failed").put("reason", r.optString("reason", "unknown driver answer")); return }
            }
            if (phase == "human") deadline = maxOf(deadline, humanDeadline + 60_000)
            if (phase != "human" && System.currentTimeMillis() > deadline) {
                out.put("status", "error").put("reason", "The check took too long."); return
            }
        }
        out.put("status", "error").put("reason", "too many steps")
    }

    private fun runPage(recipe: JSONObject, vars: JSONObject, out: JSONObject, notes: JSONArray) {
        val url = recipe.getString("start_url").replace("{url}", vars.optString("url"))
        out.put("url", url)
        require(url.startsWith("https://") || url.startsWith("http://")) { "not a web address" }
        fun readPdf(): Boolean {
            notes.put("read PDF ${url.takeLast(60)}")
            val (code, body) = fetchBytes(url)
            if (code != 200 || body.size < 5 || String(body, 0, 5, Charsets.ISO_8859_1) != "%PDF-") {
                out.put("status", "error").put("reason", "PDF not available (HTTP $code)"); return false
            }
            out.put("pdf_b64", Base64.encodeToString(body, Base64.NO_WRAP))
            return true
        }
        if (isPdf(url)) { readPdf(); return }
        notes.put("open ${url.take(90)}")
        goto(url)
        if (lastDownload != null) { readPdf(); return }   // served as a file download
        if (refused(out)) return
        Thread.sleep(1500)
        for (i in 0 until 6) {
            val r = step(JSONObject().put("recipe", recipe).put("vars", vars).put("phase", "capture").put("handover", false)
                .put("after", JSONObject.NULL).put("deadline", (System.currentTimeMillis() + 20_000).toDouble()))
            if (r == null) { waitLoaded(15_000); Thread.sleep(800); continue }
            when (r.optString("status")) {
                "done" -> { takeCapture(r, out); return }
                "blocked" -> { out.put("status", r.optString("kind", "blocked")).put("reason", r.optString("reason")); return }
                else -> { out.put("status", "error").put("reason", r.optString("reason", "could not read the page")); return }
            }
        }
        out.put("status", "error").put("reason", "The page kept changing while it was being read.")
    }

    // ------------------------------------------------------------------ rounds
    private fun label(j: JSONObject): String {
        val v = j.optJSONObject("vars") ?: JSONObject()
        val what = listOf("train", "query", "list", "pnr").firstNotNullOfOrNull { k -> v.optString(k).takeIf { it.isNotEmpty() } }
            ?: v.optString("url").takeLast(50)
        return "${j.optString("kind")} $what ${v.optString("date")}".trim()
    }

    /** Everything that is due now, one page at a time. Returns the number of pages read. */
    fun round(ctx: Context, budgetMs: Long = Long.MAX_VALUE): Int {
        if (!Prefs(ctx).assistantOn) return 0
        if (!roundLock.tryLock()) return 0
        val pm = ctx.applicationContext.getSystemService(Context.POWER_SERVICE) as PowerManager
        val wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "railvision:assistant")
        try {
            init(ctx)
            webView(ctx)
            abortRound = false
            val work = Engine.api("GET", "/api/assist/work")
            val p = pacer!!
            work.optJSONObject("pacing")?.let { pc ->
                p.minGapS = maxOf(p.floorS, pc.optDouble("min_gap_s", p.minGapS))
                p.jitterS = pc.optDouble("jitter_s", p.jitterS)
                p.maxPerDay = minOf(1000, pc.optInt("max_pages_per_day", p.maxPerDay))
            }
            val recipes = work.optJSONObject("recipes") ?: JSONObject()
            val todo = mutableListOf<Pair<JSONObject, String?>>()
            work.optJSONArray("jobs")?.let { a -> for (i in 0 until a.length()) todo += a.getJSONObject(i) to null }
            work.optJSONArray("watch")?.let { a ->
                for (i in 0 until a.length()) {
                    val item = a.getJSONObject(i)
                    val js = item.optJSONArray("jobs") ?: continue
                    for (k in 0 until js.length()) todo += js.getJSONObject(k) to item.optString("watch_id")
                }
            }
            if (todo.isEmpty()) return 0
            heartbeat("online")
            val end = if (budgetMs == Long.MAX_VALUE) Long.MAX_VALUE else System.currentTimeMillis() + budgetMs
            var n = 0
            for ((job, wid) in todo) {
                if (abortRound || System.currentTimeMillis() > end) break
                val rc = recipes.optJSONObject(job.optString("recipe")) ?: continue
                val startUrl = rc.optString("start_url").replace("{url}", job.optJSONObject("vars")?.optString("url") ?: "")
                val paused = p.hostPausedMs(startUrl)
                if (paused > 0) { setStatus("${Uri.parse(startUrl).host} paused for ${paused / 60_000} more min"); continue }
                if (!p.waitTurn { abortRound || System.currentTimeMillis() > end }) {
                    if (p.pagesToday() >= p.maxPerDay) setStatus("Daily limit of ${p.maxPerDay} pages reached — resuming tomorrow.")
                    break
                }
                wl.acquire(10 * 60_000L)
                setStatus("Checking ${label(job)}")
                val cap = runJob(job, rc, wid?.ifEmpty { null })
                if (wl.isHeld) wl.release()
                n++
                val st = cap.optString("status")
                if (st == "captcha" || st == "blocked" || st == "login") {
                    val waitMs = p.strike(startUrl)
                    cap.put("reason", cap.optString("reason") + " Pausing this site for ${waitMs / 60_000} min.")
                    heartbeat("paused", cap.optString("reason"))
                } else if (st == "ok") {
                    p.clear(startUrl)
                }
                try {
                    val res = Engine.api("POST", "/api/assist/captures", cap, 60_000)
                    setStatus("$st: ${res.optString("headline", label(job))}")
                } catch (e: Exception) {
                    setStatus("Could not save the result: ${e.message}")
                }
            }
            heartbeat("online")
            return n
        } finally {
            if (wl.isHeld) wl.release()
            roundLock.unlock()
        }
    }

    // ------------------------------------------------------------------ continuous loop
    fun startLoop(ctx: Context) {
        val app = ctx.applicationContext
        if (loopThread?.isAlive == true) { poke(); return }
        stopLoop = false
        loopThread = Thread({
            while (!stopLoop) {
                val did = try {
                    if (Engine.ensureStarted(app)) round(app) else 0
                } catch (t: Throwable) {
                    setStatus("Waiting: ${t.message ?: t.javaClass.simpleName}"); 0
                }
                if (did == 0 && status.startsWith("Checking")) setStatus("Idle — nothing due")
                synchronized(wake) { if (!stopLoop) wake.wait(if (did == 0) 15_000L else 2_000L) }
            }
        }, "rv-agent").apply { isDaemon = true; start() }
    }

    fun stopLoop(abortCurrent: Boolean = false) {
        stopLoop = true
        if (abortCurrent) abortRound = true
        synchronized(wake) { wake.notifyAll() }
    }

    fun poke() { synchronized(wake) { wake.notifyAll() } }

    val loopRunning: Boolean get() = loopThread?.isAlive == true && !stopLoop
}
