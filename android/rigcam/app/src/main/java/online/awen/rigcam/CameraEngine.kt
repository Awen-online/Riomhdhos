package online.awen.rigcam

import android.content.Context
import android.graphics.Rect
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.CaptureResult
import android.hardware.camera2.TotalCaptureResult
import android.hardware.camera2.params.ColorSpaceTransform
import android.hardware.camera2.params.RggbChannelVector
import android.util.Size
import androidx.camera.camera2.interop.Camera2CameraControl
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.CaptureRequestOptions
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.Preview
import androidx.camera.core.SurfaceOrientedMeteringPointFactory
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

    /**
     * Camera and encoder deliberately released, HTTP server still listening. NOT an error
     * state - `stateJson` publishes it so the dashboard can tell "asleep on purpose" from
     * "the stream broke", which look identical from outside otherwise.
     */
    @Volatile private var dormant = false
    fun isDormant() = dormant

    /** CamService hangs the WiFi lock off this; see `sleep()`. true = going to sleep. */
    var onDormancy: ((Boolean) -> Unit)? = null

    private val executor = Executors.newSingleThreadExecutor()
    private var camera: Camera? = null
    private var encoder: H264Encoder? = null
    // Kept so a settings change that needs a new capture session can rebind itself.
    private var owner: LifecycleOwner? = null
    private var provider: ProcessCameraProvider? = null

    // ⚠️ 1080p is the DEFAULT, not just a setting the desktop pushes. This app resets to
    // its defaults on every restart, so a 720p default meant the rig quietly came back at
    // 720p after any phone reboot - and nobody notices a resolution drop the way they
    // notice a black source. Measured at 1920x1080: 27.8 fps at 16 Mbps against 27.6 at
    // 720p, so the pixels are close to free here - the encoder renders from a Surface and
    // never touches them on the CPU.
    @Volatile private var targetSize = Size(1920, 1080)
    // ⚠️ 16 Mbps, not the 6 that was a guess. MEASURED at 1920x1080: 6 Mbps produced
    // 6.5 Mbps actual at 27.4 fps, 16 Mbps produced 17.4 Mbps at 27.8 fps - so on a LAN the
    // extra bitrate costs nothing in frame rate and is pure detail. Lower it only if this
    // ever runs over something thinner than local WiFi.
    @Volatile private var bitRate = 16_000_000
    @Volatile private var fps = 30
    @Volatile private var facing = CameraSelector.LENS_FACING_BACK
    @Volatile private var aeLock = false
    @Volatile private var awbLock = false
    // WARNING: THE POINT OF MANUAL IS THAT BOTH PHONES CAN BE GIVEN THE SAME NUMBERS.
    // Locking auto-exposure only freezes whatever each camera happened to land on, so the
    // two still start from different places - measured p50 152 against 98, a visible jump
    // on a cut. Setting ISO, shutter and white-balance gains explicitly makes them match by
    // construction, and they cannot drift apart mid-set.
    @Volatile private var manualExposure = false
    @Volatile private var iso = 400
    @Volatile private var shutterNs = 16666666L
    @Volatile private var manualWb = false
    @Volatile private var wbR = 1.8f
    @Volatile private var wbG = 1.0f
    @Volatile private var wbB = 1.9f
    @Volatile private var faceTrack = false
    @Volatile private var stabilize = false
    @Volatile private var facesSeen = 0
    @Volatile private var lastMeterMs = 0L
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
        // ⚠️ A settings change must not wake a sleeping camera. Half of /api/set rebinds
        // (resolution, lens, fps), so without this a stray dashboard poll would quietly
        // undo a sleep and nobody would know until the battery was flat. Values still
        // record themselves in the fields above; wake() binds with whatever is current.
        if (dormant) return
        val o = owner ?: return
        val p = provider ?: return
        ContextCompat.getMainExecutor(context).execute { bind(o, p) }
    }

    private fun bind(owner: LifecycleOwner, p: ProcessCameraProvider) {
        p.unbindAll()
        // ⚠️ Disconnect viewers BEFORE the new encoder exists. They are decoding a stream
        // whose geometry is about to change, and an elementary stream cannot say so.
        server?.dropNalClients()
        encoder?.release(); encoder = null

        val previewBuilder = Preview.Builder()
        // The session capture callback is the only route from CameraX to CaptureResult -
        // and therefore to STATISTICS_FACES.
        Camera2Interop.Extender(previewBuilder).setSessionCaptureCallback(
            object : CameraCaptureSession.CaptureCallback() {
                override fun onCaptureCompleted(session: CameraCaptureSession,
                                                request: CaptureRequest,
                                                result: TotalCaptureResult) {
                    try { meterOnFace(result) } catch (_: Exception) {}
                }
            })
        val preview = previewBuilder
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
            val enc = H264Encoder(r.width, r.height, fps, bitRate) { nal, key, ptsUs ->
                val s = server
                // ⚠️ Publish the codec config BEFORE the frame, not after: the muxer needs
                // SPS/PPS in hand to put them ahead of the very first keyframe.
                s?.codecConfig = encoder?.codecConfig
                s?.publishNal(nal, key, ptsUs)
            }
            encoder = enc
            request.provideSurface(enc.inputSurface, executor) { enc.release() }
        }

        camera = p.bindToLifecycle(
            owner, CameraSelector.Builder().requireLensFacing(facing).build(), preview)
        pushCaptureOptions()
    }

    /** Everything that can change without rebinding - rebinding would drop the stream. */
    private fun pushCaptureOptions() {
        val c = camera ?: return
        val b = CaptureRequestOptions.Builder()
            .setCaptureRequestOption(CaptureRequest.CONTROL_AE_LOCK, aeLock)
            .setCaptureRequestOption(CaptureRequest.CONTROL_AWB_LOCK, awbLock)

        if (manualExposure) {
            // WARNING: AE must be OFF or the sensor keys are ignored - the camera keeps
            // driving exposure itself and the values silently do nothing.
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AE_MODE,
                                      CaptureRequest.CONTROL_AE_MODE_OFF)
            b.setCaptureRequestOption(CaptureRequest.SENSOR_SENSITIVITY, iso)
            b.setCaptureRequestOption(CaptureRequest.SENSOR_EXPOSURE_TIME, shutterNs)
        } else {
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AE_MODE,
                                      CaptureRequest.CONTROL_AE_MODE_ON)
        }

        if (manualWb) {
            // Same rule: AWB off, and COLOR_CORRECTION_MODE must be TRANSFORM_MATRIX or the
            // gains are ignored.
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AWB_MODE,
                                      CaptureRequest.CONTROL_AWB_MODE_OFF)
            b.setCaptureRequestOption(CaptureRequest.COLOR_CORRECTION_MODE,
                                      CaptureRequest.COLOR_CORRECTION_MODE_TRANSFORM_MATRIX)
            b.setCaptureRequestOption(CaptureRequest.COLOR_CORRECTION_GAINS,
                                      RggbChannelVector(wbR, wbG, wbG, wbB))
            // WARNING: TRANSFORM_MATRIX MODE MEANS THE HAL USES COLOR_CORRECTION_TRANSFORM, AND
            // IT IS NOT OPTIONAL. Setting the mode and the gains but not the matrix leaves it
            // all zeroes, which is singular - measured on the Pixel 6, the pipeline rejected
            // EVERY frame with "Color correction matrix is NOT invertible!" and the stream
            // stopped dead while /api/set still cheerfully reported success. Identity here, so
            // the gains above are the only thing colouring the picture.
            b.setCaptureRequestOption(CaptureRequest.COLOR_CORRECTION_TRANSFORM, IDENTITY_CCM)
        } else {
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AWB_MODE,
                                      CaptureRequest.CONTROL_AWB_MODE_AUTO)
        }

        b.setCaptureRequestOption(CaptureRequest.STATISTICS_FACE_DETECT_MODE,
            if (faceTrack) CaptureRequest.STATISTICS_FACE_DETECT_MODE_SIMPLE
            else CaptureRequest.STATISTICS_FACE_DETECT_MODE_OFF)

        b.setCaptureRequestOption(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE,
            if (stabilize) CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE_ON
            else CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE_OFF)

        Camera2CameraControl.from(c.cameraControl).captureRequestOptions = b.build()
    }

    /**
     * Point focus and exposure at the largest face the ISP reports.
     *
     * WARNING: THIS IS FREE AND ON-DEVICE. An earlier answer said face tracking was not
     * available - true of OBS PLUGINS, and wrong about the phone: both cameras report
     * availableFaceDetectModes [0,1,2], so the ISP finds faces itself at no cost here and
     * none at all on the desktop.
     */
    private fun meterOnFace(result: CaptureResult) {
        if (!faceTrack) return
        val faces = result.get(CaptureResult.STATISTICS_FACES) ?: return
        facesSeen = faces.size
        val face = faces.maxByOrNull { it.bounds.width() * it.bounds.height() } ?: return
        val now = System.currentTimeMillis()
        // WARNING: throttled hard. Re-metering every frame makes the camera hunt
        // continuously and the picture breathe - worse than not tracking at all.
        if (now - lastMeterMs < 1200) return
        lastMeterMs = now

        val c = camera ?: return
        val active: Rect = Camera2CameraInfo.from(c.cameraInfo)
            .getCameraCharacteristic(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
            ?: return
        val cx = face.bounds.exactCenterX() / active.width().toFloat()
        val cy = face.bounds.exactCenterY() / active.height().toFloat()
        val pt = SurfaceOrientedMeteringPointFactory(1f, 1f).createPoint(cx, cy)
        try {
            c.cameraControl.startFocusAndMetering(
                FocusMeteringAction.Builder(pt,
                    FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE)
                    .disableAutoCancel()
                    .build())
        } catch (_: Exception) {
        }
    }

    /** What the sensor will actually accept - read from the device, never assumed. */
    private fun isoRange(): android.util.Range<Int>? = try {
        Camera2CameraInfo.from(camera!!.cameraInfo)
            .getCameraCharacteristic(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE)
    } catch (e: Exception) { null }

    private fun shutterRange(): android.util.Range<Long>? = try {
        Camera2CameraInfo.from(camera!!.cameraInfo)
            .getCameraCharacteristic(CameraCharacteristics.SENSOR_INFO_EXPOSURE_TIME_RANGE)
    } catch (e: Exception) { null }

    private companion object {
        /** 3x3 identity as numerator/denominator pairs, row-major. */
        val IDENTITY_CCM = ColorSpaceTransform(
            intArrayOf(1, 1, 0, 1, 0, 1,
                       0, 1, 1, 1, 0, 1,
                       0, 1, 0, 1, 1, 1))
    }

    /**
     * Let go of the camera and the encoder. Everything else stays up.
     *
     * ⚠️ MAIN THREAD. `unbindAll` has the same requirement as `bindToLifecycle`, and this
     * is called from an HTTP worker thread, so it has to be posted rather than run here.
     * The reply goes back immediately - the caller is told what was ASKED for, and
     * `/api/state` is where they read what happened.
     */
    override fun sleep(): String {
        if (dormant) return """{"dormant":true,"note":"already asleep"}"""
        dormant = true
        val p = provider
        ContextCompat.getMainExecutor(context).execute {
            // Viewers first: they are decoding a stream that is about to stop existing, and
            // an elementary stream has no way to say so. Same reason as in bind().
            server?.dropNalClients()
            try { p?.unbindAll() } catch (_: Exception) {}
            encoder?.release(); encoder = null
            camera = null
            actualW = 0; actualH = 0
        }
        onDormancy?.invoke(true)
        return """{"dormant":true}"""
    }

    override fun wake(): String {
        if (!dormant) return """{"dormant":false,"note":"already awake"}"""
        // Locks back BEFORE the camera opens, so the first frames are not the ones sent
        // through a power-saving radio.
        onDormancy?.invoke(false)
        dormant = false
        rebind()
        return """{"dormant":false}"""
    }

    override fun stateJson(): String {
        val c = camera
        val z = c?.cameraInfo?.zoomState?.value
        val ev = c?.cameraInfo?.exposureState
        val e = encoder
        return """
        {"streaming":${(e?.nalsOut ?: 0) > 0},
         "dormant":$dormant,
         "resolution":"${actualW}x${actualH}",
         "facing":"${if (facing == CameraSelector.LENS_FACING_BACK) "back" else "front"}",
         "requested":"${targetSize.width}x${targetSize.height}",
         "fps":$fps,
         "encoder":{"input":"surface",
                    "lowLatency":${encoder?.lowLatency ?: false},
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
         "manual":{"exposure":$manualExposure,"iso":$iso,"shutterNs":$shutterNs,
                   "wb":$manualWb,"wbR":$wbR,"wbG":$wbG,"wbB":$wbB,
                   "isoMin":${isoRange()?.lower ?: 0},"isoMax":${isoRange()?.upper ?: 0},
                   "shutterMinNs":${shutterRange()?.lower ?: 0},
                   "shutterMaxNs":${shutterRange()?.upper ?: 0}},
         "faceTrack":$faceTrack,"faces":$facesSeen,"stabilize":$stabilize,
         "aeLock":$aeLock,"awbLock":$awbLock,
         "torch":${c?.cameraInfo?.torchState?.value == 1}}
        """.trimIndent().replace("\n", "")
    }

    override fun apply(params: Map<String, String>): String {
        // A plain "camera not bound" here sent someone hunting a fault that was a choice.
        if (dormant) return """{"error":"dormant - /api/wake first"}"""
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
        // WARNING: clamp to what the DEVICE reports, not to a guess. An out-of-range ISO or
        // shutter is not rejected loudly - the camera quietly ignores the whole request and
        // the picture simply does not change, which reads as a broken control.
        params["iso"]?.toIntOrNull()?.let {
            val r = isoRange()
            iso = if (r != null) it.coerceIn(r.lower, r.upper) else it
            manualExposure = true; done += "iso=$iso"
        }
        params["shutterNs"]?.toLongOrNull()?.let {
            val r = shutterRange()
            shutterNs = if (r != null) it.coerceIn(r.lower, r.upper) else it
            manualExposure = true; done += "shutterNs=$shutterNs"
        }
        params["shutter"]?.let { txt ->
            // Accept "1/60" as well as nanoseconds, because that is how anyone thinks about
            // shutter speed.
            val m = Regex("""1/(\d+)""").find(txt)
            if (m != null) {
                val r = shutterRange()
                val ns = 1_000_000_000L / m.groupValues[1].toLong()
                shutterNs = if (r != null) ns.coerceIn(r.lower, r.upper) else ns
                manualExposure = true; done += "shutter=1/${m.groupValues[1]}"
            }
        }
        params["manualExposure"]?.toBooleanStrictOrNull()?.let {
            manualExposure = it; done += "manualExposure=$it"
        }
        params["wbR"]?.toFloatOrNull()?.let { wbR = it; manualWb = true; done += "wbR=$it" }
        params["wbG"]?.toFloatOrNull()?.let { wbG = it; manualWb = true; done += "wbG=$it" }
        params["wbB"]?.toFloatOrNull()?.let { wbB = it; manualWb = true; done += "wbB=$it" }
        params["manualWb"]?.toBooleanStrictOrNull()?.let { manualWb = it; done += "manualWb=$it" }
        params["faceTrack"]?.toBooleanStrictOrNull()?.let { faceTrack = it; done += "faceTrack=$it" }
        params["stabilize"]?.toBooleanStrictOrNull()?.let { stabilize = it; done += "stabilize=$it" }
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
        // Any of these live entirely in the capture request, so one push applies them all
        // without touching the session.
        if (done.any {
                it.startsWith("aeLock") || it.startsWith("awbLock") || it.startsWith("iso") ||
                it.startsWith("shutter") || it.startsWith("manual") || it.startsWith("wb") ||
                it.startsWith("faceTrack") || it.startsWith("stabilize") }) {
            pushCaptureOptions()
        }
        if (needRebind) rebind()
        return """{"applied":[${done.joinToString(",") { "\"$it\"" }}],"rebound":$needRebind}"""
    }
}
