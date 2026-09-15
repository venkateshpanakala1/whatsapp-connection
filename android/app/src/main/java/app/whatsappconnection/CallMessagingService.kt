package app.whatsappconnection

import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

class CallMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        super.onNewToken(token)
        // The app re-registers this token after sign-in. A token refresh while
        // signed in is queued for the same authenticated registration call.
        getSharedPreferences("fcm", MODE_PRIVATE).edit().putString("token", token).apply()
        if (SecureSession(this).accessToken != null) {
            Thread { try { BackendApi.registerFcm(this, token) } catch (_: Exception) {} }.start()
        }
    }

    override fun onMessageReceived(message: RemoteMessage) {
        if (message.data["type"] != "incoming_call") return
        CallNotification.show(
            this,
            message.data["call_id"] ?: return,
            message.data["caller_name"].orEmpty(),
            message.data["caller_phone"].orEmpty(),
        )
    }
}
