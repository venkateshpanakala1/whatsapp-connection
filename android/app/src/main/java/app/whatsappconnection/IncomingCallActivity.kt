package app.whatsappconnection

import android.os.Bundle
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

class IncomingCallActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val callId = intent.getStringExtra("call_id") ?: return finish()
        val title = TextView(this).apply { text = "Incoming WhatsApp Call"; textSize = 24f }
        val detail = TextView(this).apply { text = "Open the web Replies page to answer this call while native WebRTC setup is completed." }
        val reject = Button(this).apply { text = "Reject"; setOnClickListener {
            Thread { try { BackendApi.callAction(this@IncomingCallActivity, callId, "reject") } catch (_: Exception) {}
                CallNotification.dismiss(this@IncomingCallActivity); finish() }.start()
        } }
        setContentView(LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(48, 120, 48, 48)
            addView(title); addView(detail); addView(reject) })
    }
}
