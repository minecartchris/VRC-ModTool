package com.vrcmodsuite.rosteragent

import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL

/** The panel revoked this headset's key, or never issued it. */
class RevokedException(message: String) : Exception(message)

/** The pairing code is finished — used, declined or expired. */
class PairingGoneException(message: String) : Exception(message)

data class PairStart(val code: String, val secret: String, val url: String, val expiresIn: Int)

/**
 * The three calls this agent makes, matching the desktop agent exactly so the
 * server needs no new endpoints: pair once, then post a roster on a timer.
 */
class Api(private val server: String) {

    fun pairStart(clientName: String): PairStart {
        val body = JSONObject().put("client_name", clientName)
        val json = post("/api/agent/pair/start", body, token = null)
        return PairStart(
            code = json.optString("code"),
            secret = json.optString("secret"),
            url = json.optString("url"),
            expiresIn = json.optInt("expires_in", 600)
        )
    }

    /** Null until a moderator opens the link; the name they paired as after. */
    fun pairPoll(code: String, secret: String): Pair<String, String>? {
        val body = JSONObject().put("code", code).put("secret", secret)
        val json = post("/api/agent/pair/poll", body, token = null)
        if (json.optString("status") != "approved") return null
        return json.optString("token") to json.optString("name")
    }

    /**
     * Send one roster. Returns the server's reason for discarding it, empty
     * when it was kept — an instance the group does not moderate comes back
     * as a normal answer rather than an error, and the agent should say so
     * instead of looking broken.
     */
    fun postRoster(token: String, clientId: String, clientName: String, snap: Snapshot): String {
        val players = JSONArray()
        for (p in snap.players) {
            players.put(JSONObject().put("name", p.name).put("user_id", p.userId))
        }
        val roster = JSONObject()
            .put("world_name", snap.worldName)
            .put("world_id", snap.worldId)
            .put("instance_id", snap.instanceId)
            .put("players", players)
        val body = JSONObject()
            .put("client_id", clientId)
            .put("client_name", clientName)
            .put("roster", roster)
        return post("/api/sync/roster", body, token).optString("ignored", "")
    }

    private fun post(path: String, body: JSONObject, token: String?): JSONObject {
        val conn = (URL(server + path).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = 15_000
            readTimeout = 20_000
            doOutput = true
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("Accept", "application/json")
            token?.let { setRequestProperty("X-Sync-Token", it) }
        }
        try {
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            val text = (if (code >= 400) conn.errorStream else conn.inputStream)
                ?.bufferedReader()?.use(BufferedReader::readText).orEmpty()

            when {
                code == 401 -> throw RevokedException(
                    "the panel rejected this headset's key — pair again"
                )
                code == 410 -> throw PairingGoneException(reason(text, "that code is done"))
                code == 404 && path.contains("pair") ->
                    throw PairingGoneException("no such pairing")
                code == 503 -> throw Exception("the server has the sync API turned off")
                code >= 400 -> throw Exception("server said $code: ${reason(text, "")}")
            }
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        } finally {
            conn.disconnect()
        }
    }

    private fun reason(text: String, fallback: String): String = try {
        JSONObject(text).optString("error", fallback)
    } catch (e: Exception) {
        fallback
    }
}
