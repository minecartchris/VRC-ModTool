package com.vrcmodsuite.rosteragent

import android.content.Context
import android.net.Uri
import androidx.documentfile.provider.DocumentFile
import java.io.InputStream

/** One person in the room. The id is what matters; the name is for reading. */
data class Player(val name: String, val userId: String)

/** What the panel is told: a room, and who is in it. */
data class Snapshot(
    val worldName: String,
    val worldId: String,
    val instanceId: String,
    val players: List<Player>
)

/**
 * The parsing, with no Android in it, so it can be tested on a desktop.
 *
 * Deliberately identical to the desktop agent's `vrc_log.py`: one format, one
 * set of bugs, and a fix in either place is obviously the same fix in the
 * other.
 */
class RoomState {

    companion object {
        private val ROOM = Regex("""\[Behaviour] Entering Room: (.+?)\s*$""")
        private val WORLD = Regex("""\[Behaviour] Joining (wrld_[0-9a-f-]+):(\S+)""")
        private val JOIN =
            Regex("""\[Behaviour] OnPlayerJoined (.+?)( \((usr_[0-9a-f-]+)\))?\s*$""")
        private val LEAVE =
            Regex("""\[Behaviour] OnPlayerLeft (.+?)( \((usr_[0-9a-f-]+)\))?\s*$""")

        fun key(p: Player) =
            if (p.userId.isNotEmpty()) p.userId else "name:${p.name.lowercase()}"
    }

    var worldName = ""; private set
    var worldId = ""; private set
    var instanceId = ""; private set
    private val players = LinkedHashMap<String, Player>()

    /** Lines only written when VRChat's logging is set to Full. */
    var behaviourLines = 0; private set

    fun clear() {
        worldName = ""; worldId = ""; instanceId = ""
        players.clear()
        behaviourLines = 0
    }

    fun apply(line: String) {
        if (!line.contains("[Behaviour]")) return
        behaviourLines++

        WORLD.find(line)?.let {
            val newWorld = it.groupValues[1]
            val newInstance = it.groupValues[2]
            if (newWorld != worldId || newInstance != instanceId) {
                // A different room: whoever was in the last one is not here.
                players.clear()
                worldName = ""
            }
            worldId = newWorld
            instanceId = newInstance
            return
        }
        ROOM.find(line)?.let { worldName = it.groupValues[1]; return }
        JOIN.find(line)?.let {
            val player = Player(it.groupValues[1], it.groupValues[3])
            players[key(player)] = player
            return
        }
        LEAVE.find(line)?.let {
            players.remove(key(Player(it.groupValues[1], it.groupValues[3])))
            return
        }
    }

    fun snapshot() = Snapshot(
        worldName, worldId, instanceId,
        players.values.sortedBy { it.name.lowercase() }
    )
}

/**
 * Follows VRChat's log on a Quest and keeps a live picture of the instance.
 *
 * VRChat writes its Quest logs to Documents/Logs in shared storage, which the
 * moderator hands over once through the system folder picker. That is the only
 * way in that does not need All-files access, and All-files access is a
 * permission the Horizon Store only grants file managers.
 *
 * This half is the bookkeeping — which file, how far in — and [RoomState] does
 * the reading of it.
 */
class LogTail(private val context: Context, private val folder: Uri) {

    companion object {
        /** Read this much of a log without one [Behaviour] line and the
         *  moderator is almost certainly still on "Errors Only". */
        private const val QUIET_BYTES = 400_000L
    }

    private var currentName = ""
    private var offset = 0L
    private var partial = ""
    private var bytesRead = 0L
    private val room = RoomState()

    /** True when there is a log growing but nothing VRChat only writes on Full. */
    val looksLikeErrorsOnly: Boolean
        get() = room.behaviourLines == 0 && bytesRead > QUIET_BYTES

    var lastError: String = ""
        private set

    /** The newest output_log_*.txt in the chosen folder, if there is one. */
    private fun newestLog(): DocumentFile? {
        val tree = DocumentFile.fromTreeUri(context, folder) ?: return null
        return tree.listFiles()
            .filter { it.isFile }
            .filter { (it.name ?: "").startsWith("output_log_") && (it.name ?: "").endsWith(".txt") }
            .maxByOrNull { it.lastModified() }
    }

    /** Read whatever is new. Returns the current picture of the room. */
    fun poll(): Snapshot {
        val file = newestLog()
        if (file == null) {
            lastError = "no output_log_*.txt in that folder — is it Documents/Logs?"
            return snapshot()
        }
        val name = file.name ?: ""
        if (name != currentName) {
            // VRChat restarted and started a new log. The old room is gone
            // with it, so this is a fresh read rather than a continuation.
            currentName = name
            offset = 0
            partial = ""
            bytesRead = 0
            room.clear()
        }

        val size = file.length()
        if (size < offset) {
            offset = 0            // truncated under us; start again
            partial = ""
        }
        if (size == offset) {
            lastError = ""
            return snapshot()
        }

        try {
            context.contentResolver.openInputStream(file.uri).use { raw ->
                if (raw == null) {
                    lastError = "could not open $name"
                    return snapshot()
                }
                skipExactly(raw, offset)
                val chunk = raw.readBytes()
                offset += chunk.size
                bytesRead += chunk.size
                consume(String(chunk, Charsets.UTF_8))
            }
            lastError = ""
        } catch (e: Exception) {
            lastError = "reading $name: ${e.message}"
        }
        return snapshot()
    }

    /** InputStream.skip may do less than asked; a short read here would
     *  silently re-parse lines and double up the roster. */
    private fun skipExactly(stream: InputStream, target: Long) {
        var left = target
        while (left > 0) {
            val moved = stream.skip(left)
            if (moved <= 0) {
                val one = stream.read()
                if (one < 0) return
                left -= 1
            } else {
                left -= moved
            }
        }
    }

    private fun consume(text: String) {
        val whole = partial + text
        val lines = whole.split('\n')
        // The last piece has no newline yet: it is half a line VRChat is
        // still writing, and parsing it would lose the rest of the name.
        partial = lines.last()
        for (line in lines.dropLast(1)) room.apply(line.trimEnd('\r'))
    }

    private fun snapshot() = room.snapshot()
}
