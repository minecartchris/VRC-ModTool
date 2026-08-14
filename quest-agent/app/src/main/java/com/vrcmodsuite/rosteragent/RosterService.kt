package com.vrcmodsuite.rosteragent

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.IBinder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * What the UI shows. Kept here rather than passed around, because the panel
 * that displays it is closed most of the time — the service is the thing that
 * actually knows anything.
 */
object AgentStatus {
    @Volatile var running = false
    @Volatile var world = ""
    @Volatile var players = 0
    @Volatile var lastSend = 0L
    @Volatile var lastResult = ""
    @Volatile var error = ""
    @Volatile var errorsOnlyWarning = false
}

/**
 * Reads the log and posts the roster, for as long as the moderator is in
 * VRChat.
 *
 * A foreground service because that is the only kind Android keeps running
 * once its app is off screen, and off screen is the normal case here: the
 * moderator is inside VRChat, not looking at this panel. `dataSync` is the
 * service type that describes it — the work is uploading a small amount of
 * data on a timer.
 */
class RosterService : Service() {

    companion object {
        private const val CHANNEL = "roster"
        private const val NOTE_ID = 1
        private const val POLL_MS = 10_000L
        const val ACTION_STOP = "com.vrcmodsuite.rosteragent.STOP"

        fun start(context: Context) {
            val intent = Intent(context, RosterService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, RosterService::class.java))
        }
    }

    private var worker: Thread? = null
    @Volatile private var stopping = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            Settings(this).wantRunning = false
            stopSelf()
            return START_NOT_STICKY
        }
        startForeground(NOTE_ID, notification("Starting up"))
        if (worker == null) {
            stopping = false
            worker = Thread(::loop, "roster-agent").apply { isDaemon = true; start() }
        }
        AgentStatus.running = true
        // Restarted by the system after being killed: the moderator is
        // probably still in VRChat, and the roster should carry on.
        return START_STICKY
    }

    override fun onDestroy() {
        stopping = true
        worker?.interrupt()
        worker = null
        AgentStatus.running = false
        super.onDestroy()
    }

    private fun loop() {
        val settings = Settings(this)
        val tail = LogTail(this, Uri.parse(settings.logFolder))
        val api = Api(settings.server)

        while (!stopping) {
            try {
                val snap = tail.poll()
                AgentStatus.world = snap.worldName.ifEmpty { snap.worldId }
                AgentStatus.players = snap.players.size
                AgentStatus.errorsOnlyWarning = tail.looksLikeErrorsOnly

                if (tail.lastError.isNotEmpty()) {
                    AgentStatus.error = tail.lastError
                } else if (snap.worldId.isEmpty() && snap.players.isEmpty()) {
                    // Nothing to report yet rather than an error: VRChat may
                    // simply not be running.
                    AgentStatus.error = ""
                    AgentStatus.lastResult = "waiting for VRChat"
                } else {
                    val ignored = api.postRoster(
                        settings.token, settings.clientId, settings.clientName, snap
                    )
                    AgentStatus.lastSend = System.currentTimeMillis()
                    AgentStatus.lastResult = ignored.ifEmpty { "sent" }
                    AgentStatus.error = ""
                }
            } catch (e: RevokedException) {
                AgentStatus.error = e.message ?: "key rejected"
                settings.forget()
                settings.wantRunning = false
                notify(notification(AgentStatus.error))
                stopSelf()
                return
            } catch (e: InterruptedException) {
                return
            } catch (e: Exception) {
                AgentStatus.error = e.message ?: e.javaClass.simpleName
            }

            notify(notification(summary()))
            try {
                Thread.sleep(POLL_MS)
            } catch (e: InterruptedException) {
                return
            }
        }
    }

    private fun summary(): String = when {
        AgentStatus.error.isNotEmpty() -> AgentStatus.error
        AgentStatus.errorsOnlyWarning ->
            "No join lines yet — turn logging to Full in VRChat's Debug menu"
        AgentStatus.world.isEmpty() -> "Waiting for VRChat"
        else -> "${AgentStatus.players} in ${AgentStatus.world} · " +
            SimpleDateFormat("HH:mm:ss", Locale.US).format(Date(AgentStatus.lastSend))
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL, "Roster reporting", NotificationManager.IMPORTANCE_LOW
        ).apply { description = "Shown while the roster is being reported." }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL)
        } else {
            @Suppress("DEPRECATION") Notification.Builder(this)
        }
        return builder
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    private fun notify(note: Notification) {
        try {
            getSystemService(NotificationManager::class.java).notify(NOTE_ID, note)
        } catch (e: SecurityException) {
            // Notifications refused. The service keeps running; the moderator
            // just has to open the panel to see anything.
        }
    }
}
