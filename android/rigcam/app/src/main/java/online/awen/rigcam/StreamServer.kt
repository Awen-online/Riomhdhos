package online.awen.rigcam

import android.util.Log
import java.io.BufferedOutputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/**
 * A dependency-free HTTP server: MJPEG video out, JSON control in.
 *
 * WHY NOT A LIBRARY: this needs to be auditable on a phone that was deliberately wiped to
 * GrapheneOS. A ~200-line ServerSocket loop with no third-party code is easier to trust
 * than a streaming SDK, and MJPEG needs nothing cleverer.
 *
 * WHY MJPEG AND NOT H.264/RTSP: OBS ingests `multipart/x-mixed-replace` through its Media
 * Source (ffmpeg) with no plugin, and every frame is independent - so a dropped frame
 * costs one frame rather than everything up to the next keyframe. It is fatter on the wire
 * (~10-25 Mbit/s at 720p30) but this runs on a LAN, not the internet. H.264 is the upgrade
 * path if bandwidth ever matters more than simplicity.
 *
 * Endpoints:
 *   GET /                 status page
 *   GET /stream.mjpg      the video
 *   GET /snapshot.jpg     one frame
 *   GET /api/state        JSON: what the camera supports and is currently doing
 *   GET /api/set?...      zoom, aeLock, awbLock, ev, torch, lens, quality
 */
class StreamServer(
    private val port: Int,
    private val controls: Controls,
) {
    interface Controls {
        fun stateJson(): String
        fun apply(params: Map<String, String>): String
    }

    /** Latest encoded frame, published by the camera thread and read by every client. */
    private val lock = Object()
    private var frame: ByteArray? = null
    private var seq = 0L

    @Volatile private var running = false
    private var server: ServerSocket? = null

    /** SPS/PPS from the encoder. Published here so a joining client can be sent it first. */
    @Volatile var codecConfig: ByteArray? = null

    /** How many clients are watching MJPEG. The camera skips JPEG encoding when this is 0. */
    private val mjpegViewers = AtomicInteger(0)
    fun mjpegWanted() = mjpegViewers.get() > 0

    private class NalClient {
        /** Set when the stream's geometry changes; the writer loop then closes the socket. */
        @Volatile var closed = false
        // ⚠️ SHALLOW ON PURPOSE, and 120 was wrong. A queue is latency: at 30 fps, 120
        // frames is FOUR SECONDS of video that a struggling client would work through
        // before showing anything current. For a live camera a late frame is worthless -
        // dropping it and showing the newest one is always the right trade. Six frames caps
        // the queue's own contribution at ~200 ms.
        val q = ArrayBlockingQueue<Pair<ByteArray, Boolean>>(6)
        @Volatile var sawKeyframe = false
    }
    private val nalClients = CopyOnWriteArrayList<NalClient>()
    private val tsClients = CopyOnWriteArrayList<NalClient>()
    private var muxer = TsMuxer()

    /**
     * Disconnect every viewer, because the stream they are decoding no longer exists.
     *
     * ⚠️ THIS IS REQUIRED WHENEVER THE ENCODER IS REBUILT - a resolution or lens change.
     * A bare Annex-B elementary stream has NO CONTAINER, so there is no way to signal
     * mid-stream that the geometry changed. ffmpeg keeps decoding at the OLD size and
     * OBS sits there showing a stale 1280x720 while the phone sends 1920x1080, then
     * drops into a disconnect/reconnect loop. Closing the socket is the only honest
     * signal available: the client reconnects, re-probes, and gets the new SPS/PPS.
     */
    fun dropNalClients() {
        codecConfig = null          // the old SPS/PPS describes a stream that is gone
        muxer = TsMuxer()           // continuity counters restart with the new stream
        for (c in tsClients) {
            c.closed = true
            c.q.offer(ByteArray(0) to false)
        }
        tsClients.clear()
        for (c in nalClients) {
            c.closed = true
            c.q.offer(ByteArray(0) to false)   // wake the writer out of its poll()
        }
        nalClients.clear()
    }

    /** Publish one encoded H.264 access unit to every connected viewer. */
    fun publishNal(nal: ByteArray, keyframe: Boolean, ptsUs: Long) {
        push(nalClients, nal, keyframe)
        // Only pay for muxing when somebody is actually watching the TS endpoint.
        if (tsClients.isNotEmpty()) {
            // ⚠️ REPEAT SPS/PPS BEFORE EVERY KEYFRAME. The encoder reports codec config once,
            // as a separate buffer that is never a displayable frame - so in a container it
            // is simply absent, and ffmpeg reports 'Could not find codec parameters ...
            // unspecified size' and refuses to decode. In the raw path a client got them
            // from the HTTP preamble; a TS client has no preamble, so they go inline.
            val csd = codecConfig
            val au = if (keyframe && csd != null) csd + nal else nal
            push(tsClients, muxer.mux(au, ptsUs, keyframe), keyframe)
        }
    }

    private fun push(list: List<NalClient>, payload: ByteArray, keyframe: Boolean) {
        for (c in list) {
            if (!c.q.offer(payload to keyframe)) {
                c.q.poll()                       // drop the oldest, keep the newest
                c.q.offer(payload to keyframe)
            }
        }
    }

    fun publish(jpeg: ByteArray) {
        synchronized(lock) {
            frame = jpeg
            seq++
            lock.notifyAll()
        }
    }

    /** Blocks until a frame newer than [since] exists. Returns it with its sequence. */
    private fun awaitFrame(since: Long): Pair<ByteArray, Long>? {
        synchronized(lock) {
            var waited = 0
            while (running && seq <= since) {
                lock.wait(2000)
                // A client that has waited this long is watching a stalled camera. Let it
                // go rather than holding the socket open forever.
                if (++waited > 5) return null
            }
            val f = frame ?: return null
            return f to seq
        }
    }

    fun start() {
        running = true
        thread(name = "rigcam-http", isDaemon = true) {
            try {
                val s = ServerSocket(port)
                server = s
                Log.i(TAG, "listening on :$port")
                while (running) {
                    val client = try { s.accept() } catch (e: Exception) { break }
                    thread(isDaemon = true) { serve(client) }
                }
            } catch (e: Exception) {
                Log.e(TAG, "server died", e)
            }
        }
    }

    fun stop() {
        running = false
        synchronized(lock) { lock.notifyAll() }
        try { server?.close() } catch (_: Exception) {}
    }

    private fun serve(sock: Socket) {
        sock.use { s ->
            s.tcpNoDelay = true          // latency matters more than packing here
            val req = s.getInputStream().bufferedReader().readLine() ?: return
            val out = BufferedOutputStream(s.getOutputStream(), 64 * 1024)
            val path = req.split(" ").getOrNull(1) ?: "/"
            try {
                when {
                    path.startsWith("/stream.ts") -> streamTs(out)
                    path.startsWith("/stream.h264") -> streamH264(out)
                    path.startsWith("/stream.mjpg") -> streamTo(out)
                    path.startsWith("/snapshot.jpg") -> snapshot(out)
                    path.startsWith("/api/state") -> json(out, controls.stateJson())
                    path.startsWith("/api/set") -> json(out, controls.apply(query(path)))
                    path == "/" -> html(out)
                    else -> {
                        out.write("HTTP/1.0 404 Not Found\r\n\r\n".toByteArray())
                        out.flush()
                    }
                }
            } catch (_: Exception) {
                // A viewer closing its tab is a broken pipe, not an error worth logging.
            }
        }
    }

    private fun query(path: String): Map<String, String> {
        val q = path.substringAfter('?', "")
        if (q.isEmpty()) return emptyMap()
        return q.split("&").mapNotNull {
            val i = it.indexOf('=')
            if (i <= 0) null
            else URLDecoder.decode(it.substring(0, i), "UTF-8") to
                 URLDecoder.decode(it.substring(i + 1), "UTF-8")
        }.toMap()
    }

    private fun json(out: OutputStream, body: String) {
        val b = body.toByteArray()
        out.write(("HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n" +
                   "Access-Control-Allow-Origin: *\r\n" +
                   "Content-Length: ${b.size}\r\n\r\n").toByteArray())
        out.write(b); out.flush()
    }

    private fun snapshot(out: OutputStream) {
        val f = awaitFrame(0)?.first ?: return
        out.write(("HTTP/1.0 200 OK\r\nContent-Type: image/jpeg\r\n" +
                   "Access-Control-Allow-Origin: *\r\n" +
                   "Content-Length: ${f.size}\r\n\r\n").toByteArray())
        out.write(f); out.flush()
    }

    private fun streamTo(out: OutputStream) {
        out.write(("HTTP/1.0 200 OK\r\n" +
                   "Access-Control-Allow-Origin: *\r\n" +
                   "Cache-Control: no-store\r\n" +
                   "Content-Type: multipart/x-mixed-replace; boundary=$BOUNDARY\r\n\r\n")
                  .toByteArray())
        mjpegViewers.incrementAndGet()
        try {
        var last = 0L
        while (running) {
            val (f, n) = awaitFrame(last) ?: return
            last = n
            out.write(("--$BOUNDARY\r\nContent-Type: image/jpeg\r\n" +
                       "Content-Length: ${f.size}\r\n\r\n").toByteArray())
            out.write(f)
            out.write("\r\n".toByteArray())
            out.flush()
        }
        } finally {
            mjpegViewers.decrementAndGet()
        }
    }

    /**
     * Raw H.264 Annex-B over HTTP.
     *
     * ⚠️ NO CONTAINER, DELIBERATELY. ffmpeg - and therefore OBS's Media Source - probes and
     * plays a bare elementary stream, so this avoids hand-muxing MPEG-TS or fragmented MP4
     * for no gain on a LAN. The cost is that there are no container timestamps: the decoder
     * assumes a constant frame rate, so this is a live view, not something to seek in.
     *
     * A joining client is sent SPS/PPS immediately, then nothing until the next KEYFRAME -
     * feeding it mid-GOP produces a burst of decoder errors and a green screen.
     */
    private fun streamH264(out: OutputStream) {
        out.write(("HTTP/1.0 200 OK\r\n" +
                   "Content-Type: video/h264\r\n" +
                   "Cache-Control: no-store\r\n" +
                   "Access-Control-Allow-Origin: *\r\n\r\n").toByteArray())
        codecConfig?.let { out.write(it) }
        val client = NalClient()
        nalClients.add(client)
        try {
            while (running && !client.closed) {
                val item = client.q.poll(2, TimeUnit.SECONDS) ?: continue
                if (client.closed) return
                val (nal, key) = item
                if (nal.isEmpty()) continue
                if (!client.sawKeyframe) {
                    if (!key) continue
                    client.sawKeyframe = true
                }
                out.write(nal)
                out.flush()
            }
        } finally {
            nalClients.remove(client)
        }
    }

    /**
     * MPEG-TS: the same H.264, but in a container that carries a clock.
     *
     * ⚠️ THIS EXISTS FOR TIMING, NOT FOR INGEST. Bare Annex-B ingests fine; what it cannot
     * do is tell the decoder WHEN each frame is due, so ffmpeg synthesises a schedule and
     * OBS's Media Source - which is a file player - buffers for smoothness. TS carries PCR
     * and a PTS per frame.
     *
     * A joining client waits for a keyframe, and the muxer emits PAT and PMT immediately
     * before every keyframe, so the tables and a decodable picture arrive together.
     */
    private fun streamTs(out: OutputStream) {
        out.write(("HTTP/1.0 200 OK\r\n" +
                   "Content-Type: video/mp2t\r\n" +
                   "Cache-Control: no-store\r\n" +
                   "Access-Control-Allow-Origin: *\r\n\r\n").toByteArray())
        val client = NalClient()
        tsClients.add(client)
        try {
            while (running && !client.closed) {
                val item = client.q.poll(2, TimeUnit.SECONDS) ?: continue
                if (client.closed) return
                val (chunk, key) = item
                if (chunk.isEmpty()) continue
                if (!client.sawKeyframe) {
                    if (!key) continue
                    client.sawKeyframe = true
                }
                out.write(chunk)
                out.flush()
            }
        } finally {
            tsClients.remove(client)
        }
    }

    private fun html(out: OutputStream) {
        val body = """
            <!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
            <style>body{background:#111;color:#ddd;font:14px system-ui;margin:0;padding:16px}
            img{width:100%;max-width:960px;border-radius:6px}code{color:#7fd}</style>
            <h3>RigCam</h3><img src="/stream.mjpg">
            <p>OBS &rarr; Media Source &rarr; uncheck <em>Local File</em> &rarr;
            <code>http://IP:$port/stream.ts</code> (timestamped, preferred) or
            <code>/stream.h264</code></p>
            <p><code>/api/state</code> &middot; <code>/api/set?zoom=1.5&amp;aeLock=true</code></p>
        """.trimIndent().toByteArray()
        out.write(("HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n" +
                   "Content-Length: ${body.size}\r\n\r\n").toByteArray())
        out.write(body); out.flush()
    }

    companion object {
        private const val TAG = "RigCam"
        private const val BOUNDARY = "rigcamframe"
    }
}
