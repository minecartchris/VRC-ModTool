package com.vrcmodsuite.rosteragent

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.vrcmodsuite.rosteragent.databinding.ActivityMainBinding
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The setup panel: point it at the server, hand it the log folder, pair once,
 * then start reporting. After that it is only ever opened to check on things.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var ui: ActivityMainBinding
    private lateinit var settings: Settings
    private val onUi = Handler(Looper.getMainLooper())
    private var pairing: Thread? = null

    private val pickFolder = registerForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { uri: Uri? ->
        if (uri == null) return@registerForActivityResult
        // Persisted, or the grant dies with this activity and the service
        // wakes up to a folder it is no longer allowed to read.
        contentResolver.takePersistableUriPermission(
            uri, Intent.FLAG_GRANT_READ_URI_PERMISSION
        )
        settings.logFolder = uri.toString()
        render()
    }

    private val askNotifications = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { render() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = ActivityMainBinding.inflate(layoutInflater)
        setContentView(ui.root)
        settings = Settings(this)

        ui.server.setText(settings.server)
        ui.clientName.setText(settings.clientName)

        ui.chooseFolder.setOnClickListener { pickFolder.launch(logsHint()) }
        ui.pair.setOnClickListener { startPairing() }
        ui.unpair.setOnClickListener {
            RosterService.stop(this)
            settings.forget()
            settings.wantRunning = false
            say("Key forgotten on this headset. Revoke it in the panel too if it was lost.")
            render()
        }
        ui.toggle.setOnClickListener { toggle() }

        askForNotifications()
        tick()
    }

    /** Open the picker at Documents, where VRChat keeps its logs. */
    private fun logsHint(): Uri? = try {
        Uri.parse("content://com.android.externalstorage.documents/document/primary%3ADocuments")
    } catch (e: Exception) {
        null
    }

    private fun askForNotifications() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    private fun save() {
        settings.server = ui.server.text.toString()
        settings.clientName = ui.clientName.text.toString().ifBlank { Build.MODEL ?: "Quest" }
    }

    private fun toggle() {
        save()
        if (AgentStatus.running) {
            settings.wantRunning = false
            RosterService.stop(this)
            say("Stopped. Nothing is being sent.")
        } else {
            when {
                settings.server.isEmpty() -> return say("Put the panel's address in first.")
                settings.logFolder.isEmpty() -> return say("Choose VRChat's Logs folder first.")
                settings.token.isEmpty() -> return say("Pair with the panel first.")
            }
            settings.wantRunning = true
            RosterService.start(this)
            say("Reporting. You can close this and put the headset on.")
        }
        render()
    }

    private fun startPairing() {
        save()
        if (settings.server.isEmpty()) return say("Put the panel's address in first.")
        if (pairing?.isAlive == true) return say("Already waiting for that link to be opened.")

        say("Asking the panel for a link…")
        pairing = Thread {
            val api = Api(settings.server)
            try {
                val start = api.pairStart(settings.clientName)
                onUi.post {
                    ui.pairCode.text = getString(R.string.pair_code, start.code, start.url)
                    say("Open the link on any device where you are signed in to the panel.")
                    // The headset's browser is right here, so offer it — but
                    // the link works from a phone too, which is easier to type
                    // a password into than a floating keyboard.
                    try {
                        startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(start.url)))
                    } catch (e: Exception) {
                        // No browser: the code on screen is enough.
                    }
                }
                val deadline = System.currentTimeMillis() + start.expiresIn * 1000L
                while (System.currentTimeMillis() < deadline) {
                    Thread.sleep(3000)
                    val approved = api.pairPoll(start.code, start.secret) ?: continue
                    settings.token = approved.first
                    settings.pairedAs = approved.second
                    onUi.post {
                        ui.pairCode.text = ""
                        say("Paired as ${approved.second}. This headset reports as them.")
                        render()
                    }
                    return@Thread
                }
                onUi.post { say("That code expired. Ask for a new one.") }
            } catch (e: PairingGoneException) {
                onUi.post { say(e.message ?: "That code is finished.") }
            } catch (e: InterruptedException) {
                // Cancelled by leaving the screen; nothing to say.
            } catch (e: Exception) {
                onUi.post { say("Couldn't reach ${settings.server}: ${e.message}") }
            }
        }.also { it.isDaemon = true; it.start() }
    }

    private fun say(message: String) {
        ui.message.text = message
    }

    /** Repaint once a second: the service is the thing that knows anything. */
    private fun tick() {
        render()
        onUi.postDelayed({ tick() }, 1000)
    }

    private fun render() {
        ui.toggle.text = getString(
            if (AgentStatus.running) R.string.stop else R.string.start
        )
        ui.pairedAs.text = when {
            settings.pairedAs.isNotEmpty() ->
                getString(R.string.paired_as, settings.pairedAs)
            settings.token.isNotEmpty() -> getString(R.string.paired_unknown)
            else -> getString(R.string.not_paired)
        }
        ui.unpair.isEnabled = settings.token.isNotEmpty()
        ui.folder.text = if (settings.logFolder.isEmpty()) {
            getString(R.string.no_folder)
        } else {
            getString(R.string.folder_chosen, prettyFolder(settings.logFolder))
        }

        val lines = ArrayList<String>()
        lines += if (AgentStatus.running) "Reporting." else "Not reporting."
        if (AgentStatus.world.isNotEmpty()) {
            lines += "${AgentStatus.players} in ${AgentStatus.world}"
        }
        if (AgentStatus.lastSend > 0) {
            lines += "Last sent " + SimpleDateFormat("HH:mm:ss", Locale.US)
                .format(Date(AgentStatus.lastSend)) +
                if (AgentStatus.lastResult != "sent") " — ${AgentStatus.lastResult}" else ""
        }
        if (AgentStatus.errorsOnlyWarning) lines += getString(R.string.errors_only)
        if (AgentStatus.error.isNotEmpty()) lines += AgentStatus.error
        ui.status.text = lines.joinToString("\n")
    }

    private fun prettyFolder(uri: String): String =
        Uri.decode(uri).substringAfterLast(':').ifEmpty { uri }

    override fun onPause() {
        super.onPause()
        save()
    }

    override fun onDestroy() {
        if (isFinishing) pairing?.interrupt()
        super.onDestroy()
    }
}
