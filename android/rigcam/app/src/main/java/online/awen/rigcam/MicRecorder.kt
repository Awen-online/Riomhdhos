package online.awen.rigcam

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import androidx.core.content.ContextCompat
import kotlin.concurrent.thread
import kotlin.math.sqrt

/**
 * The microphone, as an independent capture with no relationship to the camera.
 *
 * ⚠️ WHY `AudioRecord` AND NOT `MediaRecorder` OR CameraX `VideoCapture`. `MediaRecorder`
 * writes a FILE through its own muxer - there is no way to get a live byte stream out of it
 * without a pipe hack. `VideoCapture` would take a second stream off the camera, and this
 * app spends its single CameraX `Preview` slot on the encoder's input surface (that is why
 * there is no on-screen preview at all, and it took three measured attempts to settle) -
 * re-opening the session shape to add a microphone would be trading a solved problem for an
 * unsolved one. `AudioRecord` is a separate HAL path: no shared surface, no shared session,
 * no use-case slot. It coexists with the camera binding without touching it.
 *
 * ⚠️ RAW PCM, NO ENCODER, DELIBERATELY. 48 kHz / 16-bit mono is 768 kbit/s against the
 * 16 Mbit/s of video already flowing - on a LAN that is noise. AAC would buy a bandwidth
 * saving nobody needs, in exchange for encoder lifecycle, ADTS framing and codec-config
 * plumbing: exactly the class of work that produced both of this app's historic "silently
 * produces nothing" bugs. PCM also makes room recording lossless by construction, which is
 * what it is for.
 *
 * ⚠️ EVERY COUNTER HERE EXISTS BECAUSE GRAPHENEOS DOES NOT SURFACE THIS APP'S LOGCAT. The
 * same lesson as the encoder: if it is not in `/api/state` it is invisible. And audio has a
 * failure mode video does not - Android's global microphone kill switch makes `AudioRecord`
 * return SILENCE, not an error, so `rms` is the only thing that would ever tell you.
 */
class MicRecorder(
    private val context: Context,
    private val onBlock: (ByteArray) -> Unit,
) {

    /** CamService listens here: the foreground-service type and the WiFi lock both move. */
    var onStateChange: ((Boolean) -> Unit)? = null

    @Volatile private var record: AudioRecord? = null
    @Volatile var running = false
        private set

    @Volatile var blocks = 0L
        private set
    @Volatile var bytesOut = 0L
        private set
    @Volatile var rms = 0f
        private set
    @Volatile var peak = 0f
        private set
    @Volatile var lastError = ""
        private set
    @Volatile var source = ""
        private set

    fun hasPermission() = ContextCompat.checkSelfPermission(
        context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    /**
     * ⚠️ PREFER `UNPROCESSED`. `MIC` runs the phone's voice chain - AGC, noise suppression,
     * sometimes a high-pass - which is tuned to make a talking head intelligible and is
     * actively wrong for a room: it pumps on quiet passages and eats exactly the ambience
     * being recorded. `UNPROCESSED` is only guaranteed where the device advertises it, so
     * ask rather than assume, and fall back rather than fail.
     */
    private fun pickSource(): Int {
        val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val ok = am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED)
        return if (ok == "true") MediaRecorder.AudioSource.UNPROCESSED
               else MediaRecorder.AudioSource.MIC
    }

    @Synchronized
    fun start(): String {
        if (running) return "already on"
        // Not an error worth throwing: audio is optional here, and the camera must keep
        // working on a phone where the microphone was never granted.
        if (!hasPermission()) {
            lastError = "RECORD_AUDIO not granted"
            return lastError
        }
        val min = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_MASK, ENCODING)
        if (min <= 0) {
            lastError = "getMinBufferSize returned $min"
            return lastError
        }
        val src = pickSource()
        val r = try {
            AudioRecord(src, SAMPLE_RATE, CHANNEL_MASK, ENCODING, maxOf(min, BLOCK_BYTES * 8))
        } catch (e: Exception) {
            lastError = "AudioRecord: ${e.message}"
            return lastError
        }
        // ⚠️ CHECK `state`, DO NOT TRUST THE CONSTRUCTOR. AudioRecord reports a failed
        // initialisation through this field rather than by throwing, and reading from an
        // uninitialised recorder returns errors forever rather than failing once.
        if (r.state != AudioRecord.STATE_INITIALIZED) {
            r.release()
            lastError = "AudioRecord did not initialise (state ${r.state})"
            return lastError
        }
        source = if (src == MediaRecorder.AudioSource.UNPROCESSED) "unprocessed" else "mic"
        record = r
        lastError = ""
        running = true
        try {
            r.startRecording()
        } catch (e: Exception) {
            running = false
            r.release(); record = null
            lastError = "startRecording: ${e.message}"
            return lastError
        }
        thread(name = "rigcam-mic", isDaemon = true) { loop(r) }
        Log.i(TAG, "mic on ($source, $SAMPLE_RATE Hz)")
        onStateChange?.invoke(true)
        return "on"
    }

    @Synchronized
    fun stop(): String {
        if (!running) return "already off"
        running = false
        try { record?.stop() } catch (_: Exception) {}
        try { record?.release() } catch (_: Exception) {}
        record = null
        rms = 0f; peak = 0f
        Log.i(TAG, "mic off")
        onStateChange?.invoke(false)
        return "off"
    }

    private fun loop(r: AudioRecord) {
        // ONE BUFFER, REUSED. Same reason as the video path: a fresh allocation per 20 ms
        // block is pointless garbage. The copy handed to the server is the only allocation,
        // and it has to exist because the block outlives this iteration in client queues.
        val buf = ByteArray(BLOCK_BYTES)
        while (running) {
            val n = try { r.read(buf, 0, buf.size) } catch (e: Exception) {
                lastError = "read: ${e.message}"; break
            }
            if (n < 0) { lastError = "read returned $n"; break }
            if (n == 0) continue
            blocks++
            bytesOut += n
            measure(buf, n)
            onBlock(if (n == buf.size) buf.copyOf() else buf.copyOf(n))
        }
        if (running) {
            // Fell out of the loop on an error rather than a stop() - reflect that in the
            // state, or /api/state would keep claiming the microphone is on.
            running = false
            onStateChange?.invoke(false)
        }
    }

    /** Level, little-endian signed 16-bit. Published so a silent mic is visible as silent. */
    private fun measure(b: ByteArray, n: Int) {
        var sum = 0.0
        var pk = 0
        var i = 0
        while (i + 1 < n) {
            val s = (((b[i + 1].toInt() and 0xFF) shl 8) or (b[i].toInt() and 0xFF)).toShort().toInt()
            sum += s.toDouble() * s
            val a = if (s < 0) -s else s
            if (a > pk) pk = a
            i += 2
        }
        val count = n / 2
        rms = if (count > 0) (sqrt(sum / count) / 32768.0).toFloat() else 0f
        peak = pk / 32768f
    }

    /**
     * ⚠️ NO `String.format` HERE. It uses the default locale, and on a phone set to a
     * comma-decimal locale that emits `"rms":0,0123` - which is not JSON. Kotlin's own
     * float interpolation always writes a dot.
     */
    fun stateJson(): String =
        """{"on":$running,"permission":${hasPermission()},"source":"$source",""" +
        """"sampleRate":$SAMPLE_RATE,"channels":$CHANNELS,"bits":16,""" +
        """"blocks":$blocks,"bytesOut":$bytesOut,"rms":$rms,"peak":$peak,""" +
        """"lastError":"$lastError"}"""

    companion object {
        private const val TAG = "RigCamMic"
        const val SAMPLE_RATE = 48_000
        const val CHANNELS = 1
        private const val CHANNEL_MASK = AudioFormat.CHANNEL_IN_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
        /** 20 ms at 48 kHz mono 16-bit. Small enough not to add audible latency. */
        const val BLOCK_BYTES = SAMPLE_RATE / 50 * 2
    }
}
