package app.whatsappconnection

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.Gravity
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.firebase.messaging.FirebaseMessaging

class MainActivity : AppCompatActivity() {
    private lateinit var email: EditText
    private lateinit var password: EditText
    private lateinit var status: TextView
    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        email = EditText(this).apply { hint = "Email" }
        password = EditText(this).apply { hint = "Password"; inputType = 0x81 }
        status = TextView(this).apply { gravity = Gravity.CENTER; text = "Sign in to receive WhatsApp calls" }
        val signIn = Button(this).apply { text = "Sign in"; setOnClickListener { signIn() } }
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; setPadding(48, 96, 48, 48)
            addView(status); addView(email); addView(password); addView(signIn)
        })
        if (SecureSession(this).accessToken != null) registerFcm()
    }

    private fun signIn() {
        status.text = "Signing in…"
        Thread {
            try {
                val data = BackendApi.login(this, email.text.toString(), password.text.toString())
                SecureSession(this).accessToken = data.getString("access_token")
                runOnUiThread { status.text = "Signed in. Registering this device…" }
                registerFcm()
            } catch (error: Exception) {
                runOnUiThread { status.text = error.message ?: "Sign-in failed" }
            }
        }.start()
    }

    private fun registerFcm() {
        FirebaseMessaging.getInstance().token.addOnSuccessListener { fcmToken ->
            Thread {
                try {
                    BackendApi.registerFcm(this, fcmToken)
                    runOnUiThread { status.text = "This Android device is ready for incoming calls." }
                } catch (error: Exception) {
                    runOnUiThread { status.text = error.message ?: "Could not register this device" }
                }
            }.start()
        }
    }
}
