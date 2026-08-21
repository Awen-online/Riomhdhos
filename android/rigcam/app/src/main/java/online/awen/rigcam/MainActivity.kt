package online.awen.rigcam

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import online.awen.rigcam.databinding.ActivityMainBinding
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * A thin controller. The camera and the server live in [CamService] so they survive this
 * Activity being backgrounded, covered by another app, or torn down by the lockscreen -
 * all of which used to take the stream down with them.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var ui: ActivityMainBinding

    private val askPerms = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()) { granted ->
        if (granted[Manifest.permission.CAMERA] == true) startCam()
        else ui.status.text = getString(R.string.no_permission)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = ActivityMainBinding.inflate(layoutInflater)
        setContentView(ui.root)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val needed = mutableListOf(Manifest.permission.CAMERA)
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
        val ip = lanAddress() ?: "?.?.?.?"
        ui.status.text = getString(R.string.serving, ip, CamService.PORT)
    }

    /** See CamService.lanAddress - a phone can be on WiFi, tethering and a VPN at once. */
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
}
