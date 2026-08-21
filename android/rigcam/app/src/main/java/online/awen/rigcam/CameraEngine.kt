package online.awen.rigcam

import android.content.Context
import android.hardware.camera2.CaptureRequest
import android.util.Size
import androidx.camera.camera2.interop.Camera2CameraControl
import androidx.camera.camera2.interop.CaptureRequestOptions
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.util.concurrent.Executors

/**
 * The camera, and every control the USB path could not give us.
 *
 * ⚠️ THIS IS THE WHOLE REASON THE APP EXISTS. Over USB with the stock `DeviceAsWebcam`, the
 * host can control NOTHING: `IAMCameraControl` reports zoom, focus, pan and tilt as
 * unsupported, and exposure/iris/brightness answer with ranges whose min equals max. See
 * `video/camctl.py`. Camera2 gives all of it back.
 *
 * The two that matter most for a two-camera show:
 *
 *   AE / AWB LOCK - two phones auto-exposing independently drift apart, so a cut between
 *                   them jumps in brightness and colour. Locking both after framing is what
 *                   makes them cut together. Impossible over UVC.
 *   ZOOM RATIO    - on the Pixel 8 the ultrawide is a physical sub-camera of the logical
 *                   back camera, reached by a zoom ratio BELOW 1.0. Measured `minZoomRatio`
 *                   is **0.549**, so that is how the 125.8° lens is selected; there is no
 *                   separate camera id to open.
 *
 * ⚠️ THERE IS NO ON-SCREEN PREVIEW AND NO MJPEG ANY MORE. CameraX allows one `Preview` use
 * case and it is spent rendering into the encoder's input surface - which is precisely what
 * took this from 9 fps to full rate. Frame the shot in OBS, not on the phone.
 */
@OptIn(ExperimentalCamera2Interop::class)
class CameraEngine(
    private val context: Context,
) : StreamServer.Controls {

    /** Set once by MainActivity. The server and the engine each need the other. */
    var server: StreamServer? = null

    private val executor = Executors.newSingleThreadExecutor()
    private var camera: Camera? = null
    private var encoder: H264Encoder? = null
    // Kept so a settings change that needs a new capture session can rebind itself.
    private var owner: LifecycleOwner? = null
    private var provider: ProcessCameraProvider? = null

    @Volatile private var targetSize = Size(1280, 720)
    @Volatile private var bitRate = 6_000_000
    @Volatile private var fps = 30
    @Volatile private var facing = CameraSelector.LENS_FACING_BACK
    @Volatile private var aeLock = false
    @Volatile private var awbLock = false
    @Volatile private var actualW = 0
    @Volatile private var actualH = 0

    fun start(owner: LifecycleOwner) {
        this.owner = owner
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            provider = future.get()
            bind(owner, future.get())
        }, ContextCompat.getMainExecutor(context))
    }

    /**
     * Re-open the capture session. Needed for anything the session is fixed at: which lens,
     * what resolution, what frame rate.
     *
     * ⚠️ MAIN THREAD ONLY - `bindToLifecycle` requires it. And ⚠️ this DROPS THE STREAM for
     * a moment and builds a new encoder, so it is a between-songs operation, not a live one.
     * Zoom, EV and the AE/AWB locks all apply without it and are the safe live controls.
     */
    private fun rebind() {
        val o = owner ?: return
        val p = provider ?: return
        ContextCompat.getMainExecutor(context).execute { bind(o, p) }
    }

    private fun bind(owner: LifecycleOwner, p: ProcessCameraProvider) {
        p.unbindAll()
        encoder?.release(); encoder = null

        val preview = Preview.Builder()
            .setResolutionSelector(
                ResolutionSelector.Builder()
                    .setResolutionStrategy(
                        ResolutionStrategy(targetSize,
                            ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER))
                    // Without this the device handed back 4:3 800x600 for a 720p request -
                    // both smaller than asked and the wrong shape for a 16:9 canvas.
                    .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
                    .build())
            .build()

        // ⚠️ BUILD THE ENCODER FROM request.resolution, NOT from targetSize. CameraX decides
        // the real resolution, and a MediaCodec input surface is fixed at the size it was
        // configured with - so configuring from a guess risks a mismatch that either fails to
        // bind or silently scales. Taking the size from the request makes that impossible.
        preview.setSurfaceProvider(executor) { request ->
            val r = request.resolution
            actualW = r.width
            actualH = r.height
            val enc = H264Encoder(r.width, r.height, fps, bitRate) { nal, key ->
                val s = server
                s?.publishNal(nal, key)
                s?.codecConfig = encoder?.codecConfig
            }
            encoder = enc
            request.provideSurface(enc.inputSurface, executor) { enc.release() }
        }

        camera = p.bindToLifecycle(
            owner, CameraSelector.Builder().requireLensFacing(facing).build(), preview)
        pushCaptureOptions()
    }

    /** AE/AWB lock live, without rebinding - rebinding would drop the stream. */
    private fun pushCaptureOptions() {
        val c = camera ?: return
        Camera2CameraControl.from(c.cameraControl).captureRequestOptions =
            CaptureRequestOptions.Builder()
                .setCaptureRequestOption(CaptureRequest.CONTROL_AE_LOCK, aeLock)
                .setCaptureRequestOption(CaptureRequest.CONTROL_AWB_LOCK, awbLock)
                .build()
    }

    override fun stateJson(): String {
        val c = camera
        val z = c?.cameraInfo?.zoomState?.value
        val ev = c?.cameraInfo?.exposureState
        val e = encoder
        return """
        {"streaming":${(e?.nalsOut ?: 0) > 0},
         "resolution":"${actualW}x${actualH}",
         "facing":"${if (facing == CameraSelector.LENS_FACING_BACK) "back" else "front"}",
         "requested":"${targetSize.width}x${targetSize.height}",
         "fps":$fps,
         "encoder":{"input":"surface",
                    "bitrateKbps":${bitRate / 1000},
                    "fps":$fps,
                    "nalsOut":${e?.nalsOut ?: 0},
                    "bytesOut":${e?.bytesOut ?: 0},
                    "csdBytes":${e?.codecConfig?.size ?: 0},
                    "lastError":"${e?.lastError ?: ""}"},
         "zoom":{"ratio":${z?.zoomRatio ?: 1.0},
                 "min":${z?.minZoomRatio ?: 1.0},
                 "max":${z?.maxZoomRatio ?: 1.0},
                 "linear":${z?.linearZoom ?: 0.0}},
         "ev":{"index":${ev?.exposureCompensationIndex ?: 0},
               "min":${ev?.exposureCompensationRange?.lower ?: 0},
               "max":${ev?.exposureCompensationRange?.upper ?: 0}},
         "aeLock":$aeLock,"awbLock":$awbLock,
         "torch":${c?.cameraInfo?.torchState?.value == 1}}
        """.trimIndent().replace("\n", "")
    }

    override fun apply(params: Map<String, String>): String {
        val c = camera ?: return """{"error":"camera not bound"}"""
        val done = mutableListOf<String>()

        params["zoom"]?.toFloatOrNull()?.let {
            c.cameraControl.setZoomRatio(it); done += "zoom=$it"
        }
        params["linearZoom"]?.toFloatOrNull()?.let {
            c.cameraControl.setLinearZoom(it); done += "linearZoom=$it"
        }
        params["ev"]?.toIntOrNull()?.let {
            c.cameraControl.setExposureCompensationIndex(it); done += "ev=$it"
        }
        params["torch"]?.toBooleanStrictOrNull()?.let {
            c.cameraControl.enableTorch(it); done += "torch=$it"
        }
        params["aeLock"]?.toBooleanStrictOrNull()?.let { aeLock = it; done += "aeLock=$it" }
        params["awbLock"]?.toBooleanStrictOrNull()?.let { awbLock = it; done += "awbLock=$it" }
        // ---- settings the capture session is fixed at: each forces a rebind ----
        var needRebind = false
        params["bitrate"]?.toIntOrNull()?.let {
            bitRate = it.coerceIn(500_000, 20_000_000); done += "bitrate=$bitRate"
            needRebind = true
        }
        params["fps"]?.toIntOrNull()?.let {
            fps = it.coerceIn(10, 60); done += "fps=$fps"; needRebind = true
        }
        params["facing"]?.let {
            val f = if (it.equals("front", true)) CameraSelector.LENS_FACING_FRONT
                    else CameraSelector.LENS_FACING_BACK
            if (f != facing) { facing = f; done += "facing=$it"; needRebind = true }
        }
        params["resolution"]?.let { r ->
            val m = Regex("""(\d+)\s*[xX]\s*(\d+)""").find(r)
            if (m != null) {
                targetSize = Size(m.groupValues[1].toInt(), m.groupValues[2].toInt())
                done += "resolution=${targetSize.width}x${targetSize.height}"
                needRebind = true
            }
        }
        if (done.any { it.startsWith("aeLock") || it.startsWith("awbLock") }) {
            pushCaptureOptions()
        }
        if (needRebind) rebind()
        return """{"applied":[${done.joinToString(",") { "\"$it\"" }}],"rebound":$needRebind}"""
    }
}
