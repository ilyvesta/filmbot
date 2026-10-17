"""Turn messy Telegram filenames into structured data.

    "[Site.com] - Spider-Man.No.Way.Home.(2021).1080p.WEB-DL.Hindi-English.mkv"
 -> title "Spider-Man No Way Home", year 2021, 1080p, WEB-DL, hindi+english

Everything after the first "technical" token (year, resolution, source,
codec, season marker) is metadata; everything before it is the title.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from normalize import normalize

_EXT = re.compile(r"\.(mkv|mp4|avi|mov|wmv|flv|webm|m4v|mpg|mpeg|3gp|zip|rar|srt)$", re.I)
_LEAD_AT = re.compile(r"^\s*@\w+\s*[-–—_.:|]*\s*")
_LEAD_SITE = re.compile(
    r"^\s*(?:(?:https?://)?www\.\S+?|[\w-]+\.(?:com|net|org|io|co|tv|me|cc|to|in|ng|xyz|info|site|club|world))"
    r"\s*[-–—|:]\s*", re.I)
_LEAD_BRACKET = re.compile(r"^\s*[\[\(\{]([^\]\)\}]{0,60})[\]\)\}]\s*[-–—_.:|]*\s*")
_BRACKET_YEAR = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_SXXEYY = re.compile(r"^s(\d{1,2})[ ._-]?e(\d{1,3})(?:-?e?(\d{1,3}))?$", re.I)
_SONLY = re.compile(r"^s(\d{1,2})$", re.I)
_NXNN = re.compile(r"^(\d{1,2})x(\d{2,3})$", re.I)
_RES = re.compile(r"^(2160|1440|1080|720|576|480|360|240)p$")
_BR_MARK = "\x00"

_SOURCES = {
    "webdl": "WEB-DL", "web": "WEB", "webrip": "WEBRip", "bluray": "BluRay",
    "bdrip": "BDRip", "brrip": "BRRip", "hdrip": "HDRip", "hdtv": "HDTV",
    "dvdrip": "DVDRip", "dvdscr": "DVDScr", "hdcam": "CAM", "cam": "CAM",
    "camrip": "CAM", "hdts": "TS", "predvd": "PreDVD", "remux": "Remux",
}
_SOURCE_RANK = {"CAM": 0, "TS": 1, "PreDVD": 1, "DVDScr": 2, "HDTV": 3, "HDRip": 4,
                "DVDRip": 4, "WEBRip": 5, "WEB": 5, "WEB-DL": 6, "BRRip": 6,
                "BDRip": 6, "BluRay": 7, "Remux": 8}
_CODECS = {"x264": "H.264", "h264": "H.264", "avc": "H.264",
           "x265": "HEVC", "h265": "HEVC", "hevc": "HEVC", "xvid": "XviD", "divx": "XviD"}
# Not shown to users, but they end the title just like codecs do.
_OTHER_TECH = {"aac", "ac3", "dts", "atmos", "ddp5", "dd5", "10bit", "hdr", "uhd", "hq",
               "proper", "repack", "extended", "unrated", "imax", "esub", "esubs",
               "hc", "hardsub", "dubbed", "dual", "multi"}
_LANGS = {
    "english": "English", "eng": "English", "hindi": "Hindi", "hin": "Hindi",
    "hausa": "Hausa", "yoruba": "Yoruba", "igbo": "Igbo", "pidgin": "Pidgin",
    "french": "French", "fre": "French", "fra": "French", "spanish": "Spanish",
    "spa": "Spanish", "portuguese": "Portuguese", "arabic": "Arabic",
    "korean": "Korean", "kor": "Korean", "japanese": "Japanese", "jpn": "Japanese",
    "chinese": "Chinese", "mandarin": "Chinese", "tamil": "Tamil", "telugu": "Telugu",
    "malayalam": "Malayalam", "kannada": "Kannada", "bengali": "Bengali",
    "urdu": "Urdu", "punjabi": "Punjabi", "russian": "Russian", "german": "German",
    "italian": "Italian", "turkish": "Turkish", "swahili": "Swahili",
    "amharic": "Amharic", "zulu": "Zulu", "xhosa": "Xhosa", "afrikaans": "Afrikaans",
    "twi": "Twi", "akan": "Akan",
}
_SMALL = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "vs", "with"}
_RES_PX = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720,
           "576p": 576, "480p": 480, "360p": 360, "240p": 240}


def quality_key_of(resolution: str | None, source: str | None) -> tuple:
    """Sort key; the biggest value is the best version: (not-cam, resolution, source)."""
    return (source not in ("CAM", "TS", "PreDVD"), _RES_PX.get(resolution or "", 0),
            _SOURCE_RANK.get(source or "", 4))


@dataclass(frozen=True)
class Parsed:
    raw: str
    title: str                      # display title, "" if nothing usable
    year: int | None = None
    kind: str = "movie"             # "movie" | "series"
    season: int | None = None
    episode: int | None = None
    resolution: str | None = None
    source: str | None = None
    codec: str | None = None
    languages: tuple[str, ...] = ()

    @property
    def norm_title(self) -> str:
        return normalize(self.title)

    @property
    def is_cam(self) -> bool:
        return self.source in ("CAM", "TS", "PreDVD")

    @property
    def quality_key(self) -> tuple:
        return quality_key_of(self.resolution, self.source)


def _max_year() -> int:
    return date.today().year + 1


def _valid_year(tok: str) -> bool:
    return bool(_YEAR.match(tok)) and 1888 <= int(tok) <= _max_year()


def _smart_case(title: str) -> str:
    """Fix ALL-CAPS and all-lowercase names; leave mixed case alone."""
    if title != title.lower() and title != title.upper():
        return title
    words = []
    for i, w in enumerate(title.split()):
        lw = w.lower()
        if i > 0 and lw in _SMALL:
            words.append(lw)
        else:
            words.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
    return " ".join(words)


def _strip_promo(name: str) -> str:
    for _ in range(3):
        before = name
        name = _LEAD_AT.sub("", name)
        name = _LEAD_SITE.sub("", name)
        m = _LEAD_BRACKET.match(name)
        if m and not re.search(r"(?:19|20)\d{2}", m.group(1)):
            name = name[m.end():]
        if name == before:
            break
    return name


def _parse(raw: str, name: str) -> Parsed:
    name = _EXT.sub("", name.strip())
    name = _BRACKET_YEAR.sub(lambda m: f" {_BR_MARK}{m.group(1)} ", name)
    name = re.sub(r"[._+\[\](){}~]", " ", name)
    toks = [t for t in name.split() if t.strip("-–—")]

    # 1) first technical token after the title's first word ends the title
    tech_idx = season = episode = None
    kind = "movie"
    for i, tok in enumerate(toks):
        if i == 0:
            continue
        low = tok.lower().strip("-")
        flat = low.replace("-", "")
        m = _SXXEYY.match(low)
        if m:
            kind, season, episode = "series", int(m.group(1)), int(m.group(2))
        elif _SONLY.match(low):
            kind, season = "series", int(_SONLY.match(low).group(1))
        elif _NXNN.match(low):
            m = _NXNN.match(low)
            kind, season, episode = "series", int(m.group(1)), int(m.group(2))
        elif low == "season" and i + 1 < len(toks) and toks[i + 1].isdigit():
            kind, season = "series", int(toks[i + 1])
        elif (_RES.match(low) or low in ("4k",) or flat in _SOURCES or flat in _CODECS
              or flat in _OTHER_TECH):
            pass
        else:
            continue
        tech_idx = i
        break

    # 2) pick the release year
    limit = tech_idx if tech_idx is not None else len(toks)
    bracketed = [i for i, t in enumerate(toks)
                 if t.startswith(_BR_MARK) and i > 0 and _valid_year(t[1:])]
    plain = [i for i, t in enumerate(toks)
             if 0 < i < limit and not t.startswith(_BR_MARK) and _valid_year(t)]
    year_idx = bracketed[-1] if bracketed else (plain[-1] if plain else None)
    year = int(toks[year_idx].lstrip(_BR_MARK)) if year_idx is not None else None

    if toks and toks[0].startswith(_BR_MARK):      # "(2007) 720p": nothing before the year
        return Parsed(raw=raw, title="")
    ends = [x for x in (tech_idx, year_idx) if x is not None]
    boundary = min(ends) if ends else len(toks)
    title_toks = [t.lstrip(_BR_MARK) for t in toks[:boundary]]
    title = " ".join(title_toks).strip(" -–—:")

    # 3) metadata lives in the tail only, so "Hindi Medium" keeps its title
    resolution = source = codec = None
    langs: list[str] = []
    for tok in toks[boundary:]:
        low = tok.lstrip(_BR_MARK).lower().strip("-")
        flat = low.replace("-", "")
        if resolution is None and (_RES.match(low) or low in ("4k", "uhd")):
            resolution = "2160p" if low in ("4k", "uhd") else low
        elif source is None and flat in _SOURCES:
            source = _SOURCES[flat]
        elif codec is None and flat in _CODECS:
            codec = _CODECS[flat]
        for part in re.split(r"[-/&,+]", low):
            lang = _LANGS.get(part)
            if lang and lang not in langs:
                langs.append(lang)

    return Parsed(raw=raw, title=_smart_case(title), year=year, kind=kind,
                  season=season, episode=episode, resolution=resolution,
                  source=source, codec=codec, languages=tuple(langs))


def parse_filename(raw: str) -> Parsed:
    """Parse one filename. Falls back to the unstripped name if cleaning ate the title
    (e.g. '[REC] (2007)')."""
    parsed = _parse(raw, _strip_promo(raw))
    if not parsed.title:
        parsed = _parse(raw, raw)
    return parsed


_JUNK = re.compile(r"^(video|document|file|audio|img|vid|movie)[\s_-]*\d*$", re.I)


def is_junk(title: str) -> bool:
    """Generic names like 'Video 123' carry no title information."""
    return not title or bool(_JUNK.match(title))


def parse_best(file_name: str | None, caption: str | None = None) -> Parsed:
    """Use the filename; if it's junk ('video_123.mp4'), try the caption's first line."""
    parsed = parse_filename(file_name or "")
    if is_junk(parsed.title) and caption:
        first = caption.strip().splitlines()[0] if caption.strip() else ""
        alt = parse_filename(first)
        if alt.title:
            return alt
    return parsed


def human_size(n: int | None) -> str:
    if not n:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.0f} {unit}" if size >= 100 else f"{size:.1f} {unit}"
        size /= 1024
    return "?"


def label_of(resolution: str | None, source: str | None, size: int | None,
             languages: tuple[str, ...] | list[str] = ()) -> str:
    """'1080p · WEB-DL · 1.8 GB · Hindi, English' — what users see instead of a filename."""
    bits = [resolution, source, human_size(size), ", ".join(languages) or None]
    return " · ".join(b for b in bits if b)


def variant_label(p: Parsed, size: int | None) -> str:
    return label_of(p.resolution, p.source, size, p.languages)
