package app.whatsappconnection

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.net.Uri
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.Person

object CallNotification {
    private const val CHANNEL_ID = "incoming_calls_v1"
    private const val NOTIFICATION_ID = 4401

    fun show(context: Context, callId: String, callerName: String, callerPhone: String) {
        createChannel(context)
        val displayName = callerName.ifBlank { callerPhone.ifBlank { "WhatsApp customer" } }
        val open = PendingIntent.getActivity(context, 0,
            Intent(context, IncomingCallActivity::class.java).putExtra("call_id", callId),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val reject = PendingIntent.getBroadcast(context, 1,
            Intent(context, CallActionReceiver::class.java).putExtra("call_id", callId).putExtra("action", "reject"),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentTitle("Incoming WhatsApp call")
            .setContentText(displayName)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setOngoing(true)
            .setFullScreenIntent(open, true)
            .setStyle(NotificationCompat.CallStyle.forIncomingCall(
                Person.Builder().setName(displayName).setImportant(true).build(),
                reject, open,
            ))
            .build()
        NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, notification)
    }

    fun dismiss(context: Context) = NotificationManagerCompat.from(context).cancel(NOTIFICATION_ID)

    private fun createChannel(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(CHANNEL_ID, "Incoming WhatsApp calls", NotificationManager.IMPORTANCE_HIGH)
        // Add android/app/src/main/res/raw/incoming_call.ogg to override this
        // with the bundled ringtone. Channel settings are user-owned after
        // creation, so change CHANNEL_ID when shipping a replacement tone.
        channel.setSound(Uri.parse("android.resource://${context.packageName}/raw/incoming_call"),
            AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE).build())
        channel.enableVibration(true)
        manager.createNotificationChannel(channel)
    }
}
