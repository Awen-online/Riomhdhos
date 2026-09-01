package online.awen.rigcam

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LifecycleService
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * The camera and its server, living in a foreground service instead of an Activity.
 *
 * ⚠️ WHY THIS EXISTS - IT IS THE DIFFERENCE BETWEEN A DEMO AND A RIG. Everything used to
 * hang off MainActivity, so the camera and the HTTP server died the moment the app lost
 * focus. That happened twice in one evening: once when the phone locked, and once when the
 * dashboard's own wired-camera control launched DeviceAsWebcam over the top of it. A camera
 * that stops when you look at something else cannot be used in a performance.
 *
 * A Service is not a LifecycleOwner, and CameraX's `bindToLifecycle` requires one - hence
 * `LifecycleService`, which supplies exactly that and nothing else.
 *
 * ⚠️ `foregroundServiceType="camera"` in the manifest is MANDATORY from Android 14, and the
 * matching FOREGROUND_SERVICE_CAMERA permission with it. Without them the service is killed
 * at startForeground with a SecurityException rather than failing quietly later.
 */
class CamService : LifecycleService() {

    private lateinit var server: StreamServer
    private lateinit var engine: CameraEngine
    private lateinit var mic: MicRecorder
    // Whether each capture is live. The WiFi lock follows "any stream", not "the camera".
    @Volatile private var camLive = true
    @Volatile private var micLive = false
    @Volatile private var notifText = "starting…"
    private var wifiLock: WifiManager.WifiLock? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        // ⚠️ NAME THE TYPE, DO NOT LET ANDROID INFER IT. The no-type overload asserts EVERY
        // foregroundServiceType in the manifest, and the manifest now declares
        // `camera|microphone`. On a phone where RECORD_AUDIO was never granted that throws
        // and the service dies at startup - taking the CAMERA down too, over a microphone
        // nobody asked for. startForeground accepts a SUBSET, so claim camera alone here
        // and add microphone only once audio is actually running.
        startForeground(NOTIF_ID, notification(notifText),
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA)
        acquireLocks()

        engine = CameraEngine(this)
        server = StreamServer(PORT, engine)
        engine.server = server
        mic = MicRecorder(this) { block -> server.publishAudio(block) }
        engine.mic = mic
        mic.onStateChange = { on ->
            micLive = on
            // The FGS type has to follow the microphone, or Android 14 kills the service
            // the first time it notices audio running under a camera-only declaration.
            updateForegroundType()
            syncLocks()
            notify(if (on) "$notifText · MIC LIVE" else notifText)
        }
        // ⚠️ THE WIFI LOCK IS THE ONLY LOCK SLEEP DROPS. WIFI_MODE_FULL_HIGH_PERF exists to
        // keep a video stream usable (packet loss went 0% -> 37.8% without it once the
        // screen locked) - with no stream it buys nothing and costs radio power, so it goes
        // and comes back with the camera.
        //
        // ⚠️ THE PARTIAL WAKE LOCK STAYS, DELIBERATELY. It is what guarantees the HTTP
        // server still answers, and on the WiFi phone /api/wake is the ONLY way back -
        // there is no adb to fall back on. A sleep that might not wake is worse than a
        // sleep that saves slightly less.
        engine.onDormancy = { asleep ->
            camLive = !asleep
            syncLocks()
            notifText = if (asleep) "asleep - /api/wake to bring the camera back"
                        else "http://${lanAddress() ?: "?"}:$PORT/stream.h264"
            notify(if (micLive) "$notifText · MIC LIVE" else notifText)
        }
        engine.start(this)          // LifecycleService IS the LifecycleOwner
        server.start()
        RUNNING = true

        notifText = "http://${lanAddress() ?: "?"}:$PORT/stream.h264"
        notify(notifText)
    }

    /**
     * ⚠️ THE LOCK FOLLOWS ANY STREAM, NOT THE CAMERA. It used to be released whenever the
     * camera went dormant, which was right when dormant meant nothing was being sent. A
     * dormant camera STREAMING AUDIO over WiFi is a stream, and without the high-perf lock
     * it walks straight into the measured 37.8% packet loss that arrives the moment the
     * screen locks - silently, because a lossy PCM stream just sounds bad.
     */
    private fun syncLocks() {
        try {
            if (camLive || micLive) wifiLock?.acquire() else wifiLock?.release()
        } catch (_: Exception) {}
    }

    private fun updateForegroundType() {
        var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
        if (micLive && mic.hasPermission()) {
            type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        }
        try {
            startForeground(NOTIF_ID, notification(notifText), type)
        } catch (e: Exception) {
            // Never let this kill the service: the camera is the thing worth protecting.
            android.util.Log.e("RigCam", "startForeground type change failed", e)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        // ⚠️ START_STICKY: if Android kills this under memory pressure mid-show, it comes
        // back by itself. That is the entire point of moving out of the Activity.
        return START_STICKY
    }

    override fun onDestroy() {
        RUNNING = false
        if (::mic.isInitialized) mic.stop()
        if (::server.isInitialized) server.stop()
        try { wifiLock?.release() } catch (_: Exception) {}
        try { wakeLock?.release() } catch (_: Exception) {}
        super.onDestroy()
    }

    /**
     * ⚠️ MEASURED, NOT PRECAUTIONARY: on the Pixel 6 over WiFi, packet loss went from ~0%
     * to 37.8% as soon as the screen locked, because Android parks the WiFi radio to save
     * power. WIFI_MODE_FULL_HIGH_PERF is what keeps a video stream usable.
     */
    private fun acquireLocks() {
        val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "rigcam:wifi")
            .apply { setReferenceCounted(false); acquire() }
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rigcam:cpu")
            .apply { setReferenceCounted(false); acquire() }
    }

    private fun notify(text: String) {
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
            .notify(NOTIF_ID, notification(text))
    }

    private fun notification(text: String): Notification {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL, "RigCam", NotificationManager.IMPORTANCE_LOW)
                    .apply { setShowBadge(false) })
        }
        val stop = PendingIntent.getService(
            this, 1, Intent(this, CamService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle("RigCam")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.presence_video_online)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .addAction(0, "Stop", stop)
            .build()
    }

    /**
     * ⚠️ Do not use the first address found. A phone can be tethering, on WiFi and on a VPN
     * at once; the desktop hit exactly this and reported a VPN tunnel address as its own.
     */
    private fun lanAddress(): String? {
        val candidates = mutableListOf<Pair<Int, String>>()
        for (nif in NetworkInterface.getNetworkInterfaces()) {
            if (!nif.isUp || nif.isLoopback || nif.isVirtual) continue
            val name = nif.name.lowercase()
            val rank = when {
                name.startsWith("wlan") -> 0
                name.startsWith("rndis") || name.startsWith("ncm") -> 1
                name.startsWith("tun") || name.startsWith("ppp") -> 9
                else -> 5
            }
            for (addr in nif.inetAddresses) {
                if (addr is Inet4Address && !addr.isLoopbackAddress) {
                    candidates += rank to addr.hostAddress.orEmpty()
                }
            }
        }
        return candidates.minByOrNull { it.first }?.second
    }

    companion object {
        const val PORT = 8090
        const val CHANNEL = "rigcam"
        const val NOTIF_ID = 42
        const val ACTION_STOP = "online.awen.rigcam.STOP"
        @Volatile var RUNNING = false
    }
}
