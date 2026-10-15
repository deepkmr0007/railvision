# RAILVISION AI on your Android phone (v2.4)

The phone app is the full RAILVISION, not a web link. The engine (live status, crossing checks,
journeys, announcements, the chatbot) runs **inside the app**, and its browser assistant opens the
official NTES / IRCTC / PNR / zonal railway pages itself, one page at a time. You don't need a computer.

## Install

1. On the phone, open **https://github.com/deepkmr0007/railvision/releases/latest**.
2. Tap **RailVision.apk** to download it, then open it from the download notification.
3. The first time, Android asks to allow installs from your browser: *Settings → Install unknown apps →
   (your browser) → Allow*. Go back and tap **Install**.
4. Open **RAILVISION AI**. Allow notifications when asked.

Direct link to the newest build: `https://github.com/deepkmr0007/railvision/releases/latest/download/RailVision.apk`

* Android 8.0 or newer, 64-bit phones (almost all phones since 2019). About 60–80 MB installed.
* Updates: install a newer APK over the old one. Your trips, watch list and settings stay.
* It is not on the Play Store. Play Protect may say "unknown app"; that's expected for apps installed from
  GitHub. Each release lists the APK's SHA-256 so you can check the file.

## What runs where

| Part | On the phone |
|---|---|
| Engine | Python, inside the app, reachable only from the phone itself (127.0.0.1) |
| Dashboard | The same dashboard as the desktop version, in the app |
| Browser assistant | The app's own browser. Same rules: ≥20 s between pages, ≤400 pages a day, stops at CAPTCHAs and logins |
| Alerts | Android notifications (delays, platform, chart, berths, PNR, announcements) |
| Background | Every 15 minutes with the app closed (Android's minimum). *Keep running* in Settings → Phone app checks every few minutes and shows a small permanent notification |

## When a site shows a CAPTCHA

The assistant never solves it. You get a notification **"The assistant needs you"**. Tap it: the app
opens the assistant browser at the bottom of the screen. Type the CAPTCHA and press the site's button.
The assistant notices the result within a few seconds and carries on. If nobody answers within
5 minutes, that site pauses for 30 minutes (doubling up to 4 hours), exactly like the desktop version.

The PNR page always has a CAPTCHA, so **Check PNR** always asks you.

## Settings → Phone app

* **Browser assistant reads the official pages for me** — on by default.
* **Keep running with the app closed** — more frequent checks; uses more battery.
* **Let it run in background** — asks Android not to delay the app's checks to save battery. Some phone
  brands (Xiaomi, Oppo, Vivo, Realme, OnePlus, Samsung) also have their own "auto-start" or "sleeping
  apps" lists; add RAILVISION there if checks stop when the screen is off.
* **Open assistant browser** — watch what it is doing.

## Privacy

Everything stays on the phone, in the app's private storage: the database, your token, your trips.
Nothing is sent to the developer. The app talks only to the official sites you watch, PIB, the zonal
railway sites, and X or WhatsApp only if you add your own keys.

## Building it yourself

GitHub Actions builds it on every push (`.github/workflows/android.yml`). Locally: install Android
Studio (JDK 17, Android SDK 35) and Python 3.12, then

```
cd android
gradle assembleRelease        # Gradle 8.10+; or open the android/ folder in Android Studio
```

The APK is signed with `android/app/railvision-public.p12` (password `railvision`). That key is
public, so anyone can sign an APK that installs over yours. To use your own key, add repository
secrets `RV_KEYSTORE_B64` (base64 of a PKCS12 file), `RV_KEYSTORE_PASSWORD`, `RV_KEY_ALIAS` and
`RV_KEY_PASSWORD`. A key change means uninstalling the old app once.

## Not verified yet

The phone's page-reading driver (`android/app/src/main/assets/rv_driver.js`) passes the same mock-site
tests as the desktop assistant (`tests/test_phone_driver.py`). The real NTES / IRCTC pages may be laid
out differently on a phone; if a check says *Needs teaching*, record it once with the desktop
`teach` command and the phone uses that recipe after you copy it in through the dashboard.
