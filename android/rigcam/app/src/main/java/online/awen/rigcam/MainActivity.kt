package online.awen.rigcam

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.WindowManager
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import online.awen.rigcam.databinding.ActivityMainBinding
import org.json.JSONObject

/**
 * A thin controller. The camera and the server live in [CamService] so they survive this
 * Activity being backgrounded, covered by another app, or torn down by the lockscreen -
 * all of which used to take the stream down with them.
 *
 * Its other job is the on-screen readout. The handset is usually the only thing you can
 * see from where it is standing - the dashboard is on a machine in another room - so it
 * has to answer, without being touched: is anything taking this picture, is the camera
 * awake, what is it set to, and where should the desk be pulling from.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var ui: ActivityMainBinding

    private val askPerms = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()) { granted ->
        if (granted[Manifest.permission.CAMERA] == true) startCam()
        else {
            ui.state.text = getString(R.string.no_permission)
            ui.state.setTextColor(BAD)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = ActivityMainBinding.inflate(layoutInflater)
        setContentView(ui.root)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val needed = mutableListOf(Manifest.permission.CAMERA)
        // ⚠️ ASKED FOR, NEVER REQUIRED. Audio is opt-in and off by default, so a denial
        // here must not stop the camera starting - see the `granted[CAMERA]` check above,
        // which is the only permission `startCam` waits on.
        needed += Manifest.permission.RECORD_AUDIO
        // ⚠️ Without POST_NOTIFICATIONS on Android 13+ the foreground-service notification is
        // suppressed. The service still runs, but there is then no way to see that it is
        // running or to stop it - a silent background process holding the camera.
        if (Build.VERSION.SDK_INT >= 33) needed += Manifest.permission.POST_NOTIFICATIONS

        val missing = needed.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) startCam() else askPerms.launch(missing.toTypedArray())
    }

    private fun startCam() {
        ContextCompat.startForegroundService(this, Intent(this, CamService::class.java))
    }

    // ⚠️ THE TICKER IS TIED TO VISIBILITY, NOT TO THE SERVICE. The service is built to run
    // for hours with no Activity at all; a poll that kept running in the background would
    // spend battery redrawing a screen nobody is looking at, on the one device in this rig
    // that is always on its own power budget.
    private val ticker = Handler(Looper.getMainLooper())
    private val refresh = object : Runnable {
        override fun run() {
            draw()
            ticker.postDelayed(this, 1000)
        }
    }

    override fun onResume() {
        super.onResume()
        ticker.post(refresh)
    }

    override fun onPause() {
        super.onPause()
        ticker.removeCallbacks(refresh)
    }

    private fun draw() {
        val svc = CamService.LIVE
        if (svc == null) {
            ui.state.text = getString(R.string.starting)
            ui.state.setTextColor(WARN)
            ui.detail.text = ""
            return
        }
        val s = try { JSONObject(svc.stateJson()) } catch (_: Exception) { JSONObject() }

        val dormant = s.optBoolean("dormant", false)
        val clients = s.optJSONObject("clients")
        val video = clients?.optInt("video") ?: 0
        val mjpeg = clients?.optInt("mjpeg") ?: 0
        val audioClients = clients?.optInt("audio") ?: 0

        // ⚠️ THREE STATES, NOT TWO, and the middle one is the one worth having. A camera
        // that is awake with nobody pulling is not an error and not success - it is the
        // shape you are in when the bridge has died at the other end, and it used to be
        // indistinguishable from feeding because the preview looks identical either way.
        when {
            dormant -> {
                ui.state.text = "ASLEEP"
                ui.state.setTextColor(WARN)
            }
            video > 0 -> {
                ui.state.text = if (video == 1) "FEEDING" else "FEEDING ×$video"
                ui.state.setTextColor(OK)
            }
            else -> {
                ui.state.text = "IDLE · nobody pulling"
                ui.state.setTextColor(WARN)
            }
        }

        val lines = mutableListOf<String>()
        val addr = svc.address()
        lines += if (addr != null) "http://$addr:${CamService.PORT}"
                 else "USB only · adb forward to :${CamService.PORT}"

        if (dormant) {
            lines += "camera released to save power"
            lines += "/api/wake to bring it back"
        } else {
            val enc = s.optJSONObject("encoder")
            val mbps = (enc?.optInt("bitrateKbps") ?: 0) / 1000.0
            lines += "${s.optString("resolution", "?")} · ${s.optInt("fps")} fps · " +
                     "${trim(mbps)} Mb/s"

            val zoom = s.optJSONObject("zoom")?.optDouble("ratio") ?: 1.0
            lines += "${s.optString("facing", "?")} · zoom ${trim(zoom)}× · " +
                     "EV ${s.optJSONObject("ev")?.optInt("index") ?: 0}"

            // The three that decide whether the two cameras match. Named the way the
            // dashboard names them, so a mismatch is obvious side by side.
            val m = s.optJSONObject("manual")
            val f = s.optJSONObject("focus")
            lines += (if (m?.optBoolean("exposure") == true)
                          "ISO ${m.optInt("iso")} · ${shutter(m.optLong("shutterNs"))}"
                      else "auto exposure") +
                     " · " + (if (m?.optBoolean("wb") == true) "manual WB" else "auto WB") +
                     " · " + (if (f?.optBoolean("manual") == true) "manual focus" else "AF")

            val locks = mutableListOf<String>()
            if (s.optBoolean("aeLock")) locks += "AE lock"
            if (s.optBoolean("awbLock")) locks += "AWB lock"
            if (s.optBoolean("stabilize")) locks += "stabilised"
            if (s.optBoolean("torch")) locks += "torch"
            if (locks.isNotEmpty()) lines += locks.joinToString(" · ")

            val err = enc?.optString("lastError").orEmpty()
            if (err.isNotEmpty()) lines += "encoder: $err"
        }

        // Audio is independent of the camera and stays visible while dormant - that
        // combination is the room-recording shape, and it is not obvious from the picture.
        val audio = s.optJSONObject("audio")
        if (audio != null && audio.optBoolean("on")) {
            lines += "MIC LIVE · ${audio.optInt("sampleRate") / 1000} kHz · $audioClients listening"
        }
        if (mjpeg > 0) lines += "$mjpeg browser preview(s)"

        ui.detail.text = lines.joinToString("\n")
    }

    /** 1.0 -> "1", 0.8 -> "0.8". Trailing ".0" on a zoom ratio reads as false precision. */
    private fun trim(v: Double): String =
        if (v == v.toLong().toDouble()) v.toLong().toString() else String.format("%.2f", v)

    /** Nanoseconds to the 1/x every photographer actually thinks in. */
    private fun shutter(ns: Long): String =
        if (ns <= 0) "?" else "1/${Math.round(1_000_000_000.0 / ns)}"

    private companion object {
        // Matching the dashboard's palette, so the same state is the same colour in both.
        val OK = Color.parseColor("#5FB87A")
        val WARN = Color.parseColor("#E0A458")
        val BAD = Color.parseColor("#E0645F")
    }
}
