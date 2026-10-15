package com.deepkmr0007.railvision

import android.app.Application
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class RVApp : Application() {
    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        Notifier.channels(this)
        BackgroundWorker.schedule(this)
        Thread({ Engine.ensureStarted(this) }, "rv-engine-start").start()
    }
}
