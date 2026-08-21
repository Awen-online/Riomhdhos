package online.awen.rigcam

import java.io.ByteArrayOutputStream

/**
 * A minimal MPEG-TS muxer: H.264 access units in, 188-byte transport packets out.
 *
 * ⚠️ WHY, AFTER SAYING A CONTAINER WAS NOT NEEDED. It was not needed to *ingest* - OBS
 * reads a bare Annex-B elementary stream over HTTP quite happily. It is needed for
 * *timing*. An elementary stream carries NO TIMESTAMPS, so ffmpeg has to invent a
 * presentation schedule, and OBS's Media Source is a file player: its instinct is to buffer
 * for smooth playback rather than to show the newest frame. That is the leading explanation
 * for the WiFi camera sitting ~500 ms behind the wired one after four other hypotheses were
 * measured and disproved (OBS buffering_mb at 4/1/0, a shallower client queue, ffmpeg
 * nobuffer/low_delay, and MediaCodec's own low-latency keys - none of which moved it).
 *
 * TS gives the decoder a PCR clock and a PTS per frame, so it knows when each frame is due
 * instead of guessing.
 *
 * Deliberately small: one program, one video stream, no audio, no PSI beyond PAT and PMT.
 *
 *   PID 0x0000  PAT   -> says program 1's map lives on 0x1000
 *   PID 0x1000  PMT   -> says program 1 is one H.264 stream on 0x0100
 *   PID 0x0100  video PES, carrying PCR in its adaptation field
 *
 * PAT and PMT are re-emitted before every keyframe, so a client that joins mid-stream gets
 * the tables and a decodable picture at the same moment.
 */
class TsMuxer {

    private var patCc = 0
    private var pmtCc = 0
    private var vidCc = 0

    /** One access unit -> the TS packets that carry it, tables included on a keyframe. */
    fun mux(accessUnit: ByteArray, ptsUs: Long, keyframe: Boolean): ByteArray {
        val out = ByteArrayOutputStream(accessUnit.size + 512)
        if (keyframe) {
            out.write(section(PID_PAT, patTable(), patCc)); patCc = (patCc + 1) and 0x0F
            out.write(section(PID_PMT, pmtTable(), pmtCc)); pmtCc = (pmtCc + 1) and 0x0F
        }
        // 90 kHz is the PTS clock; the incoming timestamp is microseconds.
        val pts90 = (ptsUs * 9) / 100
        out.write(pes(accessUnit, pts90, keyframe))
        return out.toByteArray()
    }

    // -- PES ------------------------------------------------------------------------------

    private fun pes(au: ByteArray, pts90: Long, keyframe: Boolean): ByteArray {
        val header = ByteArrayOutputStream(32)
        header.write(0x00); header.write(0x00); header.write(0x01)   // start code prefix
        header.write(0xE0)                                           // stream_id: video
        // PES_packet_length 0 = unbounded, which is legal (and usual) for video.
        header.write(0x00); header.write(0x00)
        header.write(0x84)          // '10', no scrambling, data-alignment indicator set
        header.write(0x80)          // PTS present, no DTS
        header.write(0x05)          // PES header data length
        writePts(header, pts90)
        val payload = header.toByteArray() + au

        val out = ByteArrayOutputStream(payload.size + 256)
        var offset = 0
        var first = true
        while (offset < payload.size) {
            val pkt = ByteArray(188)
            pkt[0] = 0x47
            // ⚠️ PUSI marks the packet that STARTS a PES. On every packet, or on none, a
            // decoder never finds a frame boundary.
            pkt[1] = (((if (first) 0x40 else 0x00) or ((PID_VIDEO shr 8) and 0x1F)).toByte())
            pkt[2] = (PID_VIDEO and 0xFF).toByte()

            // The first packet of a keyframe carries the PCR, so a decoder gets its clock
            // reference exactly where it can start decoding.
            val wantPcr = first && keyframe
            // -1 means "no adaptation field at all", which is NOT the same as a zero-length
            // one. A packet with no AF spends 0 bytes; a zero-length AF still spends the
            // length byte. Conflating them is what produced 189-byte packets and an
            // ArrayIndexOutOfBounds that killed the encoder's drain thread - and with it,
            // all video.
            var afLen = if (wantPcr) 7 else -1          // flags byte + 6 PCR bytes
            val overhead = if (afLen >= 0) afLen + 1 else 0
            val body = minOf(payload.size - offset, 184 - overhead)

            // TS packets are ALWAYS exactly 188 bytes, so a short tail is padded by growing
            // the adaptation field - creating one where there was none costs its length byte.
            val used = 4 + overhead + body
            if (used < 188) {
                val pad = 188 - used
                afLen = if (afLen < 0) pad - 1 else afLen + pad
            }

            var p = 4
            if (afLen >= 0) {
                pkt[3] = (0x30 or (vidCc and 0x0F)).toByte()   // adaptation + payload
                pkt[p++] = afLen.toByte()
                if (afLen > 0) {
                    var flags = 0
                    if (wantPcr) flags = flags or 0x10           // PCR present
                    if (keyframe && first) flags = flags or 0x40 // random access point
                    pkt[p++] = flags.toByte()
                    if (wantPcr) { writePcr(pkt, p, pts90); p += 6 }
                    while (p < 5 + afLen) pkt[p++] = 0xFF.toByte()   // stuffing
                }
            } else {
                pkt[3] = (0x10 or (vidCc and 0x0F)).toByte()   // payload only
            }
            vidCc = (vidCc + 1) and 0x0F

            System.arraycopy(payload, offset, pkt, p, body)
            offset += body
            first = false
            out.write(pkt)
        }
        return out.toByteArray()
    }

    /** PTS is 33 bits, split across 5 bytes with a marker bit after each group. */
    private fun writePts(o: ByteArrayOutputStream, pts: Long) {
        o.write((0x21 or (((pts shr 30) and 0x07).toInt() shl 1)))
        o.write((((pts shr 22) and 0xFF).toInt()))
        o.write((0x01 or (((pts shr 15) and 0x7F).toInt() shl 1)))
        o.write((((pts shr 7) and 0xFF).toInt()))
        o.write((0x01 or ((pts and 0x7F).toInt() shl 1)))
    }

    /** PCR: 33-bit base at 90 kHz, 6 reserved bits, 9-bit extension (unused, 0). */
    private fun writePcr(b: ByteArray, at: Int, pts90: Long) {
        b[at] = ((pts90 shr 25) and 0xFF).toByte()
        b[at + 1] = ((pts90 shr 17) and 0xFF).toByte()
        b[at + 2] = ((pts90 shr 9) and 0xFF).toByte()
        b[at + 3] = ((pts90 shr 1) and 0xFF).toByte()
        b[at + 4] = ((((pts90 and 0x01) shl 7) or 0x7E).toInt()).toByte()
        b[at + 5] = 0x00
    }

    // -- PSI ------------------------------------------------------------------------------

    private fun patTable(): ByteArray {
        val s = ByteArrayOutputStream()
        s.write(0x00)                       // table_id: PAT
        s.write(0xB0); s.write(0x0D)        // syntax indicator + section_length 13
        s.write(0x00); s.write(0x01)        // transport_stream_id
        s.write(0xC1)                       // version 0, current
        s.write(0x00); s.write(0x00)        // section 0 of 0
        s.write(0x00); s.write(0x01)        // program_number 1
        s.write(0xE0 or ((PID_PMT shr 8) and 0x1F)); s.write(PID_PMT and 0xFF)
        return withCrc(s.toByteArray())
    }

    private fun pmtTable(): ByteArray {
        val s = ByteArrayOutputStream()
        s.write(0x02)                       // table_id: PMT
        s.write(0xB0); s.write(0x12)        // section_length 18
        s.write(0x00); s.write(0x01)        // program_number
        s.write(0xC1)
        s.write(0x00); s.write(0x00)
        s.write(0xE0 or ((PID_VIDEO shr 8) and 0x1F)); s.write(PID_VIDEO and 0xFF)  // PCR PID
        s.write(0xF0); s.write(0x00)        // program_info_length 0
        s.write(0x1B)                       // stream_type: H.264
        s.write(0xE0 or ((PID_VIDEO shr 8) and 0x1F)); s.write(PID_VIDEO and 0xFF)
        s.write(0xF0); s.write(0x00)        // ES_info_length 0
        return withCrc(s.toByteArray())
    }

    /** Wrap one PSI section into a single TS packet (both fit comfortably in 184 bytes). */
    private fun section(pid: Int, table: ByteArray, cc: Int): ByteArray {
        val pkt = ByteArray(188)
        pkt[0] = 0x47
        pkt[1] = (0x40 or ((pid shr 8) and 0x1F)).toByte()    // PUSI set for PSI
        pkt[2] = (pid and 0xFF).toByte()
        pkt[3] = (0x10 or (cc and 0x0F)).toByte()
        pkt[4] = 0x00                                          // pointer_field
        System.arraycopy(table, 0, pkt, 5, table.size)
        for (i in 5 + table.size until 188) pkt[i] = 0xFF.toByte()
        return pkt
    }

    companion object {
        private const val PID_PAT = 0x0000
        private const val PID_PMT = 0x1000
        private const val PID_VIDEO = 0x0100

        /**
         * ⚠️ MPEG-2 CRC32, which is NOT the common zlib one: polynomial 0x04C11DB7 taken
         * MSB-first, initialised to all ones, with NO input/output reflection and NO final
         * XOR. Using zlib's crc32 here produces a stream every decoder silently rejects.
         */
        private fun withCrc(section: ByteArray): ByteArray {
            var crc = -0x1  // 0xFFFFFFFF
            for (b in section) {
                crc = crc xor ((b.toInt() and 0xFF) shl 24)
                repeat(8) {
                    crc = if (crc and -0x80000000 != 0) (crc shl 1) xor 0x04C11DB7
                          else crc shl 1
                }
            }
            return section + byteArrayOf(
                ((crc ushr 24) and 0xFF).toByte(),
                ((crc ushr 16) and 0xFF).toByte(),
                ((crc ushr 8) and 0xFF).toByte(),
                (crc and 0xFF).toByte())
        }
    }
}
