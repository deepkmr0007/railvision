plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// The phone app runs the SAME Python code as the desktop version: railvision/ and web/ are copied in at build time.
val repoRoot: File = rootDir.parentFile
val rvVersion: String = Regex("__version__\\s*=\\s*\"([^\"]+)\"")
    .find(repoRoot.resolve("railvision/__init__.py").readText())?.groupValues?.get(1) ?: "0.0.0"
val rvParts = rvVersion.split(".").map { it.toIntOrNull() ?: 0 } + listOf(0, 0, 0)
val runNumber = (System.getenv("GITHUB_RUN_NUMBER")?.toIntOrNull() ?: 0) % 1000
val rvVersionCode = (rvParts[0] * 100 + rvParts[1]) * 10000 + rvParts[2] * 1000 + runNumber

val pyDir: File = layout.buildDirectory.dir("rv-python").get().asFile
val webDir: File = layout.buildDirectory.dir("rv-web").get().asFile
sync {
    from(repoRoot.resolve("railvision")) { exclude("**/__pycache__/**", "**/*.pyc") }
    into(pyDir.resolve("railvision"))
}
sync {
    from(repoRoot.resolve("web"))
    into(webDir.resolve("web"))
}

android {
    namespace = "com.deepkmr0007.railvision"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.deepkmr0007.railvision"
        minSdk = 26
        targetSdk = 35
        versionCode = rvVersionCode
        versionName = rvVersion
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }

    signingConfigs {
        create("rv") {
            // A real key can be supplied from GitHub secrets; otherwise the public build key in this folder is used,
            // which keeps the signature the same between releases so updates install over the old version.
            storeFile = file(System.getenv("RV_KEYSTORE_FILE") ?: "railvision-public.p12")
            storePassword = System.getenv("RV_KEYSTORE_PASSWORD") ?: "railvision"
            keyAlias = System.getenv("RV_KEY_ALIAS") ?: "railvision"
            keyPassword = System.getenv("RV_KEY_PASSWORD") ?: "railvision"
            storeType = "pkcs12"
        }
    }

    buildTypes {
        getByName("debug") { signingConfig = signingConfigs.getByName("rv") }
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("rv")
        }
    }

    sourceSets {
        getByName("main") { assets.srcDir(webDir) }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { buildConfig = true }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        System.getenv("RV_BUILD_PYTHON")?.let { buildPython(it) }
        pip {
            install("pypdf")
            install("certifi")
        }
    }
    sourceSets {
        getByName("main") { srcDir(pyDir.path) }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
}
