"""Lightweight partial-retrieval Media Information inspector for Render Free constraints.

Strategy:
  1. Never download the full video. Enforce a 24 MiB application request budget
     (including retries) and a 120-second overall deadline.
  2. Start at 128 KiB, adapt up to 1 MiB, and follow container offsets.
     Alternate same-DC routing and shrink failed ranges. Retain bounded
     stream_media fallback for clients without the range API.
  3. Allow bounded retries for transient transport failures. Log retrieval
     stages, timing and budget use. Keep one inspection active at a time.
  4. Use original container metadata for duration, track counts and languages;
     FFprobe is optional supplementation for missing video codec/dimensions.
  5. Never guess missing languages; clean up temporary files immediately.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import struct
import tempfile
from contextlib import aclosing
from dataclasses import dataclass, field
from html import escape

from parse import human_size

log = logging.getLogger("seriesbot.mediainfo")

CHUNK_SIZE = 1024 * 1024                 # Kurigram stream_media chunk size: 1 MiB
RANGE_SIZE = 128 * 1024
RANGE_RPC_TIMEOUT_SEC = 18.0
RANGE_ATTEMPT_TIMEOUT_SEC = 35.0
HEAD_CHUNKS_MAX = 12
TAIL_CHUNKS_MAX = 12
MAX_TOTAL_BYTES = 24 * 1024 * 1024       # includes app-level retries
INSPECT_TIMEOUT_SEC = 120.0
CHUNK_TIMEOUT_SEC = 55.0                 # allow media DC reconnection before retrying
MAX_CHUNK_RETRIES = 2
RETRY_DELAY_SEC = 1.0

_LANG_NAMES = {
    "eng": "English", "en": "English",
    "jpn": "Japanese", "ja": "Japanese",
    "kor": "Korean", "ko": "Korean",
    "chi": "Chinese", "zho": "Chinese", "zh": "Chinese", "cmn": "Chinese",
    "hin": "Hindi", "hi": "Hindi",
    "spa": "Spanish", "es": "Spanish",
    "fre": "French", "fra": "French", "fr": "French",
    "ger": "German", "deu": "German", "de": "German",
    "ita": "Italian", "it": "Italian",
    "por": "Portuguese", "pt": "Portuguese",
    "rus": "Russian", "ru": "Russian",
    "ara": "Arabic", "ar": "Arabic",
    "tur": "Turkish", "tr": "Turkish",
    "tha": "Thai", "th": "Thai",
    "ind": "Indonesian", "id": "Indonesian",
    "msa": "Malay", "may": "Malay", "ms": "Malay",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "tam": "Tamil", "ta": "Tamil",
    "tel": "Telugu", "te": "Telugu",
    "mal": "Malayalam", "ml": "Malayalam",
    "pol": "Polish", "pl": "Polish",
    "dut": "Dutch", "nld": "Dutch", "nl": "Dutch",
    "swe": "Swedish", "sv": "Swedish",
    "nor": "Norwegian", "no": "Norwegian",
    "dan": "Danish", "da": "Danish",
    "fin": "Finnish", "fi": "Finnish",
    "heb": "Hebrew", "he": "Hebrew",
    "gre": "Greek", "ell": "Greek", "el": "Greek",
    "ces": "Czech", "cze": "Czech", "cs": "Czech",
    "hun": "Hungarian", "hu": "Hungarian",
    "ron": "Romanian", "rum": "Romanian", "ro": "Romanian",
    "ukr": "Ukrainian", "uk": "Ukrainian",
    "fil": "Filipino", "tgl": "Tagalog", "tl": "Tagalog",
}

_CODEC_LABELS = {
    "s_text/utf8": "SubRip", "subrip": "SubRip", "s_text/ass": "ASS", "s_text/ssa": "SSA",
    "s_hdmv/pgs": "PGS", "s_vobsub": "VobSub", "mov_text": "Timed text", "tx3g": "Timed text",
    "hevc": "HEVC / H.265", "h265": "HEVC / H.265", "hvc1": "HEVC / H.265", "hev1": "HEVC / H.265",
    "v_mpegh/iso/hevc": "HEVC / H.265",
    "h264": "H.264 / AVC", "avc": "H.264 / AVC", "avc1": "H.264 / AVC", "avc3": "H.264 / AVC",
    "v_mpeg4/iso/avc": "H.264 / AVC",
    "av1": "AV1", "av01": "AV1", "v_av1": "AV1",
    "vp9": "VP9", "vp09": "VP9", "v_vp9": "VP9",
    "vp8": "VP8", "v_vp8": "VP8",
    "mpeg4": "MPEG-4", "mp4v": "MPEG-4", "v_mpeg4/iso/asp": "MPEG-4",
    "mpeg2video": "MPEG-2", "v_mpeg2": "MPEG-2",
    "aac": "AAC", "mp4a": "AAC", "a_aac": "AAC",
    "ac3": "AC-3", "ac-3": "AC-3", "a_ac3": "AC-3",
    "eac3": "E-AC-3", "ec-3": "E-AC-3", "a_eac3": "E-AC-3",
    "opus": "Opus", "a_opus": "Opus",
    "flac": "FLAC", "a_flac": "FLAC",
    "mp3": "MP3", "a_mpeg/l3": "MP3",
    "dts": "DTS", "a_dts": "DTS",
    "truehd": "TrueHD", "a_truehd": "TrueHD",
    "vorbis": "Vorbis", "a_vorbis": "Vorbis",
}


def _norm_lang(code: str | None) -> str | None:
    if not code:
        return None
    raw = str(code).strip().lower().replace("_", "-")
    if raw == "zxx":
        return "No linguistic content"
    if raw == "mul":
        return "Multiple languages"
    if not raw or raw.split("-")[0] in ("und", "unk", "unknown", "none", "zxx", "mis", "qaa"):
        return None
    base = raw.split("-")[0]
    if base in _LANG_NAMES:
        return _LANG_NAMES[base]
    if raw in _LANG_NAMES:
        return _LANG_NAMES[raw]
    if len(base) in (2, 3) and base.isalpha():
        return base.upper()
    return None


def _language_from_track_name(name: str | None) -> str | None:
    """Use only explicit, unambiguous language labels in embedded track names.

    Do not look at the filename, arbitrary words, or FFprobe defaults. Allow
    common accessibility/region labels, but not mixed-language track names.
    """
    import re
    label = " ".join((name or "").strip().split())
    for language in sorted(set(_LANG_NAMES.values()), key=len, reverse=True):
        match = re.fullmatch(re.escape(language) +
            r"(?:\s*\((?:SDH|CC|forced|default|full|dub|commentary|audio description|Brasil|Brazil|Portugal|US|UK)\))?"
            r"(?:\s+(?:SDH|CC|forced|default|full|dub|commentary))?", label, re.I)
        if match:
            return language
    return None


def _norm_codec(raw: str | None) -> str | None:
    if not raw:
        return None
    key = str(raw).strip().lower()
    if key in _CODEC_LABELS:
        return _CODEC_LABELS[key]
    for prefix, label in (("a_aac/", "AAC"), ("v_mpeg4/iso/avc", "H.264 / AVC"), ("v_mpegh/iso/hevc", "HEVC / H.265")):
        if key.startswith(prefix):
            return label
    return str(raw).strip().upper()[:24]


def _resolution_label(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    tag = None
    if height >= 2000 or width >= 3700:
        tag = "2160p"
    elif height >= 1350 or width >= 2400:
        tag = "1440p"
    elif height >= 1000 or width >= 1800:
        tag = "1080p"
    elif height >= 680 or width >= 1200:
        tag = "720p"
    elif height >= 520:
        tag = "540p"
    elif height >= 440:
        tag = "480p"
    elif height >= 320:
        tag = "360p"
    return f"{width} × {height}" + (f" ({tag})" if tag else "")


def _format_duration(seconds: float | None) -> str | None:
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        return None
    total = int(round(seconds))
    hrs, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hrs:
        return f"{hrs}h {mins}m {secs}s"
    return f"{mins}m {secs}s" if mins else f"{secs}s"


@dataclass
class TrackInfo:
    kind: str                       # "video" | "audio" | "subtitle"
    codec: str | None = None
    language: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    language_default: bool = False
    name: str | None = None
    channels: int | None = None
    sample_rate: float | None = None
    aspect: str | None = None
    bit_depth: int | None = None
    default: bool | None = None
    forced: bool | None = None
    hearing_impaired: bool | None = None


@dataclass
class MediaReport:
    file_name: str = ""
    file_size: int = 0
    duration: float | None = None
    video: list[TrackInfo] = field(default_factory=list)
    audio: list[TrackInfo] = field(default_factory=list)
    subtitles: list[TrackInfo] = field(default_factory=list)
    bytes_fetched: int = 0
    method: str = ""
    tracks_complete: bool = False
    container: str | None = None
    writing_app: str | None = None
    muxing_app: str | None = None
    creation_time: str | None = None

    @property
    def has_tracks(self) -> bool:
        return bool(self.video or self.audio or self.subtitles)


def _track_details(t: TrackInfo) -> str:
    details = []
    if t.language_default:
        details.append("container default")
    if t.name:
        details.append(t.name[:60])
    if t.channels:
        details.append({1: "Mono", 2: "Stereo"}.get(t.channels, f"{t.channels} channels"))
    if t.sample_rate and 1000 <= t.sample_rate <= 768000:
        details.append(f"{t.sample_rate / 1000:g} kHz")
    if t.default:
        details.append("Default")
    if t.forced:
        details.append("Forced")
    if t.hearing_impaired:
        details.append("Hearing impaired")
    return " · " + " · ".join(escape(d) for d in details) if details else ""


def format_media_report(report: MediaReport) -> str:
    """Format a user-friendly Telegram HTML report showing only retrieved fields."""
    lines: list[str] = ["<b>MEDIA INFORMATION</b>", ""]
    if report.file_name:
        name = report.file_name[:500]
        lines.append(f"File: <code>{escape(name)}{'…' if len(name) < len(report.file_name) else ''}</code>")
    if report.file_size and report.file_size > 0:
        lines.append(f"Size: {escape(human_size(report.file_size))}")
    dur = _format_duration(report.duration)
    if dur:
        lines.append(f"Duration: {escape(dur)}")
    else:
        lines.append("Duration: unavailable")

    if report.container:
        lines.append(f"Format: {escape(report.container)}")
    if report.file_size > 0 and report.duration and math.isfinite(report.duration) and report.duration > 0:
        lines.append(f"Average bitrate: {round(report.file_size * 8 / report.duration / 1000):,} kbps")

    if report.video:
        v = report.video[0]
        v_lines: list[str] = []
        res = _resolution_label(v.width, v.height)
        if res:
            v_lines.append(f"• Resolution: {escape(res)}")
        if v.codec:
            v_lines.append(f"• Codec: {escape(v.codec)}")
        if v.fps and 1.0 <= v.fps <= 240.0:
            fps_s = f"{v.fps:.3f}".rstrip("0").rstrip(".")
            v_lines.append(f"• Frame rate: {fps_s} FPS")
        if v.aspect:
            v_lines.append(f"• Display aspect ratio: {escape(v.aspect)}")
        if v.bit_depth:
            v_lines.append(f"• Bit depth: {v.bit_depth}-bit (declared)")
        if v_lines:
            lines += ["", "<b>VIDEO</b>", *v_lines]

    if report.audio:
        a_lines = [f"• Tracks: {len(report.audio)}"]
        for i, a in enumerate(report.audio, 1):
            lang = a.language or "Unknown"
            extra = f" ({a.codec})" if a.codec else ""
            a_lines.append(f"• Track {i}: {escape(lang)}{escape(extra)}" + _track_details(a))
        lines += ["", "<b>AUDIO</b>", *a_lines]

    else:
        lines += ["", "<b>AUDIO</b>", "• Tracks: 0" if report.tracks_complete else "• Tracks: unavailable"]

    if report.subtitles:
        s_lines = [f"• Tracks: {len(report.subtitles)}"]
        for i, st in enumerate(report.subtitles, 1):
            lang = st.language or "Unknown"
            s_lines.append(f"• Track {i}: {escape(lang)}" + (f" ({escape(st.codec)})" if st.codec else "") + _track_details(st))
        lines += ["", "<b>SUBTITLES</b>", *s_lines]

    else:
        lines += ["", "<b>SUBTITLES</b>", "• Tracks: 0" if report.tracks_complete else "• Tracks: unavailable"]

    technical = [("Muxing app", report.muxing_app), ("Writing app", report.writing_app), ("Creation time", report.creation_time)]
    if any(v for _, v in technical):
        lines += ["", "<b>TECHNICAL</b>"] + [f"• {k}: {escape(v[:100])}" for k,v in technical if v]
    # Preserve whole HTML lines and both track sections, including the counts.
    # Normal files (including 29 subtitle tracks) fit easily. For exceptional
    # files, reserve room for headings and state the number of omitted rows.
    result = []
    omitted = 0
    used = sum(len(line)+1 for line in lines if not line.startswith("• Track "))
    for line in lines:
        if line.startswith("• Track ") and used + len(line) + 1 > 3650:
            omitted += 1
        else:
            result.append(line)
            if line.startswith("• Track "):
                used += len(line) + 1
    if omitted:
        result.append(f"… {omitted} additional track(s) not displayed.")
    return "\n".join(result)


# ------------------------------------------------------------------ MP4 (ISO BMFF) parser
def _iter_mp4_boxes(buf: bytes, start: int = 0, end: int | None = None):
    n = len(buf) if end is None else min(end, len(buf))
    pos = start
    while pos + 8 <= n:
        size = struct.unpack_from(">I", buf, pos)[0]
        btype = buf[pos + 4:pos + 8]
        hdr = 8
        if size == 1:
            if pos + 16 > n:
                break
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = n - pos
        if size < hdr or pos + size > n:
            break
        yield btype, pos, pos + hdr, pos + size
        pos += size


def _extract_mp4_box(buf: bytes, target: bytes) -> bytes | None:
    """Locate a complete top-level MP4 box (e.g. ftyp or moov) inside a head or tail buffer."""
    # First try structured top-level walk (fast path for head buffers)
    for btype, start, _hdr, end in _iter_mp4_boxes(buf):
        if btype == target:
            return buf[start:end]
    # Tail buffer may start mid-mdat; scan for exact box header + valid length
    idx = 4
    n = len(buf)
    while idx < n:
        found = buf.find(target, idx)
        if found < 4:
            break
        size = struct.unpack_from(">I", buf, found - 4)[0]
        header = 8
        if size == 1 and found + 12 <= n:
            size = struct.unpack_from(">Q", buf, found + 4)[0]
            header = 16
        if header + 8 <= size <= n - (found - 4):
            # Verify child box structure for moov to avoid false positives inside media payload
            candidate = buf[found - 4:found - 4 + size]
            if target != b"moov":
                return candidate
            child_types = {bt for bt, _, _, _ in _iter_mp4_boxes(candidate, header, len(candidate))}
            if b"mvhd" in child_types or b"trak" in child_types:
                return candidate
        idx = found + 4
    return None


def _unpack_mp4_lang(bits: int) -> str | None:
    c1 = ((bits >> 10) & 0x1F)
    c2 = ((bits >> 5) & 0x1F)
    c3 = (bits & 0x1F)
    if not all(1 <= c <= 26 for c in (c1, c2, c3)):
        return None
    code = chr(c1 + 0x60) + chr(c2 + 0x60) + chr(c3 + 0x60)
    return _norm_lang(code)


def parse_mp4_bytes(buf: bytes, file_name: str = "", file_size: int = 0) -> MediaReport | None:
    moov = _extract_mp4_box(buf, b"moov")
    if not moov:
        return None
    rep = MediaReport(file_name=file_name, file_size=file_size, method="mp4_parser", tracks_complete=True, container="MP4 / ISO BMFF")
    box_header = 16 if moov[:4] == b"\x00\x00\x00\x01" else 8
    for btype, _s, hdr, end in _iter_mp4_boxes(moov, box_header, len(moov)):
        if btype == b"mvhd" and end - hdr >= 20:
            ver = moov[hdr]
            if ver == 1 and end - hdr >= 32:
                ts = struct.unpack_from(">I", moov, hdr + 20)[0]
                dur = struct.unpack_from(">Q", moov, hdr + 24)[0]
            else:
                ts = struct.unpack_from(">I", moov, hdr + 12)[0]
                dur = struct.unpack_from(">I", moov, hdr + 16)[0]
            if ts > 0 and dur > 0:
                rep.duration = dur / ts
        elif btype == b"trak":
            tr = _parse_mp4_trak(moov, hdr, end)
            if tr is None:
                continue
            if tr.kind == "video":
                rep.video.append(tr)
            elif tr.kind == "audio":
                rep.audio.append(tr)
            elif tr.kind == "subtitle":
                rep.subtitles.append(tr)
    return rep if rep.has_tracks else None


def _parse_mp4_trak(buf: bytes, start: int, end: int) -> TrackInfo | None:
    tk_w = tk_h = None
    hdlr_type = None
    md_ts = md_dur = 0
    lang = None
    codec = None
    st_w = st_h = None
    fps = None

    for btype, _s, hdr, bend in _iter_mp4_boxes(buf, start, end):
        if btype == b"tkhd" and bend - hdr >= 84:
            ver = buf[hdr]
            off = hdr + (88 if ver == 1 else 76)
            if off + 8 <= bend:
                w = struct.unpack_from(">I", buf, off)[0] >> 16
                h = struct.unpack_from(">I", buf, off + 4)[0] >> 16
                if w > 0 and h > 0:
                    tk_w, tk_h = w, h
        elif btype == b"mdia":
            for mtype, _ms, mhdr, mend in _iter_mp4_boxes(buf, hdr, bend):
                if mtype == b"mdhd" and mend - mhdr >= 24:
                    ver = buf[mhdr]
                    if ver == 1 and mend - mhdr >= 36:
                        md_ts = struct.unpack_from(">I", buf, mhdr + 20)[0]
                        md_dur = struct.unpack_from(">Q", buf, mhdr + 24)[0]
                        lang = _unpack_mp4_lang(struct.unpack_from(">H", buf, mhdr + 32)[0])
                    else:
                        md_ts = struct.unpack_from(">I", buf, mhdr + 12)[0]
                        md_dur = struct.unpack_from(">I", buf, mhdr + 16)[0]
                        lang = _unpack_mp4_lang(struct.unpack_from(">H", buf, mhdr + 20)[0])
                elif mtype == b"hdlr" and mend - mhdr >= 12:
                    hdlr_type = buf[mhdr + 8:mhdr + 12]
                elif mtype == b"minf":
                    for itype, _is, ihdr, iend in _iter_mp4_boxes(buf, mhdr, mend):
                        if itype != b"stbl":
                            continue
                        for stype, _ss, shdr, send in _iter_mp4_boxes(buf, ihdr, iend):
                            if stype == b"stsd" and send - shdr >= 16:
                                entry_off = shdr + 8
                                if entry_off + 8 <= send:
                                    esize = struct.unpack_from(">I", buf, entry_off)[0]
                                    ecodec = buf[entry_off + 4:entry_off + 8].decode("ascii", "ignore")
                                    codec = _norm_codec(ecodec)
                                    # VisualSampleEntry has width/height at +32/+34 from entry start
                                    if esize >= 36 and entry_off + 36 <= send:
                                        w = struct.unpack_from(">H", buf, entry_off + 32)[0]
                                        h = struct.unpack_from(">H", buf, entry_off + 34)[0]
                                        if 64 <= w <= 8192 and 64 <= h <= 8192:
                                            st_w, st_h = w, h
                            elif stype == b"stts" and send - shdr >= 16 and md_ts > 0:
                                count = struct.unpack_from(">I", buf, shdr + 4)[0]
                                total_samples = 0
                                total_delta = 0
                                off = shdr + 8
                                for _ in range(min(count, 2000)):
                                    if off + 8 > send:
                                        break
                                    sc, sd = struct.unpack_from(">II", buf, off)
                                    total_samples += sc
                                    total_delta += sc * sd
                                    off += 8
                                if total_samples > 0 and total_delta > 0:
                                    fps = (total_samples * md_ts) / total_delta

    if hdlr_type == b"vide":
        return TrackInfo("video", codec=codec, language=lang, width=st_w or tk_w, height=st_h or tk_h, fps=fps)
    if hdlr_type == b"soun":
        return TrackInfo("audio", codec=codec, language=lang)
    if hdlr_type in (b"subt", b"sbtl", b"text", b"clcp"):
        return TrackInfo("subtitle", codec=codec, language=lang)
    return None


# ------------------------------------------------------------------ MKV / WebM (EBML) parser
def _read_vint(buf: bytes, pos: int, keep_marker: bool = False) -> tuple[int, int] | None:
    if pos >= len(buf):
        return None
    b0 = buf[pos]
    if b0 == 0:
        return None
    length = 1
    mask = 0x80
    while length <= 8 and not (b0 & mask):
        length += 1
        mask >>= 1
    if length > 8 or pos + length > len(buf):
        return None
    val = b0 if keep_marker else (b0 & (mask - 1))
    for i in range(1, length):
        val = (val << 8) | buf[pos + i]
    return val, pos + length


def _ebml_uint(data: bytes) -> int:
    v = 0
    for b in data:
        v = (v << 8) | b
    return v


def _ebml_float(data: bytes) -> float | None:
    if len(data) == 4:
        return struct.unpack(">f", data)[0]
    if len(data) == 8:
        return struct.unpack(">d", data)[0]
    return None


def _iter_ebml(buf: bytes, start: int, end: int):
    pos = start
    limit = min(end, len(buf))
    while pos < limit:
        eid_res = _read_vint(buf, pos, keep_marker=True)
        if not eid_res:
            break
        eid, pos2 = eid_res
        sz_res = _read_vint(buf, pos2, keep_marker=False)
        if not sz_res:
            break
        size, data_start = sz_res
        # Unknown-size marker (all 1-bits)
        sz_len = data_start - pos2
        if size == (1 << (7 * sz_len)) - 1:
            data_end = limit
        else:
            data_end = min(data_start + size, limit)
        yield eid, data_start, data_end, (data_start + size <= limit if size != (1 << (7 * sz_len)) - 1 else True)
        if data_end <= pos:
            break
        pos = data_end


def parse_mkv_bytes(buf: bytes, file_name: str = "", file_size: int = 0) -> MediaReport | None:
    if not buf.startswith(b"\x1a\x45\xdf\xa3"):
        return None
    rep = MediaReport(file_name=file_name, file_size=file_size, method="mkv_parser", container="Matroska / WebM")
    ts_scale = 1_000_000
    raw_dur = None

    def _parse_info(s: int, e: int):
        nonlocal ts_scale, raw_dur
        for eid, ds, de, _complete in _iter_ebml(buf, s, e):
            if not _complete:
                continue
            if eid == 0x4D80:
                rep.muxing_app = buf[ds:min(de, ds+100)].decode("utf-8", "replace").strip("\x00")
            elif eid == 0x5741:
                rep.writing_app = buf[ds:min(de, ds+100)].decode("utf-8", "replace").strip("\x00")
            elif eid == 0x4461 and de-ds == 8:
                from datetime import datetime, timedelta, timezone
                try:
                    stamp = datetime(2001, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int.from_bytes(buf[ds:de], "big", signed=True)//1000)
                    rep.creation_time = stamp.strftime("%Y-%m-%d %H:%M:%S UTC")
                except (OverflowError, ValueError):
                    pass
            elif eid == 0x2AD7B1:          # TimestampScale (ns)
                v = _ebml_uint(buf[ds:de])
                if v > 0:
                    ts_scale = v
            elif eid == 0x4489:          # Duration
                raw_dur = _ebml_float(buf[ds:de])

    def _parse_tracks(s: int, e: int):
        for eid, ds, de, _complete in _iter_ebml(buf, s, e):
            if eid != 0xAE or not _complete:              # TrackEntry
                continue
            ttype = 0
            codec_id = None
            lang_bcp47 = None
            lang_iso = None
            track_name = None
            default_dur = None
            w = h = dw = dh = channels = sample_rate = bit_depth = None
            display_unit = 0
            flags = {}
            complete = True
            last_end = ds
            for teid, tds, tde, _tc in _iter_ebml(buf, ds, de):
                last_end = tde
                if not _tc:
                    complete = False
                    continue
                if teid in (0x88, 0x55AA, 0x55AB):
                    flags[teid] = bool(_ebml_uint(buf[tds:tde]))
                elif teid == 0x83:
                    ttype = _ebml_uint(buf[tds:tde])
                elif teid == 0x86:
                    codec_id = buf[tds:tde].decode("utf-8", "ignore").strip("\x00")
                elif teid == 0x22B59D:   # LanguageBCP47 (takes precedence in Matroska v4)
                    lang_bcp47 = buf[tds:tde].decode("utf-8", "ignore").strip("\x00")
                elif teid == 0x22B59C:   # Language (ISO-639-2)
                    lang_iso = buf[tds:tde].decode("utf-8", "ignore").strip("\x00")
                elif teid == 0x536E:    # Name: explicitly stored track label
                    track_name = buf[tds:min(tde,tds+240)].decode("utf-8", "ignore").strip("\x00")
                elif teid == 0x23E383:   # DefaultDuration (ns)
                    default_dur = _ebml_uint(buf[tds:tde])
                elif teid == 0xE0:       # Video
                    for veid, vds, vde, _vc in _iter_ebml(buf, tds, tde):
                        if not _vc:
                            continue
                        if veid == 0x54B0:
                            dw = _ebml_uint(buf[vds:vde])
                        elif veid == 0x54BA:
                            dh = _ebml_uint(buf[vds:vde])
                        elif veid == 0x54B2:
                            display_unit = _ebml_uint(buf[vds:vde])
                        elif veid == 0x55B0:
                            for cid, cds, cde, ok in _iter_ebml(buf, vds, vde):
                                if ok and cid == 0x55B2:
                                    depth = _ebml_uint(buf[cds:cde])
                                    if 1 <= depth <= 32: bit_depth = depth
                        elif veid == 0xB0:
                            w = _ebml_uint(buf[vds:vde])
                        elif veid == 0xBA:
                            h = _ebml_uint(buf[vds:vde])
                elif teid == 0xE1:       # Audio metadata, no codec payload probing
                    for aid, ads, ade, ok in _iter_ebml(buf, tds, tde):
                        if not ok: continue
                        if aid == 0x9F:
                            n = _ebml_uint(buf[ads:ade])
                            if 1 <= n <= 64: channels = n
                        elif aid == 0xB5:
                            n = _ebml_float(buf[ads:ade])
                            if n and math.isfinite(n) and 1000 <= n <= 768000: sample_rate = n
            # RFC 9559: absent legacy Language defaults to eng; modern tag overrides.
            # Explicit undefined never inherits the default. Embedded names retain the existing fallback.
            raw_lang = lang_bcp47 if lang_bcp47 is not None else lang_iso
            lang = _norm_lang(raw_lang) or _language_from_track_name(track_name)
            default_lang = complete and last_end == de and raw_lang is None and lang is None
            if default_lang: lang = "English"
            fps = (1_000_000_000.0 / default_dur) if (default_dur and default_dur > 0) else None
            extra = dict(language_default=default_lang, name=track_name, default=flags.get(0x88), forced=flags.get(0x55AA), hearing_impaired=flags.get(0x55AB))
            aspect = None
            aw, ah = dw or w, dh or h
            if aw and ah and display_unit in (0, 3):
                gcd = math.gcd(aw, ah)
                aspect = f"{aw//gcd}:{ah//gcd}"
            if ttype == 1:
                rep.video.append(TrackInfo("video", codec=_norm_codec(codec_id), language=lang, width=w, height=h, fps=fps, aspect=aspect, bit_depth=bit_depth, **extra))
            elif ttype == 2:
                rep.audio.append(TrackInfo("audio", codec=_norm_codec(codec_id), language=lang, channels=channels, sample_rate=sample_rate, **extra))
            elif ttype == 17:
                rep.subtitles.append(TrackInfo("subtitle", codec=_norm_codec(codec_id), language=lang, **extra))

    for eid, ds, de, _complete in _iter_ebml(buf, 0, len(buf)):
        if eid == 0x18538067:            # Segment
            for seid, sds, sde, _sc in _iter_ebml(buf, ds, de):
                if seid == 0x1F43B675:   # Cluster: media payload starts; stop scanning
                    break
                if seid == 0x1549A966:   # Info
                    _parse_info(sds, sde)
                elif seid == 0x1654AE6B: # Tracks
                    if _sc:
                        _parse_tracks(sds, sde)
                        rep.tracks_complete = True
            break

    if raw_dur and math.isfinite(raw_dur) and raw_dur > 0:
        rep.duration = (raw_dur * ts_scale) / 1_000_000_000.0
    return rep if rep.has_tracks and rep.tracks_complete else None


# ------------------------------------------------------------------ FFprobe runner
def _parse_ffprobe_json(data: dict, file_name: str, file_size: int) -> MediaReport | None:
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    rep = MediaReport(file_name=file_name, file_size=file_size, method="ffprobe")
    try:
        dur = float(fmt.get("duration") or 0)
        if dur > 0:
            rep.duration = dur
    except (TypeError, ValueError):
        pass

    for st in streams:
        ctype = str(st.get("codec_type") or "").lower()
        tags = st.get("tags") or {}
        lang = _norm_lang(tags.get("language") or tags.get("LANGUAGE"))
        codec = _norm_codec(st.get("codec_name") or st.get("codec_tag_string"))
        if ctype == "video":
            disp = st.get("disposition") or {}
            if disp.get("attached_pic"):
                continue
            w = int(st.get("width") or 0) or None
            h = int(st.get("height") or 0) or None
            fps = None
            for key in ("avg_frame_rate", "r_frame_rate"):
                raw_r = str(st.get(key) or "")
                if "/" in raw_r:
                    a, b = raw_r.split("/", 1)
                    try:
                        ai, bi = float(a), float(b)
                        if bi > 0 and ai > 0:
                            cand = ai / bi
                            if 1.0 <= cand <= 240.0:
                                fps = cand
                                break
                    except ValueError:
                        pass
            rep.video.append(TrackInfo("video", codec=codec, language=lang, width=w, height=h, fps=fps))
        elif ctype == "audio":
            rep.audio.append(TrackInfo("audio", codec=codec, language=lang))
        elif ctype == "subtitle":
            rep.subtitles.append(TrackInfo("subtitle", codec=codec, language=lang))
    return rep if rep.has_tracks else None


async def _run_ffprobe_on_bytes(data: bytes, suffix: str, file_name: str, file_size: int, timeout: float = 10.0) -> MediaReport | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not data:
        return None
    tmp_path = None
    proc = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="exivum-mi-", suffix=suffix or ".bin")
        try:
            with os.fdopen(fd, "wb", closefd=False) as out:
                out.write(data)
        finally:
            os.close(fd)
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return None
        if proc.returncode != 0 or not stdout:
            return None
        parsed = json.loads(stdout.decode("utf-8", "ignore"))
        return _parse_ffprobe_json(parsed, file_name, file_size)
    except Exception:
        log.debug("ffprobe failed on partial buffer", exc_info=True)
        return None
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _is_mp4_header(buf: bytes) -> bool:
    return len(buf) >= 12 and buf[4:8] == b"ftyp"


def _is_mkv_header(buf: bytes) -> bool:
    return buf.startswith(b"\x1a\x45\xdf\xa3")


async def _probe_buffer(buf: bytes, suffix: str, file_name: str, file_size: int) -> MediaReport | None:
    # The original container is authoritative for duration and language. FFprobe
    # may substitute defaults and derive duration from incomplete sample tables.
    if _is_mkv_header(buf):
        rep = parse_mkv_bytes(buf, file_name, file_size)
    elif _is_mp4_header(buf) or b"moov" in buf or suffix in (".mp4", ".mov", ".m4v"):
        rep = parse_mp4_bytes(buf, file_name, file_size)
    else:
        return None
    if rep and any(not t.codec or not t.width or not t.height for t in rep.video):
        # Optional supplementary codec/dimensions only, never language, duration
        # or track counts from a synthetic/incomplete file.
        extra = await _run_ffprobe_on_bytes(buf, suffix, file_name, file_size)
        if extra and len(extra.video) == len(rep.video):
            for t, x in zip(rep.video, extra.video):
                t.codec = t.codec or x.codec
                t.width = t.width or x.width
                t.height = t.height or x.height
    return rep


class _MediaTransportError(Exception):
    """A requested metadata chunk could not be retrieved intact."""


@dataclass
class _RetrievalBudget:
    maximum: int
    requested: int = 0
    received: int = 0
    phase: str = "source message"
    partial: MediaReport | None = None

    @property
    def remaining(self) -> int:
        return self.maximum - self.requested


def _retryable_stream_error(exc: Exception) -> bool:
    # Kurigram may surface transport errors as OSError/TimeoutError or an RPC
    # server error. Do not retry permission errors or flood waits ourselves.
    return isinstance(exc, (OSError, TimeoutError, EOFError, _MediaTransportError)) or (
        getattr(exc, "CODE", None) in (500, 503)
    )


async def _fetch_chunk(client, src_msg, offset: int, expected: int,
                       budget: _RetrievalBudget, phase: str) -> bytes | None:
    """Fetch exactly one chunk; charge the entire request before every attempt.

    An interrupted request is charged too, even if no bytes were yielded. The
    SDK's own MTProto retries are opaque to stream_media; its network traffic
    is not a separately enforceable byte counter at this layer.
    """
    budget.phase = phase
    for attempt in range(MAX_CHUNK_RETRIES + 1):
        if budget.remaining < CHUNK_SIZE:
            if attempt:
                raise _MediaTransportError("retry budget exhausted")
            return None
        budget.requested += CHUNK_SIZE
        started = asyncio.get_running_loop().time()
        log.info("media info %s offset=%d attempt=%d requested=%d/%d",
                 phase, offset, attempt + 1, budget.requested, budget.maximum)
        try:
            async def receive():
                data = bytearray()
                # Exhaust the bounded generator normally, releasing Kurigram's
                # underlying transmission semaphore before starting another range.
                async with aclosing(client.stream_media(src_msg, limit=1, offset=offset)) as stream:
                    async for chunk in stream:
                        if not chunk:
                            continue
                        budget.received += len(chunk)
                        if len(data) + len(chunk) > expected:
                            raise _MediaTransportError("unexpected chunk length")
                        data.extend(chunk)
                if len(data) != expected:
                    raise _MediaTransportError("incomplete metadata chunk")
                return bytes(data)

            result = await asyncio.wait_for(receive(), timeout=CHUNK_TIMEOUT_SEC)
            log.info("media info %s offset=%d received=%d elapsed=%.2fs",
                     phase, offset, len(result), asyncio.get_running_loop().time() - started)
            return result
        except Exception as exc:
            log.warning("media info %s offset=%d attempt=%d failed=%s elapsed=%.2fs",
                        phase, offset, attempt + 1, type(exc).__name__,
                        asyncio.get_running_loop().time() - started)
            if not _retryable_stream_error(exc):
                raise
            if attempt >= MAX_CHUNK_RETRIES or budget.remaining < CHUNK_SIZE:
                raise _MediaTransportError("media retrieval retries exhausted") from exc
            await asyncio.sleep(RETRY_DELAY_SEC)
    return None


class _TelegramRangeReader:
    """Small, aligned document ranges through Kurigram 2.2.25's session API.

    Do not restart or replace SDK sessions: a failing media-only connection
    can be bypassed using the regular endpoint for the SAME data centre.
    Requests are serial, and SDK retries are bounded to one attempt per call.
    """
    def __init__(self, client, media):
        from pyrogram import raw
        from pyrogram.file_id import FileId
        fid = FileId.decode(media.file_id)
        self.client = client
        self.raw = raw
        self.dc_id = fid.dc_id
        self.location = raw.types.InputDocumentFileLocation(
            id=fid.media_id, access_hash=fid.access_hash,
            file_reference=fid.file_reference, thumb_size="")
        self.prefer_media = True

    async def fetch(self, index: int, expected: int, budget: _RetrievalBudget,
                    phase: str, size: int = RANGE_SIZE, file_size: int = 0) -> bytes | None:
        budget.phase = phase
        for attempt in range(MAX_CHUNK_RETRIES + 1):
            if budget.remaining < size:
                if attempt:
                    raise _MediaTransportError("range retry budget exhausted")
                return None
            budget.requested += size
            route = self.prefer_media
            started = asyncio.get_running_loop().time()
            log.info("media info %s range=%d size=%d dc=%d route=%s attempt=%d requested=%d/%d",
                     phase, index*RANGE_SIZE, size, self.dc_id,
                     "media" if route else "regular", attempt+1, budget.requested, budget.maximum)
            async def receive():
                async with self.client.get_file_semaphore:
                    session = await self.client.get_session(self.dc_id, is_media=route)
                    result = await session.invoke(self.raw.functions.upload.GetFile(
                        location=self.location, offset=index*RANGE_SIZE, limit=size,
                        cdn_supported=False), retries=1, timeout=RANGE_RPC_TIMEOUT_SEC,
                        sleep_threshold=0, retry_delay=0)
                    if not isinstance(result, self.raw.types.upload.File):
                        raise _MediaTransportError("unexpected Telegram range response")
                    data = result.bytes
                    budget.received += len(data)
                    if len(data) != expected:
                        raise _MediaTransportError("incomplete Telegram range")
                    return data
            try:
                data = await asyncio.wait_for(receive(), RANGE_ATTEMPT_TIMEOUT_SEC)
                log.info("media info %s range=%d received=%d elapsed=%.2fs", phase,
                         index*RANGE_SIZE, len(data), asyncio.get_running_loop().time()-started)
                return data
            except Exception as exc:
                log.warning("media info %s range=%d route=%s failed=%s elapsed=%.2fs", phase,
                            index*RANGE_SIZE, "media" if route else "regular", type(exc).__name__,
                            asyncio.get_running_loop().time()-started)
                if not _retryable_stream_error(exc):
                    raise
                if attempt >= MAX_CHUNK_RETRIES or budget.remaining < RANGE_SIZE:
                    raise _MediaTransportError("Telegram range retries exhausted") from exc
                self.prefer_media = not route
                if size > RANGE_SIZE:
                    size = RANGE_SIZE
                    expected = min(size, file_size - index*RANGE_SIZE)
                await asyncio.sleep(RETRY_DELAY_SEC)
        return None


def _range_reader(client, media):
    # Older SDKs and local test transports retain the bounded stream interface.
    if not getattr(media, "file_id", None) or not callable(getattr(client, "get_session", None)):
        return None
    return _TelegramRangeReader(client, media)


def _mp4_head_may_contain_moov(head: bytes) -> bool:
    """Check the next top-level atom; do not grow a head blocked by a large mdat."""
    pos = 0
    for _kind, _, _, end in _iter_mp4_boxes(head):
        pos = end
    if pos + 8 > len(head):
        return True
    return head[pos + 4:pos + 8] != b"mdat"


# ------------------------------------------------------------------ global coordinator
class MediaInspector:
    """Enforces single-flight execution, per-user dedup, byte budget and deadline."""

    def __init__(self, max_bytes: int = MAX_TOTAL_BYTES, timeout_sec: float = INSPECT_TIMEOUT_SEC) -> None:
        self.max_bytes = min(MAX_TOTAL_BYTES, max(0, int(max_bytes)))
        self.timeout_sec = float(timeout_sec)
        self._busy = False
        self._users: set[int] = set()

    @property
    def busy(self) -> bool:
        return self._busy

    async def inspect(self, client, uid: int, chat_id: int, message_id: int,
                      fallback_name: str = "", fallback_size: int = 0) -> tuple[bool, str, MediaReport | None]:
        if uid in self._users:
            return False, "Your previous Media Information check is still running. Please wait a moment.", None
        if self._busy:
            return False, "Another Media Information check is currently running. Please try again after it finishes.", None
        self._busy = True
        self._users.add(uid)
        budget = _RetrievalBudget(self.max_bytes)
        started = asyncio.get_running_loop().time()
        outcome = "failed"
        log.info("media info start ch=%s mid=%s timeout=%.1fs budget=%d",
                 chat_id, message_id, self.timeout_sec, self.max_bytes)
        try:
            report = await asyncio.wait_for(
                self._inspect_inner(client, chat_id, message_id, fallback_name, fallback_size, budget),
                timeout=self.timeout_sec,
            )
            if report is None:
                outcome = "metadata unavailable"
                return False, (
                    "Metadata could not be retrieved within the partial-download limit "
                    f"({human_size(self.max_bytes)}). Full-file downloads are disabled to keep the bot fast."
                ), None
            outcome = "success"
            if report.duration is None:
                outcome = "partial metadata"
                return True, format_media_report(report) + "\n\nDuration could not be verified.", report
            return True, format_media_report(report), report
        except asyncio.TimeoutError:
            outcome = "timeout"
            if budget.partial is not None:
                budget.partial.bytes_fetched = budget.received
                outcome = "partial timeout"
                return True, format_media_report(budget.partial) + "\n\nMetadata inspection incomplete (timeout).", budget.partial
            if budget.phase in ("head", "tail", "targeted"):
                return False, "Media Information timed out while retrieving data from Telegram. Please try again later.", None
            return False, "Media Information timed out before metadata could be retrieved. Please try again later.", None
        except _MediaTransportError:
            outcome = "transport failure"
            if budget.partial is not None:
                budget.partial.bytes_fetched = budget.received
                outcome = "partial transport failure"
                return True, format_media_report(budget.partial) + "\n\nMetadata inspection incomplete (connection failure).", budget.partial
            return False, "Telegram's media connection did not return the required metadata after retrying. Please try again later.", None
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as e:
            log.warning("media info inspection failed ch=%s mid=%s: %s", chat_id, message_id, type(e).__name__)
            return False, "Could not inspect this file's metadata right now.", None
        finally:
            log.info("media info end ch=%s mid=%s outcome=%s phase=%s received=%d requested=%d elapsed=%.2fs",
                     chat_id, message_id, outcome, budget.phase, budget.received, budget.requested,
                     asyncio.get_running_loop().time() - started)
            self._users.discard(uid)
            self._busy = False

    async def _inspect_inner(self, client, chat_id: int, message_id: int,
                             fallback_name: str, fallback_size: int,
                             budget: _RetrievalBudget) -> MediaReport | None:
        src_msg = await client.get_messages(chat_id, message_id)
        if not src_msg or getattr(src_msg, "empty", False):
            return None
        media = getattr(src_msg, "document", None) or getattr(src_msg, "video", None) or getattr(src_msg, "audio", None)
        if not media:
            return None
        file_name = (getattr(media, "file_name", None) or fallback_name or "").strip()
        actual_size = int(getattr(media, "file_size", 0) or 0)
        file_size = actual_size or int(fallback_size or 0)
        ext = ("." + file_name.rsplit(".", 1)[-1].lower()) if "." in file_name else ".bin"
        if len(ext) > 6:
            ext = ".bin"
        reader = _range_reader(client, media)
        if reader and actual_size:
            return await _inspect_ranges(reader, file_name, actual_size, budget)
        chunk_size = RANGE_SIZE if reader else CHUNK_SIZE
        head_max = HEAD_CHUNKS_MAX * CHUNK_SIZE // chunk_size
        tail_max = TAIL_CHUNKS_MAX * CHUNK_SIZE // chunk_size
        if file_size <= chunk_size or budget.remaining < chunk_size:
            return None
        total_chunks = (file_size + chunk_size - 1) // chunk_size
        head_count = tail_count = 0
        head = tail = b""
        log.info("media info source size=%d chunks=%d extension=%s transport=%s", file_size, total_chunks, ext, "range" if reader else "stream")

        async def grow_head() -> bool:
            nonlocal head, head_count
            # Always leave at least one source chunk unread, even for small files.
            if head_count + tail_count >= total_chunks - 1 or head_count >= head_max:
                return False
            part = (await reader.fetch(head_count, chunk_size, budget, "head") if reader else
                    await _fetch_chunk(client, src_msg, head_count, chunk_size, budget, "head"))
            if part is None:
                return False
            head += part
            head_count += 1
            return True

        async def grow_tail() -> bool:
            nonlocal tail, tail_count
            if head_count + tail_count >= total_chunks - 1 or tail_count >= tail_max:
                return False
            index = total_chunks - tail_count - 1
            expected = min(chunk_size, file_size - index * chunk_size)
            # Negative offsets require the actual Telegram media size; a known
            # indexed fallback size can instead be used with a positive offset.
            offset = -tail_count - 1 if actual_size else index
            part = (await reader.fetch(index, expected, budget, "tail") if reader else
                    await _fetch_chunk(client, src_msg, offset, expected, budget, "tail"))
            if part is None:
                return False
            tail = part + tail
            tail_count += 1
            return True

        async def probe(data: bytes, suffix: str) -> MediaReport | None:
            budget.phase = "container probe"
            rep = await _probe_buffer(data, suffix, file_name, file_size)
            if rep is not None:
                rep.bytes_fetched = budget.received
            return rep

        if not await grow_head():
            return None
        rep = await probe(head, ext)
        if rep is not None:
            return rep

        if _is_mp4_header(head) or ext in (".mp4", ".m4v", ".mov"):
            # Faststart moov may span multiple chunks. If an incomplete mdat
            # blocks the head, skip directly to the tail instead.
            while _mp4_head_may_contain_moov(head) and await grow_head():
                rep = await probe(head, ext)
                if rep is not None:
                    return rep
            ftyp = _extract_mp4_box(head, b"ftyp") or b""
            while await grow_tail():
                moov = _extract_mp4_box(tail, b"moov")
                if moov:
                    rep = await probe(ftyp + moov, ".mp4")
                    if rep is not None:
                        return rep
            return None

        if _is_mkv_header(head) or ext in (".mkv", ".webm"):
            while await grow_head():
                rep = await probe(head, ext)
                if rep is not None:
                    return rep
        return None


async def _inspect_ranges(reader, name, size, budget):
    """Walk declared container offsets; fetch aligned ranges only as needed."""
    cache = {}
    preferred = RANGE_SIZE
    # Keep one aligned block unread, even if metadata spans the entire file.
    max_blocks = (size + RANGE_SIZE - 1) // RANGE_SIZE - 1
    async def read(pos, length):
        nonlocal preferred
        if pos < 0 or length < 0 or pos + length > size or length > MAX_TOTAL_BYTES:
            return None
        end = pos + length
        parts = []
        while pos < end:
            index = pos // RANGE_SIZE
            if index not in cache:
                # Telegram requires a power-of-two divisor of 1 MiB and no
                # crossing of its 1 MiB boundaries for non-precise requests.
                amount = preferred
                while (index*RANGE_SIZE % amount or amount > budget.remaining or
                       any(i in cache for i in range(index, index+amount//RANGE_SIZE)) or
                       len(cache)+amount//RANGE_SIZE > max_blocks):
                    amount //= 2
                    if amount < RANGE_SIZE:
                        return None
                data = await reader.fetch(index, min(amount, size-index*RANGE_SIZE), budget,
                                          'targeted', size=amount, file_size=size)
                if not data:
                    return None
                for off in range(0,len(data),RANGE_SIZE):
                    cache[index+off//RANGE_SIZE] = data[off:off+RANGE_SIZE]
                preferred = min(CHUNK_SIZE, amount*2) if len(data)==amount else RANGE_SIZE
            block = cache[index]
            start = pos % RANGE_SIZE
            take = min(end-pos, len(block)-start)
            if take <= 0:
                return None
            parts.append(block[start:start+take]); pos += take
        return b''.join(parts)

    async def ebml(pos, limit):
        raw = await read(pos, min(12, limit-pos)) if pos < limit else None
        if not raw: return None
        ident = _read_vint(raw,0,True)
        if not ident: return None
        val = _read_vint(raw,ident[1])
        if not val: return None
        eid, start = ident[0], pos+val[1]
        unknown = val[0] == (1 << (7*(val[1]-ident[1]))) - 1
        end = limit if unknown else start+val[0]
        if end > limit or end <= pos: return None
        return eid,start,end,unknown

    head = await read(0,min(16,size))
    if not head: return None
    if _is_mp4_header(head):
        pos = 0
        for _ in range(256):
            if pos+8 > size: break
            raw = await read(pos,min(16,size-pos))
            if not raw: break
            n,kind = struct.unpack_from('>I4s',raw)
            hdr = 8
            if n == 1:
                if len(raw)<16: break
                n=struct.unpack_from('>Q',raw,8)[0]; hdr=16
            elif n == 0: n=size-pos
            if n<hdr or pos+n>size: break
            if kind == b'moov':
                log.info('media info targeted MP4 moov offset=%d size=%d',pos,n)
                data=await read(pos,n)
                if data:
                    rep=await _probe_buffer(data,'.mp4',name,size)
                    if rep: rep.bytes_fetched=budget.received
                    return rep
                break
            pos+=n
        return None
    if not _is_mkv_header(head): return None
    pos=0; segment=None
    for _ in range(16):
        item=await ebml(pos,size)
        if not item: break
        eid,start,end,unknown=item
        if eid==0x18538067:
            segment=(start,end); break
        pos=end
    if segment is None: return None
    base,limit=segment; pos=base; info=tracks=None; seeks={}; visited=set()
    def report():
        if tracks is None: return None
        # Rebuild only complete, validated metadata elements, not media payload.
        payload=(info or b'')+tracks
        synthetic=b'\x1a\x45\xdf\xa3\x80\x18\x53\x80\x67\xff'+payload
        rep=parse_mkv_bytes(synthetic,name,size)
        if rep: rep.bytes_fetched=budget.received
        return rep
    for _ in range(256):
        if pos in visited or pos>=limit: break
        visited.add(pos)
        item=await ebml(pos,limit)
        if not item: break
        eid,start,end,unknown=item
        if eid in (0x1549A966,0x1654AE6B,0x114D9B74) and not unknown:
            log.info('media info targeted MKV element=%x offset=%d size=%d',eid,pos,end-pos)
            data=await read(pos,end-pos)
            if data is None: break
            if eid==0x1549A966: info=data
            elif eid==0x1654AE6B: tracks=data
            else:
                for key,a,b,complete in _iter_ebml(data,start-pos,len(data)):
                    if key!=0x4DBB or not complete: continue
                    target=offset=None
                    for k,c,d,ok in _iter_ebml(data,a,b):
                        if not ok: continue
                        if k==0x53AB: target=_ebml_uint(data[c:d])
                        elif k==0x53AC: offset=_ebml_uint(data[c:d])
                    if target in (0x1549A966,0x1654AE6B) and offset is not None and base+offset<limit:
                        seeks[target]=base+offset
            rep=report()
            if rep:
                budget.partial=rep
                if info is not None: return rep
        # Validate SeekHead targets before trusting them; otherwise walk sizes.
        next_pos=end
        for target,missing in ((0x1549A966,info is None),(0x1654AE6B,tracks is None)):
            candidate=seeks.pop(target,None) if missing else None
            if candidate is not None and candidate not in visited:
                check=await ebml(candidate,limit)
                if check and check[0]==target:
                    next_pos=candidate; break
        if unknown and next_pos==end: break
        if eid == 0x1F43B675 and tracks is not None and next_pos == end:
            break
        pos=next_pos
    return report()
