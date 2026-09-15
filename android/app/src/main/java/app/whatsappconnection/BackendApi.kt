package app.whatsappconnection

import android.content.Context
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.UUID

object BackendApi {
    private val client = OkHttpClient()
    private val json = "application/json; charset=utf-8".toMediaType()

    private fun request(path: String, body: JSONObject? = null, token: String? = null): JSONObject {
        val builder = Request.Builder().url(BuildConfig.BACKEND_BASE_URL + path)
            .header("Accept", "application/json")
        if (token != null) builder.header("Authorization", "Bearer $token")
        if (body != null) builder.post(body.toString().toRequestBody(json)) else builder.get()
        client.newCall(builder.build()).execute().use { response ->
            val result = JSONObject(response.body?.string().orEmpty().ifBlank { "{}" })
            if (!response.isSuccessful) throw IllegalStateException(result.optString("error", "Request failed"))
            return result
        }
    }

    fun login(context: Context, email: String, password: String): JSONObject =
        request("/api/mobile/auth/login", JSONObject().put("email", email).put("password", password)
            .put("device_name", android.os.Build.MODEL))

    fun registerFcm(context: Context, token: String) {
        val access = SecureSession(context).accessToken ?: return
        request("/api/mobile/devices/register", JSONObject().put("fcm_token", token)
            .put("device_name", android.os.Build.MODEL), access)
    }

    fun callAction(context: Context, callId: String, action: String) {
        val access = SecureSession(context).accessToken ?: throw IllegalStateException("Please sign in again")
        val agent = context.getSharedPreferences("agent", Context.MODE_PRIVATE)
            .getString("id", null) ?: UUID.randomUUID().toString().also {
                context.getSharedPreferences("agent", Context.MODE_PRIVATE).edit().putString("id", it).apply()
            }
        val builder = Request.Builder().url(BuildConfig.BACKEND_BASE_URL + "/api/calls/$callId/action")
            .header("Authorization", "Bearer $access")
            .header("X-Call-Agent-ID", "android:$agent")
            .post(JSONObject().put("action", action).toString().toRequestBody(json))
        client.newCall(builder.build()).execute().use { response ->
            if (!response.isSuccessful) {
                val result = JSONObject(response.body?.string().orEmpty().ifBlank { "{}" })
                throw IllegalStateException(result.optString("error", "Call action failed"))
            }
        }
    }
}
