package online.awen.rigcam

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.util.Log
import android.view.Surface
import kotlin.concurrent.thread
import androidx.camera.core.ImageProxy
import java.nio.ByteBuffer

/**
 * Hardware H.264, because the software path could not carry the picture.
 *
 * ⚠️ WHY THIS REPLACED MJPEG - MEASURED. `YuvImage.compressToJpeg` gave **16.5 fps at
 * 800x600** with a 62 ms encode. The decisive clue was that JPEG *quality* changed nothing
 * (40 -> 62 ms, 60 -> 62 ms, 90 -> 62 ms): if entropy coding were the cost, quality would
 * move it. It did not, so the time went into the colour conversion Skia does on the CPU.
 * That is inherent to the path, not tunable, so the path had to change.
 *
 * ⚠️ EVERY COUNTER BELOW IS EXPOSED OVER HTTP ON PURPOSE. GrapheneOS does not surface this
 * app's logcat, and the first version of this encoder reported a confident **33 fps while
 * emitting zero bytes** - the camera was running, the encoder was being handed nothing, and
 * `encodeMs: 0` was the only hint. A frame counter that counts frames the CAMERA produced
 * proves nothing about the ENCODER. Count both ends, or you measure your own optimism.
 */
class H264Encoder(
    private val width: Int,
    private val height: Int,
    fps: Int,
    bitRate: Int,
    private val onNal: (ByteArray, Boolean) -> Unit,
) {
    private val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
    private val info = MediaCodec.BufferInfo()

    /**
     * The camera renders here. Zero CPU involvement in the pixel path.
     *
     * ⚠️ WHY THIS EXISTS - MEASURED. Copying YUV_420_888 into the codec by hand cost
     * **115 ms/frame at 1280x720, capping the whole pipeline at 9 fps**, with `starved 0`
     * proving the encoder was never the limit. ~1.4 MB per frame should take single-digit
     * ms from ordinary RAM; camera buffers are typically UNCACHED dmabuf, where CPU reads
     * run about an order of magnitude slower. That is a property of the memory, not of the
     * loop, so no amount of tuning the copy could fix it - the copy had to go.
     */
    lateinit var inputSurface: Surface
        private set

    @Volatile private var draining = true

    /** SPS/PPS. Every client needs these BEFORE any frame or it decodes nothing. */
    @Volatile var codecConfig: ByteArray? = null
        private set

    // Diagnostics, all readable at /api/state.
    @Volatile var inputMode = "?"; private set     // "image" | "buffer" | "none"
    @Volatile var submitted = 0L; private set      // frames actually handed to the codec
    @Volatile var starved = 0L; private set        // frames dropped: no free input buffer
    @Volatile var nalsOut = 0L; private set        // access units the codec produced
    @Volatile var bytesOut = 0L; private set
    @Volatile var lastError = ""; private set
    private var forceBuffer = false

    private var frameIndex = 0L
    private val usPerFrame = 1_000_000L / fps
    private var stride = 0
    private var sliceHeight = 0
    private var scratch: ByteArray? = null

    init {
        val fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT,
                       MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible)
            setInteger(MediaFormat.KEY_BIT_RATE, bitRate)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps)
            // One second between keyframes: a viewer joining mid-stream sees nothing until
            // the next one, so a long GOP means a long black wait on every reconnect.
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
            setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0)   // reordering = latency
        }
        codec.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        // ⚠️ createInputSurface() MUST come after configure() and before start().
        // This is the whole point of the rewrite: the camera renders straight into the
        // encoder, so no frame is ever read by the CPU.
        inputSurface = codec.createInputSurface()
        codec.start()
        thread(name = "rigcam-drain", isDaemon = true) {
            while (draining) {
                drain()
                Thread.sleep(4)
            }
        }
        val inFmt = codec.inputFormat
        stride = inFmt.getInteger(MediaFormat.KEY_STRIDE, width)
        sliceHeight = inFmt.getInteger(MediaFormat.KEY_SLICE_HEIGHT, height)
        Log.i(TAG, "encoder ${width}x$height stride=$stride slice=$sliceHeight")
    }

    fun submit(image: ImageProxy): Boolean {
        val idx = codec.dequeueInputBuffer(0)
        if (idx < 0) { starved++; return false }

        var size = 0
        try {
            // getInputImage is allowed to return null, and is also allowed to hand back a
            // view whose planes do not tolerate a full-width write. Both are survivable;
            // silently leaking the buffer is not.
            val img = if (forceBuffer) null
                      else try { codec.getInputImage(idx) } catch (_: Exception) { null }
            if (img != null) {
                inputMode = "image"
                copyPlanes(image, img)
                size = width * height * 3 / 2
            } else {
                val buf = codec.getInputBuffer(idx)
                if (buf == null) {
                    inputMode = "none"
                } else {
                    inputMode = "buffer"
                    size = copyNv12(image, buf)
                }
            }
        } catch (e: Exception) {
            lastError = e.javaClass.simpleName + ": " + (e.message ?: "")
            // Self-heal: the Image path failed once, so stop using it. The raw ByteBuffer
            // path lays the frame out to the codec's own stride and is the safer of the two.
            forceBuffer = true
            size = 0
        }

        // ⚠️ ALWAYS GIVE THE BUFFER BACK, even after a failed copy. The first version
        // returned early on failure and never queued it; input buffers leaked one per frame
        // until dequeueInputBuffer starved permanently. It presented as **417 camera frames,
        // 417 starved, 0 submitted, 0 bytes out** - a camera running perfectly into an
        // encoder that could never be fed again.
        codec.queueInputBuffer(idx, 0, size, ptsUs(), 0)
        if (size <= 0) return false
        frameIndex++
        submitted++
        return true
    }

    fun drain() {
        while (true) {
            val idx = codec.dequeueOutputBuffer(info, 0)
            if (idx == MediaCodec.INFO_TRY_AGAIN_LATER) return
            if (idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                // SPS/PPS also arrive here, and this is the more reliable of the two places
                // to pick them up.
                val f = codec.outputFormat
                val sps = f.getByteBuffer("csd-0")
                val pps = f.getByteBuffer("csd-1")
                if (sps != null && pps != null) {
                    val a = ByteArray(sps.remaining()); sps.get(a)
                    val b = ByteArray(pps.remaining()); pps.get(b)
                    codecConfig = a + b
                }
                continue
            }
            if (idx < 0) continue

            val buf = codec.getOutputBuffer(idx)
            if (buf == null) { codec.releaseOutputBuffer(idx, false); continue }
            buf.position(info.offset)
            buf.limit(info.offset + info.size)
            val bytes = ByteArray(info.size)
            buf.get(bytes)
            codec.releaseOutputBuffer(idx, false)

            if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
                codecConfig = bytes
            } else {
                nalsOut++
                bytesOut += bytes.size
                onNal(bytes, info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0)
            }
        }
    }

    fun release() {
        draining = false
        try { codec.signalEndOfInputStream() } catch (_: Exception) {}
        try { inputSurface.release() } catch (_: Exception) {}
        try { codec.stop() } catch (_: Exception) {}
        try { codec.release() } catch (_: Exception) {}
    }

    private fun ptsUs() = frameIndex * usPerFrame

    /**
     * YUV_420_888 -> NV12 laid out to the codec's own stride/sliceHeight.
     *
     * ⚠️ STRIDE IS THE CODEC'S, NOT THE IMAGE'S. The encoder's input buffer is padded to
     * `KEY_STRIDE` per row and its chroma starts at `stride * sliceHeight`, neither of which
     * has to equal width/height. Packing to width instead produces a skewed image.
     */
    private fun copyNv12(src: ImageProxy, dst: ByteBuffer): Int {
        val w = src.width
        val h = src.height
        val cw = w / 2
        val ch = h / 2
        var out = scratch
        val need = stride * sliceHeight + stride * ch
        if (out == null || out.size < need) { out = ByteArray(need); scratch = out }

        // Y
        val yp = src.planes[0]
        val yb = yp.buffer
        val yrow = ByteArray(yp.rowStride)
        for (y in 0 until h) {
            yb.position(y * yp.rowStride)
            yb.get(yrow, 0, minOf(yrow.size, yb.remaining()))
            if (yp.pixelStride == 1) {
                System.arraycopy(yrow, 0, out, y * stride, w)
            } else {
                var o = y * stride
                for (x in 0 until w) out[o++] = yrow[x * yp.pixelStride]
            }
        }

        // UV, interleaved, starting at the codec's slice boundary
        val up = src.planes[1]
        val vp = src.planes[2]
        val ub = up.buffer
        val vb = vp.buffer
        val urow = ByteArray(up.rowStride)
        val vrow = ByteArray(vp.rowStride)
        val base = stride * sliceHeight
        for (y in 0 until ch) {
            ub.position(y * up.rowStride)
            ub.get(urow, 0, minOf(urow.size, ub.remaining()))
            vb.position(y * vp.rowStride)
            vb.get(vrow, 0, minOf(vrow.size, vb.remaining()))
            var o = base + y * stride
            for (x in 0 until cw) {
                out[o++] = urow[x * up.pixelStride]
                out[o++] = vrow[x * vp.pixelStride]
            }
        }

        dst.position(0)
        dst.put(out, 0, need)          // ONE bulk put, not a per-pixel walk
        return need
    }

    companion object {
        private const val TAG = "RigCam"

        private fun copyPlanes(src: ImageProxy, dst: android.media.Image) {
            val w = src.width
            val h = src.height
            copyPlane(src.planes[0], dst.planes[0], w, h)

            val cw = w / 2
            val ch = h / 2
            val du = dst.planes[1]
            if (du.pixelStride == 2) {
                // Semi-planar destination: planes[1] and planes[2] are two views of ONE
                // interleaved buffer, so build the interleaved row and put it in one go.
                val su = src.planes[1]
                val sv = src.planes[2]
                val urow = ByteArray(su.rowStride)
                val vrow = ByteArray(sv.rowStride)
                val out = ByteArray(cw * 2)
                val ub = su.buffer
                val vb = sv.buffer
                val db = du.buffer
                for (y in 0 until ch) {
                    ub.position(y * su.rowStride)
                    ub.get(urow, 0, minOf(urow.size, ub.remaining()))
                    vb.position(y * sv.rowStride)
                    vb.get(vrow, 0, minOf(vrow.size, vb.remaining()))
                    var i = 0
                    for (x in 0 until cw) {
                        out[i++] = urow[x * su.pixelStride]
                        out[i++] = vrow[x * sv.pixelStride]
                    }
                    db.position(y * du.rowStride)
                    db.put(out, 0, cw * 2)
                }
            } else {
                copyPlane(src.planes[1], du, cw, ch)
                copyPlane(src.planes[2], dst.planes[2], cw, ch)
            }
        }

        /**
         * ⚠️ ONE ByteBuffer CALL PER ROW, NEVER PER PIXEL. The first version did
         * `position()` + `put()` for every pixel: ~1.8 million virtual calls per frame at
         * 720p, measured at **457 ms/frame - 2.5 fps**, worse than the JPEG it replaced.
         */
        private fun copyPlane(sp: ImageProxy.PlaneProxy, dp: android.media.Image.Plane,
                              w: Int, h: Int) {
            val sb = sp.buffer
            val db = dp.buffer
            val srow = ByteArray(sp.rowStride)
            val packed = if (sp.pixelStride == 1) srow else ByteArray(w)
            for (y in 0 until h) {
                sb.position(y * sp.rowStride)
                sb.get(srow, 0, minOf(srow.size, sb.remaining()))
                if (sp.pixelStride != 1) {
                    for (x in 0 until w) packed[x] = srow[x * sp.pixelStride]
                }
                db.position(y * dp.rowStride)
                db.put(packed, 0, w)
            }
        }
    }
}
