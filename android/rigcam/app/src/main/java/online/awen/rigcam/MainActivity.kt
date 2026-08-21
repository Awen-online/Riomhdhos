package online.awen.rigcam

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.wifi.WifiManager
import android.os.Bundle
import android.os.PowerManager
import android.view.WindowManager
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import online.awen.rigcam.databinding.ActivityMainBinding
import java.net.Inet4Address
import java.net.NetworkInterface

class MainActivity : AppCompatActivity() {

    private lateinit var ui: ActivityMainBinding
    private lateinit var server: StreamServer
    private lateinit var engine: CameraEngine
    private var wifiLock: WifiManager.WifiLock? = null
    private var wakeLock: PowerManager.WakeLock? = null

    private val askCamera = registerForActivityResult(
        ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) startEverything() else ui.status.text = getString(R.string.no_permission)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = ActivityMainBinding.inflate(layoutInflater)
        setContentView(ui.root)

        // A camera that sleeps is not a camera. Keeping the screen on also sidesteps the
        // whole lockscreen class of problem that plagued the USB route.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED) {
            startEverything()
        } else {
            askCamera.launch(Manifest.permission.CAMERA)
        }
    }

    private fun startEverything() {
        acquireLocks()

        // The engine IS the control surface, so it can be handed to the server directly;
        // the server is then handed back so the encoder can publish into it.
        engine = CameraEngine(this)
        server = StreamServer(PORT, engine)
        engine.server = server

        // No PreviewView: the one Preview use case CameraX allows is spent feeding the
        // encoder surface. Framing happens in OBS.
        engine.start(this)
        server.start()

        val ip = lanAddress() ?: "?.?.?.?"
        ui.status.text = getString(R.string.serving, ip, PORT)
    }

    /**
     * ⚠️ MEASURED, NOT PRECAUTIONARY: on the Pixel 6 over WiFi, packet loss went from ~0%
     * to 37.8% as soon as the screen locked, because Android parks the WiFi radio in a
     * power-saving mode. WIFI_MODE_FULL_HIGH_PERF is what keeps a video stream usable.
     */
    private fun acquireLocks() {
        val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "rigcam:wifi")
            .apply { setReferenceCounted(false); acquire() }
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rigcam:cpu")
            .apply { setReferenceCounted(false); acquire() }
    }

    /**
     * The address a host on the LAN can actually reach.
     *
     * ⚠️ Do not use the first address found. This phone may be tethering, on WiFi, and on a
     * VPN at once; the desktop hit exactly this and reported a ProtonVPN tunnel address as
     * its own. Prefer a real site-local IPv4 on a non-virtual, non-loopback interface.
     */
    private fun lanAddress(): String? {
        val candidates = mutableListOf<Pair<Int, String>>()
        for (nif in NetworkInterface.getNetworkInterfaces()) {
            if (!nif.isUp || nif.isLoopback || nif.isVirtual) continue
            val name = nif.name.lowercase()
            val rank = when {
                name.startsWith("wlan") -> 0        // WiFi: what we actually want
                name.startsWith("rndis") || name.startsWith("ncm") -> 1   // USB tether
                name.startsWith("tun") || name.startsWith("ppp") -> 9     // VPN: last
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

    override fun onDestroy() {
        super.onDestroy()
        if (::server.isInitialized) server.stop()
        try { wifiLock?.release() } catch (_: Exception) {}
        try { wakeLock?.release() } catch (_: Exception) {}
    }

    companion object {
        const val PORT = 8090
    }
}
