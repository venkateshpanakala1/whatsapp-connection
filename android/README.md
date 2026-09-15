# Native Android companion

This Android project is separate from the Flask web app and uses the same Railway backend, webhook, PostgreSQL call records, and Meta call-action endpoints.

## Before building

1. In Firebase Console, create/register the Android app with package name `app.whatsappconnection`.
2. Download its `google-services.json` into `android/app/google-services.json`. Do not commit it.
3. Add a custom ringtone as `android/app/src/main/res/raw/incoming_call.ogg` (or `.wav`). The app falls back safely if it is absent, but the release build should include it.
4. On Railway configure **one** of:
   - `FIREBASE_SERVICE_ACCOUNT_JSON`: the complete Firebase service-account JSON, or
   - `GOOGLE_APPLICATION_CREDENTIALS`: a readable service-account JSON path in the deployed container.
5. Set `WHATSAPP_CALLING_ENABLED=true` as already required for incoming calls.

## Build

Install Android Studio (which supplies the Gradle wrapper) or add a standard Gradle wrapper, then from this directory run:

```powershell
./gradlew.bat assembleDebug
```

The APK is generated at `app/build/outputs/apk/debug/app-debug.apk`. Copy it to the backend's release artifact as `public/downloads/V7.apk` only after testing; Railway can then serve it at `/downloads/V7.apk`. The login page's **Download Android APK** button already uses this URL; the old **Install Web Shortcut** button remains available for the PWA.

## Current phase

Implemented: Android login, encrypted bearer-token storage, FCM registration, high-priority incoming-call notification, call-style/full-screen notification, custom ringtone channel, and reject synchronization.

Native WebRTC answer/media is intentionally not enabled yet: accepting a Meta WhatsApp call requires a tested native `org.webrtc` implementation that creates an SDP answer, waits for ICE, then calls `claim`, `pre_accept`, and `accept`. Do not expose an Answer button until that phase is tested on a physical Android device.
