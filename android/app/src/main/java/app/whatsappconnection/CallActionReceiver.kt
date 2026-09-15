package app.whatsappconnection

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class CallActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val callId = intent.getStringExtra("call_id") ?: return
        val pending = goAsync()
        Thread {
            try {
                if (intent.getStringExtra("action") == "reject") BackendApi.callAction(context, callId, "reject")
            } catch (_: Exception) {
                // The next backend/FCM state update will reconcile the UI.
            } finally {
                CallNotification.dismiss(context)
                pending.finish()
            }
        }.start()
    }
}
