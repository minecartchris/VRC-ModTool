package com.vrcmodsuite.rosteragent

import android.content.Context
import java.util.UUID

/**
 * Everything this agent remembers between launches.
 *
 * The roster key is a credential, so it lives in the app's private prefs and
 * is never shown in the UI — the pairing flow exists precisely so nobody has
 * to read one out or paste it into a headset keyboard.
 */
class Settings(context: Context) {

    private val prefs = context.getSharedPreferences("roster-agent", Context.MODE_PRIVATE)

    var server: String
        get() = prefs.getString("server", "") ?: ""
        set(value) = prefs.edit()
            .putString("server", value.trim().trimEnd('/')).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value).apply()

    /** Who the panel says this agent reports as, for the status line. */
    var pairedAs: String
        get() = prefs.getString("paired_as", "") ?: ""
        set(value) = prefs.edit().putString("paired_as", value).apply()

    var logFolder: String
        get() = prefs.getString("log_folder", "") ?: ""
        set(value) = prefs.edit().putString("log_folder", value).apply()

    /** What the moderator wants; the service may still be starting or stopped. */
    var wantRunning: Boolean
        get() = prefs.getBoolean("want_running", false)
        set(value) = prefs.edit().putBoolean("want_running", value).apply()

    var clientName: String
        get() = prefs.getString("client_name", "") ?: android.os.Build.MODEL ?: "Quest"
        set(value) = prefs.edit().putString("client_name", value).apply()

    /**
     * Stable id for this headset. The panel treats one client id as one
     * reporter, so it has to survive restarts — a new id every launch would
     * show up as a room full of ghost agents.
     */
    val clientId: String
        get() {
            prefs.getString("client_id", null)?.let { return it }
            val fresh = UUID.randomUUID().toString().replace("-", "").take(12)
            prefs.edit().putString("client_id", fresh).apply()
            return fresh
        }

    val ready: Boolean
        get() = server.isNotEmpty() && token.isNotEmpty() && logFolder.isNotEmpty()

    fun forget() {
        prefs.edit().remove("token").remove("paired_as").apply()
    }
}
