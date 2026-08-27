"""
Pure-python MPEG-TS stream probe (no external binary): parse PAT/PMT for the
stream map, grab the h264 SPS for resolution / display aspect ratio / fps, and
use PCR stamps to estimate the overall bitrate.

Why this exists: it is instant (a few hundred KB scanned, no subprocess), and
it keeps the local-file "details" popup working even on sandbox CPU variants
where the static ffmpeg build's mpegts demuxer is unstable.
"""

from __future__ import annotations

import struct
from pathlib import Path

_TS_PACKET = 188
_STREAM_TYPES = {
    0x01: ("video", "mpeg1"), 0x02: ("video", "mpeg2"), 0x10: ("video", "mpeg2"),
    0x1B: ("video", "h264"), 0x24: ("video", "hevc"), 0xEA: ("video", "vc1"),
    0x03: ("audio", "mp3"), 0x04: ("audio", "mp3"), 0x0F: ("audio", "aac"),
    0x11: ("audio", "aac"), 0x81: ("audio", "ac3"), 0x82: ("audio", "dts"),
    0x06: ("audio", "ac3"), 0x88: ("audio", "eac3"),
}
_AR_IDC = {1: ("1:1", 1.0), 2: ("12:11", 12 / 11), 3: ("10:11", 10 / 11),
           4: ("16:11", 16 / 11), 5: ("40:33", 40 / 33), 6: ("24:11", 24 / 11),
           7: ("20:11", 20 / 11), 8: ("32:11", 32 / 11), 9: ("80:33", 80 / 33),
           10: ("18:11", 18 / 11), 11: ("15:11", 15 / 11), 12: ("64:33", 64 / 33),
           13: ("160:99", 160 / 99), 14: ("4:3", 4 / 3), 15: ("3:2", 3 / 2),
           16: ("2:1", 2.0)}


class _Bits:
    def __init__(self, data: bytes):
        self.d = data
        self.pos = 0

    def u(self, n: int) -> int:
        val = 0
        for _ in range(n):
            byte = self.d[self.pos >> 3]
            val = (val << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return val

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                return 0
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self) -> int:
        k = self.ue()
        return -(-k // 2) if k & 1 else -(k // 2)


def _skip_scaling_list(b: _Bits, count: int) -> None:
    last = 8
    nxt = 8
    for _ in range(count):
        if nxt:
            nxt = (last + b.se() + 256) % 256
        last = last if nxt == 0 else nxt


def parse_h264_sps(sps: bytes) -> dict:
    """Best-effort SPS parse -> {width,height,ratio,fps} (fps only when
    VUI timing info present)."""
    out: dict = {}
    try:
        b = _Bits(sps[1:])                      # drop the NAL header byte
        profile = b.u(8)
        b.u(8)                                  # constraint flags/reserved
        b.u(8)                                  # level
        b.ue()                                  # seq_parameter_set_id
        chroma = 1
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135, 41, 42, 120, 121):
            chroma = b.ue()
            if chroma == 3:
                b.u(1)
            b.ue(); b.ue(); b.u(1)
            if b.u(1):                          # seq_scaling_matrix_present
                for i in range(8 if chroma != 3 else 12):
                    if b.u(1):
                        _skip_scaling_list(b, 16 if i < 6 else 64)
        b.ue()                                  # log2_max_frame_num_minus4
        poc_type = b.ue()
        if poc_type == 0:
            b.ue()
        elif poc_type == 1:
            b.u(1); b.se(); b.se()
            for _ in range(b.ue()):
                b.se()
        b.ue()                                  # max_num_ref_frames
        b.u(1)                                  # gaps_in_frame_num_value_allowed
        wmb = b.ue() + 1                        # pic_width_in_mbs_minus1
        hmmu = b.ue() + 1                       # pic_height_in_map_units_minus1
        frame_only = b.u(1)
        if not frame_only:
            b.u(1)
        b.u(1)                                  # direct_8x8_inference
        crop = [0, 0, 0, 0]
        if b.u(1):                              # frame_cropping_flag
            crop = [b.ue() * 2, b.ue() * 2, b.ue() * 2, b.ue() * 2]
        width = wmb * 16 - (crop[0] + crop[1])
        height = (2 - frame_only) * hmmu * 16 - (crop[2] + crop[3])
        out["width"], out["height"] = width, height
        fps = None
        if b.u(1):                              # vui_parameters_present
            if b.u(1):                          # aspect_ratio_info_present
                idc = b.u(8)
                sar_num, sar_den = 1, 1
                if idc == 255:                  # EXTENDED_SAR
                    sar_num, sar_den = b.u(16), b.u(16)
                    if sar_den:
                        out["ratio"] = f"{round(width * sar_num / sar_den / max(height, 1) * 100) / 100}:1"
                elif idc in _AR_IDC:
                    sar = _AR_IDC[idc][1]
                    dar = width / max(height, 1) * sar
                    # snap to well-known display ratios
                    known = {16 / 9: "16:9", 4 / 3: "4:3", 21 / 9: "21:9",
                             220 / 100: "2.2:1", 239 / 100: "2.39:1", 1.0: "1:1"}
                    close = min(known, key=lambda k: abs(k - dar) / max(dar, 1e-9))
                    out["ratio"] = known[close] if abs(close - dar) / dar < 0.04 \
                        else f"{dar:.2f}:1"
            if b.u(1):                          # overscan
                b.u(1)
            if b.u(1):                          # video_signal_type
                b.u(3)
                if b.u(1):
                    b.u(8); b.u(8); b.u(8)
            if b.u(1):                          # chroma_loc_info
                b.ue(); b.ue()
            if b.u(1):                          # timing_info_present
                num_units_in_tick = b.u(32)
                time_scale = b.u(32)
                fixed = b.u(1)
                if num_units_in_tick:
                    fps = time_scale / (2 * num_units_in_tick) if fixed else None
        if fps:
            out["fps"] = round(fps, 3)
    except (IndexError, ValueError, ZeroDivisionError):
        pass
    return out


def probe_ts(path: str, scan_bytes: int = 4 * 1024 * 1024) -> dict:
    """Probe an MPEG-TS file -> dict shaped like services.probe.probe_media()."""
    data = Path(path).read_bytes()[:scan_bytes]
    if len(data) < _TS_PACKET * 5:
        return {"error": "file too small for TS probe"}

    # locate packet alignment: first 0x47 followed by two more at +188
    off = None
    for i in range(0, len(data) - 3 * _TS_PACKET):
        if data[i] == 0x47 and data[i + _TS_PACKET] == 0x47 and data[i + 2 * _TS_PACKET] == 0x47:
            off = i
            break
    if off is None:
        return {"error": "not an MPEG-TS file (no sync bytes)"}

    pmt_pid = None
    video = audio = []
    pids: dict[int, str] = {}
    video_pes = bytearray()
    first_pcr = last_pcr = None
    stream_names = {"vcodec": None, "acodecs": []}

    for pkt_off in range(off, len(data) - _TS_PACKET, _TS_PACKET):
        pkt = data[pkt_off:pkt_off + _TS_PACKET]
        if pkt[0] != 0x47:
            continue
        payload_starts = bool(pkt[1] & 0x40)
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        has_af = bool(pkt[3] & 0x20)
        has_payload = bool(pkt[3] & 0x10)
        pos = 4
        if has_af:
            af_len = pkt[pos]
            af = pkt[pos + 1:pos + 1 + af_len]
            if af_len >= 7 and (af[0] & 0x10):  # PCR flag
                pcr_base = ((af[1] << 25) | (af[2] << 17) | (af[3] << 9) |
                            (af[4] << 1) | ((af[5] & 0x80) >> 7))
                stamp = pcr_base / 90_000.0
                if first_pcr is None:
                    first_pcr = stamp
                elif stamp >= last_pcr if last_pcr is not None else True:
                    # monotonic advance (wrap handled only if span < wrap period,
                    # which 4 MB of PCR always is)
                    last_pcr = stamp
            pos += 1 + af_len
        if not has_payload or pos >= _TS_PACKET:
            continue
        payload = pkt[pos:]
        if pid == 0 and payload_starts:          # PAT
            ptr = payload[0]
            sec = payload[1 + ptr:]
            if len(sec) >= 12 and sec[0] == 0x00:
                section_end = 3 + ((sec[1] & 0x0F) << 8 | sec[2])
                i = 8
                while i + 4 <= section_end - 4:
                    prog = (sec[i] << 8) | sec[i + 1]
                    ppid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
                    if prog:
                        pmt_pid = pmt_pid if pmt_pid is not None else ppid
                    i += 4
        elif pmt_pid is not None and pid == pmt_pid and payload_starts:  # PMT
            ptr = payload[0]
            sec = payload[1 + ptr:]
            if len(sec) >= 12 and sec[0] == 0x02:
                prog_info_len = ((sec[10] & 0x0F) << 8) | sec[11]
                i = 12 + prog_info_len
                section_end = 3 + ((sec[1] & 0x0F) << 8 | sec[2])
                while i + 5 <= section_end - 4:
                    stype = sec[i]
                    ep = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
                    es_info_len = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
                    kind, codec = _STREAM_TYPES.get(stype, (None, None))
                    if ep in pids:
                        i += 5 + es_info_len
                        continue
                    if kind == "video" and stream_names["vcodec"] is None:
                        stream_names["vcodec"] = codec
                        pids[ep] = "video"
                    elif kind == "audio":
                        stream_names["acodecs"].append(codec)
                        pids[ep] = "audio"
                    else:
                        pids[ep] = "other"
                    i += 5 + es_info_len
        elif pids.get(pid) == "video":           # gather video ES for SPS
            if stream_names["vcodec"] == "h264" and not video:
                video_pes.extend(payload)
                info = _find_sps(bytes(video_pes))
                if info.get("width"):
                    info["codec"] = "h264"
                    video = [info]

    # parse out codec details for hevc/mpeg2 (best-effort: codec name only)
    if not video and stream_names["vcodec"]:
        video = [{"codec": stream_names["vcodec"]}]
    if video:
        video[0].setdefault("codec", stream_names["vcodec"])
    for idx, ac in enumerate(stream_names["acodecs"]):
        audio.append({"codec": ac, "index": idx})

    dur = (last_pcr - first_pcr) if (first_pcr is not None and last_pcr is not None) else None
    out: dict = {"video": video[0] if video else None, "audio": audio}
    if dur and dur > 0.1:
        out["duration_s"] = round(dur, 1)
        out["overall_kbps"] = round(len(data) * 8 / dur / 1000)
    try:
        size = Path(path).stat().st_size
        if dur and dur < 1 and size:
            pass                                   # keep silent: PCR span too short
        out["container"] = "mpegts"
    except OSError:
        pass
    if not out["video"] and not out["audio"]:
        return {"error": "no elementary streams found in TS"}
    return out


def _find_sps(buf: bytes) -> dict:
    """Scan a PES byte stream for the first SPS NAL unit (type 7)."""
    # Annex-B start codes
    i = 0
    end = len(buf)
    while i + 5 < end:
        if buf[i] == 0 and buf[i + 1] == 0 and buf[i + 2] == 1 or \
           (i + 3 < end and buf[i] == 0 and buf[i + 1] == 0 and buf[i + 2] == 0 and buf[i + 3] == 1):
            sc = 3 if buf[i + 2] == 1 else 4
            nal_type = buf[i + sc] & 0x1F
            if nal_type == 7:                     # SPS
                # until next start code or end
                j = i + sc
                k = j
                while k + 2 < end:
                    if buf[k] == 0 and buf[k + 1] == 0 and buf[k + 2] == 1 or \
                       (k + 3 < end and buf[k] == 0 and buf[k + 1] == 0 and buf[k + 2] == 0 and buf[k + 3] == 1):
                        break
                    k += 1
                return parse_h264_sps(_rbsp(buf[j:k]))
            i += sc
        else:
            i += 1
    return {}


def _rbsp(nal: bytes) -> bytes:
    """Strip emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    i = 0
    while i < len(nal):
        if i + 2 < len(nal) and nal[i] == 0 and nal[i + 1] == 0 and nal[i + 2] == 3:
            out.extend(nal[i:i + 2])
            i += 3
        else:
            out.append(nal[i])
            i += 1
    return bytes(out)
