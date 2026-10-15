package com.deepkmr0007.railvision

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import org.json.JSONObject

class MainActivity : Activity() {
    companion object {
        const val EXTRA_ASSISTANT = "open_assistant"
        @Volatile var visible = false
    }

    private val ui = Handler(Looper.getMainLooper())
    private lateinit var dash: WebView
    private lateinit var panel: LinearLayout
    private lateinit var panelHost: FrameLayout
    private lateinit var statusText: TextView
    private lateinit var splash: TextView
    private var engineReady = false

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)

        val root = FrameLayout(this).apply { setBackgroundColor(Color.parseColor("#0B1220")) }
        val column = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(column, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        dash = WebView(this)
        column.addView(dash, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))

        // The assistant panel: the same browser the assistant uses, so you can watch it or type a CAPTCHA yourself.
        panel = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; visibility = View.GONE; setBackgroundColor(Color.parseColor("#111A2E")) }
        val bar = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; setPadding(dp(10), dp(4), dp(4), dp(4)) }
        statusText = TextView(this).apply { setTextColor(Color.WHITE); textSize = 13f; maxLines = 2; text = "Assistant: ${AssistantAgent.status}" }
        bar.addView(statusText, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        bar.addView(Button(this).apply { text = "Check now"; isAllCaps = false; setOnClickListener { AssistantAgent.poke() } })
        bar.addView(Button(this).apply { text = "Hide"; isAllCaps = false; setOnClickListener { showPanel(false) } })
        panel.addView(bar)
        panelHost = FrameLayout(this)
        panel.addView(panelHost, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        column.addView(panel, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, (resources.displayMetrics.heightPixels * 0.55).toInt()))

        splash = TextView(this).apply {
            text = "RAILVISION AI\n\nStarting the engine on your phone…\n(the first start takes a few seconds)"
            setTextColor(Color.WHITE); textSize = 17f; gravity = Gravity.CENTER; typeface = Typeface.DEFAULT_BOLD
            setBackgroundColor(Color.parseColor("#0B1220"))
        }
        root.addView(splash, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)

        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val b = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime() or WindowInsetsCompat.Type.displayCutout())
            v.setPadding(b.left, b.top, b.right, b.bottom)
            WindowInsetsCompat.CONSUMED
        }

        with(dash.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            setSupportMultipleWindows(false)
        }
        dash.addJavascriptInterface(AppBridge(), "RVAndroid")
        dash.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val u = request.url
                if (u.host == "127.0.0.1" && u.port == Engine.port) return false     // the dashboard itself
                openOutside(u)                                                        // official sites, WhatsApp, tel: …
                return true
            }
        }

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 7)
        }

        Thread({
            val ok = Engine.ensureStarted(this)
            ui.post {
                if (ok) {
                    engineReady = true
                    dash.loadUrl(Engine.dashboardUrl)
                    splash.visibility = View.GONE
                    if (visible) startBackgroundParts()
                    if (intent?.getBooleanExtra(EXTRA_ASSISTANT, false) == true) showPanel(true)
                } else {
                    splash.text = "RAILVISION could not start.\n\n${Engine.lastError ?: ""}\n\nClose the app and open it again."
                }
            }
        }, "rv-ui-start").start()
    }

    private fun startBackgroundParts() {
        AssistantAgent.startLoop(this)
        Notifier.startPolling(this)
        if (Prefs(this).keepRunning) KeepAliveService.start(this)
    }

    override fun onStart() {
        super.onStart()
        visible = true
        AssistantAgent.onStatus = { s -> ui.post { statusText.text = "Assistant: $s" } }
        AssistantAgent.onNeedsYou = { r -> ui.post { if (r != null) showPanel(true) } }
        if (engineReady) startBackgroundParts()
        if (AssistantAgent.needsYou != null && engineReady) showPanel(true)
    }

    override fun onStop() {
        visible = false
        AssistantAgent.onStatus = null
        AssistantAgent.onNeedsYou = null
        if (!Prefs(this).keepRunning) {
            AssistantAgent.stopLoop()          // finishes the page it is on; the 15-minute job carries on
            Notifier.stopPolling()
        }
        super.onStop()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent.getBooleanExtra(EXTRA_ASSISTANT, false) && engineReady) showPanel(true)
    }

    override fun onDestroy() {
        AssistantAgent.webView?.let { if (it.parent === panelHost) { panelHost.removeView(it); AssistantAgent.detached() } }
        dash.destroy()
        super.onDestroy()
    }

    @Deprecated("Back handling for all Android versions")
    override fun onBackPressed() {
        when {
            panel.visibility == View.VISIBLE -> showPanel(false)
            dash.canGoBack() -> dash.goBack()
            else -> super.onBackPressed()
        }
    }

    fun showPanel(show: Boolean) {
        if (show) {
            val wv = AssistantAgent.webView(this)
            if (wv.parent !== panelHost) {
                (wv.parent as? ViewGroup)?.removeView(wv)
                panelHost.addView(wv, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
            }
            statusText.text = "Assistant: ${AssistantAgent.needsYou ?: AssistantAgent.status}"
            panel.visibility = View.VISIBLE
        } else {
            AssistantAgent.webView?.let { if (it.parent === panelHost) { panelHost.removeView(it); AssistantAgent.detached() } }
            panel.visibility = View.GONE
        }
    }

    private fun openOutside(u: Uri) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, u).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: ActivityNotFoundException) { /* nothing can open it */ }
    }

    /** What the dashboard can ask the phone to do. Only the app's own dashboard (127.0.0.1) gets this. */
    inner class AppBridge {
        @JavascriptInterface fun isApp(): Boolean = true
        @JavascriptInterface fun version(): String = BuildConfig.VERSION_NAME

        @JavascriptInterface fun phoneState(): String {
            val pm = getSystemService(PowerManager::class.java)
            val p = Prefs(this@MainActivity)
            return JSONObject()
                .put("keep_running", p.keepRunning)
                .put("assistant_on", p.assistantOn)
                .put("battery_unrestricted", pm.isIgnoringBatteryOptimizations(packageName))
                .put("notifications", Build.VERSION.SDK_INT < 33 ||
                    checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)
                .put("assistant_status", AssistantAgent.status)
                .put("needs_you", AssistantAgent.needsYou ?: "")
                .put("version", BuildConfig.VERSION_NAME)
                .toString()
        }

        @JavascriptInterface fun setKeepRunning(on: Boolean) {
            Prefs(this@MainActivity).keepRunning = on
            if (on) KeepAliveService.start(this@MainActivity) else KeepAliveService.stop(this@MainActivity)
        }

        @JavascriptInterface fun setAssistantOn(on: Boolean) {
            Prefs(this@MainActivity).assistantOn = on
            if (on) AssistantAgent.startLoop(this@MainActivity) else AssistantAgent.stopLoop(abortCurrent = true)
        }

        @JavascriptInterface fun openAssistant() { ui.post { showPanel(true) } }
        @JavascriptInterface fun checkNow() { AssistantAgent.poke() }

        @SuppressLint("BatteryLife")
        @JavascriptInterface fun askBatteryUnrestricted() {
            ui.post {
                try {
                    startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
                } catch (e: Exception) {
                    try { startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) } catch (_: Exception) { }
                }
            }
        }

        @JavascriptInterface fun askNotifications() {
            ui.post {
                if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 7)
                else startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName))
            }
        }

        /** Share a status / PNR / alert text to WhatsApp or any app, as yourself. */
        @JavascriptInterface fun shareText(title: String, text: String) {
            ui.post {
                val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text).putExtra(Intent.EXTRA_SUBJECT, title)
                startActivity(Intent.createChooser(send, title))
            }
        }
    }
}
