"""Turn messy Telegram filenames into structured data.

    "Adam.2019.720p.WEBRip.800MB.x264-GalaxyRG.mkv"
 -> title "Adam", year 2019, 720p, WEBRip, x264, encoder "GalaxyRG", MKV

Everything after the first "technical" token (year, resolution, source,
codec, season marker) is metadata; everything before it is the title.
The encoder/uploader is a best guess (see _pick_group) and is simply left
blank when the filename gives no clear sign of one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from normalize import normalize

VIDEO_EXTS = {"mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp", "ts", "vob", "divx"}
SUB_EXTS = {"srt", "ass", "ssa", "vtt", "sub"}
_STRIP_EXTS = VIDEO_EXTS | SUB_EXTS | {"zip", "rar", "7z", "nfo", "txt", "jpg", "png", "idx"}
_EXT = re.compile(r"\.([A-Za-z0-9]{2,4})$")
_LEAD_AT = re.compile(r"^\s*@\w+\s*[-–—_.:|]*\s*")
_LEAD_AT_UNDERSCORE = re.compile(r"^\s*@[^_\s.]+[_\-:|\s.]*")   # "@Handle_Some_Title_2019": handle = first piece only
# A site at the very start is promo only if it looks like one: a www./http prefix, or a plain
# domain followed by a SPACED dash/colon ("Site.com - Title"). Dotted title words never match.
_LEAD_SITE = re.compile(
    r"^\s*(?:(?:https?://)?www\.\S+?|[\w-]+\.(?:com|org|info|xyz|cc|site|in|ng|io|net))"
    r"(?:\s+[-–—|:]\s+|\s*:\s+)", re.I)
_LEAD_BRACKET = re.compile(r"^\s*[\[\(\{]([^\]\)\}]{0,60})[\]\)\}]\s*[-–—_.:|]*\s*")
_BRACKET_YEAR = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
_BRACKET_ANY = re.compile(r"[\[\(\{]([^\]\)\}]+)[\]\)\}]")
_TLDS = {"com", "net", "org", "info", "in", "cc", "io", "tv", "me", "to", "co", "ng", "xyz", "site", "club", "world"}
_DOMAIN_FULL = re.compile(r"(?:www\.)?[\w-]+\.(?:" + "|".join(sorted(_TLDS)) + r")", re.I)
_HANDLE = re.compile(r"[-–—_.\s]*@\w+")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_SXXEYY = re.compile(r"^s(\d{1,2})[ ._-]?e(\d{1,3})(?:-?e?(\d{1,3}))?$", re.I)
_SONLY = re.compile(r"^s(\d{1,2})$", re.I)
_NXNN = re.compile(r"^(\d{1,2})x(\d{2,3})$", re.I)
_RES = re.compile(r"^(2160|1440|1080|720|576|480|360|240)p$")
_DOT = "\x03"    # stands in for the dot inside sizes like 1.4GB while we split on dots
_SIZE = re.compile(r"^\d+([.\x03]\d+)?(kb|mb|gb|tb)$", re.I)
_TLD_WORDS = re.compile(r"(?i)^([\w-]+)\s+(com|net|org|cc|xyz|info|site|club|world|in)$")
_BR = "\x00"      # bracketed year marker
_REF = "\x01"     # bracketed text placeholder

_SOURCES = {
    "webdl": "WEB-DL", "web": "WEB", "webrip": "WEBRip", "bluray": "BluRay",
    "bdrip": "BDRip", "brrip": "BRRip", "hdrip": "HDRip", "hdtv": "HDTV",
    "dvdrip": "DVDRip", "dvdscr": "DVDScr", "hdcam": "CAM", "cam": "CAM",
    "camrip": "CAM", "hdts": "TS", "predvd": "PreDVD", "remux": "Remux",
}
_SOURCE_RANK = {"CAM": 0, "TS": 1, "PreDVD": 1, "DVDScr": 2, "HDTV": 3, "HDRip": 4,
                "DVDRip": 4, "WEBRip": 5, "WEB": 5, "WEB-DL": 6, "BRRip": 6,
                "BDRip": 6, "BluRay": 7, "Remux": 8}
_CODECS = {"x264": "x264", "h264": "x264", "avc": "x264",
           "x265": "x265", "h265": "x265", "hevc": "x265", "xvid": "XviD", "divx": "XviD"}
# Words that end the title but aren't shown on their own.
_OTHER_TECH = {"aac", "ac3", "dts", "atmos", "ddp5", "dd5", "10bit", "hdr", "hdr10", "uhd", "hq",
               "proper", "repack", "extended", "unrated", "imax", "esub", "esubs",
               "hc", "hardsub", "dubbed", "dub", "dual", "multi", "uncut", "remastered"}
# Never an encoder name.
_NOISE = {"mkv", "mp4", "avi", "sub", "subs", "subbed", "audio", "dl", "rip", "web", "hd", "fhd", "sd",
          "dv", "sdr", "8bit", "ddp", "aac2", "truehd", "flac", "opus", "eac3", "nf", "amzn", "dsnp",
          "hmax", "atvp", "hulu", "pcok", "line", "org", "original", "clean", "internal", "limited",
          "criterion", "complete", "full", "movie", "film", "by", "www", "com", "net", "cc", "tv",
          "me", "to", "in", "io", "co", "ng", "xyz", "info", "site", "club", "world", "bluray",
          "hindi", "english", "dubbed", "ms", "mb", "gb", "esub", "esubs", "cut", "directors", "aud", "auds",
          "multi", "dual", "h", "mal"}
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
_TAGS = {"dual": "Dual Audio", "multi": "Multi Audio", "esub": "ESub", "esubs": "ESub", "sub": "ESub",
         "subs": "ESub", "subbed": "ESub", "hdr": "HDR", "hdr10": "HDR", "10bit": "10-bit",
         "extended": "Extended", "uncut": "Uncut", "remastered": "Remastered", "imax": "IMAX",
         "dubbed": "Dubbed", "dub": "Dubbed"}
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
    group: str | None = None        # encoder / uploader, best guess
    tags: tuple[str, ...] = ()      # Dubbed, Dual Audio, ESub, HDR, ...
    ext: str | None = None          # MKV, MP4, SRT, ...

    @property
    def norm_title(self) -> str:
        return normalize(self.title)

    @property
    def is_cam(self) -> bool:
        return self.source in ("CAM", "TS", "PreDVD")

    @property
    def is_sub(self) -> bool:
        return (self.ext or "").lower() in SUB_EXTS

    @property
    def quality_key(self) -> tuple:
        return quality_key_of(self.resolution, self.source)


def _valid_year(tok: str) -> bool:
    return bool(_YEAR.match(tok)) and 1888 <= int(tok) <= date.today().year + 1


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
        bare = name.strip()
        if bare.startswith("@") and " " not in bare and bare.count("_") >= 2:
            name = _LEAD_AT_UNDERSCORE.sub("", name)
        else:
            name = _LEAD_AT.sub("", name)
        name = _LEAD_SITE.sub("", name)
        m = _LEAD_BRACKET.match(name)
        if m and not re.search(r"(?:19|20)\d{2}", m.group(1)):
            name = name[m.end():]
        if name == before:
            break
    return name


def _known(low: str) -> bool:
    flat = low.replace("-", "")
    return bool(_RES.match(low) or low in ("4k", "uhd") or flat in _SOURCES or flat in _CODECS
                or flat in _OTHER_TECH or flat in _NOISE or flat in _LANGS or low.isdigit()
                or _SIZE.match(low) or _YEAR.match(low))


def _valid_group(g: str) -> bool:
    g = g.strip()
    return 2 <= len(g) <= 30 and bool(re.search(r"[A-Za-z]", g)) and g.lower() not in _NOISE \
        and not _known(g.lower())


def _clean_group(txt: str) -> str:
    txt = " ".join(txt.split())
    m = _TLD_WORDS.match(txt)
    return f"{m.group(1)}.{m.group(2).lower()}" if m else txt


def _series_tag(toks: list[str], lead_nxnn: bool = True):
    """First season/episode tag anywhere in the tokens: (index, season, episode, tokens used) or None.
    Looks like S01E01, S1E1, S01, 1x01, "Season 2". 'S1m0ne' and 'Se7en' are not tags. A leading '10x10'
    is not treated as one, so the film 10x10 stays a film."""
    for i, tok in enumerate(toks):
        if tok.startswith((_BR, _REF)):
            continue
        low = tok.lower().strip("-")
        m = _SXXEYY.match(low)
        if m:
            return i, int(m.group(1)), int(m.group(2)), 1
        m = _SONLY.match(low)
        if m:
            return i, int(m.group(1)), None, 1
        m = _NXNN.match(low)
        if m and (i > 0 or (lead_nxnn and not any(_valid_year(t.lstrip(_BR)) for t in toks))):
            return i, int(m.group(1)), int(m.group(2)), 1
        if low == "season" and i + 1 < len(toks) and re.fullmatch(r"\d{1,2}", toks[i + 1]):
            return i, int(toks[i + 1]), None, 2
    return None


def looks_like_series(text: str) -> bool:
    """For what people type into the search box: 'friends s01', 'show season 2', 'S01E03'."""
    return _series_tag(normalize(text).split(), lead_nxnn=False) is not None      # a lone "10x10" is a film title


def _is_tech(low: str) -> bool:
    flat = low.replace("-", "")
    return bool(_RES.match(low) or low in ("4k",) or flat in _SOURCES or flat in _CODECS or flat in _OTHER_TECH
                or _SXXEYY.match(low) or _SONLY.match(low) or _NXNN.match(low))


def _parse(raw: str, name: str) -> Parsed:
    name = name.strip()
    ext = None
    m = _EXT.search(name)
    if m and m.group(1).lower() in _STRIP_EXTS:
        ext, name = m.group(1).upper(), name[:m.start()]
    name = _HANDLE.sub(" ", name)                      # "-@Channel", "BRRip@Channel", "[@Channel]": promo, never title
    name = re.sub(r"(?<=\d)\.(?=\d+\s?(?:gb|mb|kb|tb)\b)", _DOT, name, flags=re.I)    # 1.4GB stays one piece
    name = _BRACKET_YEAR.sub(lambda mm: f" {_BR}{mm.group(1)} ", name)
    refs: list[str] = []

    def stash(mm):
        refs.append(mm.group(1).strip())
        return f" {_REF}{len(refs) - 1} "
    name = _BRACKET_ANY.sub(stash, name)
    name = re.sub(r"[._+~]", " ", name)
    toks = [t for t in name.split() if t.strip("-–—")]
    while len(toks) > 1 and toks[-1].lower() in _STRIP_EXTS:          # a literal "mkv" word at the end
        ext = ext or toks[-1].upper()
        toks.pop()
    tag = _series_tag(toks)
    kind, season, episode = ("series", tag[1], tag[2]) if tag else ("movie", None, None)
    if tag and tag[0] == 0 and len(toks) > tag[3]:                                  # "S01E01 Better Call Saul ..."
        toks = toks[tag[3]:]
    lead_year = None
    if len(toks) > 1 and toks[0].startswith(_BR) and _valid_year(toks[0][1:]):      # "(1987) Title ..."
        if _is_tech(toks[1].lower()):                    # "(2007) 720p": the title must be elsewhere
            return Parsed(raw=raw, title="")
        lead_year, toks = int(toks[0][1:]), toks[1:]

    # 1) first technical token after the title's first word ends the title
    tech_idx = None
    for i, tok in enumerate(toks):
        if i == 0 or tok.startswith(_REF):
            continue
        low = tok.lower().strip("-")
        flat = low.replace("-", "")
        if (_SXXEYY.match(low) or _SONLY.match(low) or _NXNN.match(low)
                or (low == "season" and i + 1 < len(toks) and re.fullmatch(r"\d{1,2}", toks[i + 1]))
                or _RES.match(low) or low == "4k" or flat in _SOURCES or flat in _CODECS
                or flat in _OTHER_TECH or _SIZE.match(low)):
            tech_idx = i
            break

    # 2) pick the release year
    strong_idx = next((i for i, t in enumerate(toks) if i > 0 and not t.startswith(_REF) and (
        _RES.match(t.lower()) or t.lower() == "4k" or t.lower().replace("-", "") in _SOURCES
        or t.lower().replace("-", "") in _CODECS or _SXXEYY.match(t) or _SONLY.match(t) or _NXNN.match(t))), None)
    limit = strong_idx if strong_idx is not None else len(toks)
    bracketed = [i for i, t in enumerate(toks) if t.startswith(_BR) and i > 0 and _valid_year(t[1:])]
    plain = [i for i, t in enumerate(toks) if 0 < i < limit and not t.startswith((_BR, _REF)) and _valid_year(t)]
    year_idx = bracketed[-1] if bracketed else (plain[-1] if plain else None)
    year = int(toks[year_idx].lstrip(_BR)) if year_idx is not None else lead_year

    if toks and toks[0].startswith(_BR):            # "(2007) 720p": nothing before the year
        return Parsed(raw=raw, title="")
    ends = [x for x in (tech_idx, year_idx) if x is not None]
    boundary = min(ends) if ends else len(toks)
    title_toks = [t.lstrip(_BR) for t in toks[:boundary] if not t.startswith(_REF)]

    tags: list[str] = []
    langs: list[str] = []

    def add_tag(t: str) -> None:
        if t not in tags:
            tags.append(t)

    def add_lang(l: str) -> None:
        if l not in langs:
            langs.append(l)

    if boundary < len(toks) and toks[boundary].lower() in ("dubbed", "dub") and len(title_toks) > 1 \
            and title_toks[-1].lower() in _LANGS:                         # "Adam Hindi Dubbed 2019"
        add_lang(_LANGS[title_toks.pop().lower()])
    while title_toks and title_toks[-1].lower() in ("dubbed", "dub"):
        title_toks.pop()
        add_tag("Dubbed")
        if title_toks and title_toks[-1].lower() in _LANGS and len(title_toks) > 1:
            add_lang(_LANGS[title_toks.pop().lower()])
    title = " ".join(title_toks).strip(" -–—:")
    if not title and toks and toks[0].startswith(_REF):                  # "[REC] (2007)": the bracket IS the title
        title = refs[int(toks[0][1:])]

    # 3) metadata lives in the tail only, so "Hindi Medium" keeps its title
    resolution = source = codec = None
    group: str | None = None
    unknown: list[str] = []
    tail: list[tuple[str, bool]] = []          # (token, is hyphen-suffix candidate)
    for tok in toks[boundary:]:
        if tok.startswith(_REF):
            seg = refs[int(tok[1:])]
            low = seg.lower()
            words = [w for w in re.split(r"[^a-z0-9]+", low) if w]
            if "." in seg and _DOMAIN_FULL.fullmatch(seg):
                group = re.sub(r"(?i)^www\.", "", seg)
            elif words and all(_known(w) for w in words):
                for w in words:
                    if w in _LANGS:
                        add_lang(_LANGS[w])
                    if w in _TAGS:
                        add_tag(_TAGS[w])
            elif _valid_group(_clean_group(seg)):
                group = _clean_group(seg)
            continue
        low = tok.lstrip(_BR).lower().strip("-")
        if "-" in low and not _known(low):
            parts = [p for p in low.split("-") if p]
            orig = [p for p in tok.lstrip(_BR).strip("-").split("-") if p]
            for j, (p, o) in enumerate(zip(parts, orig)):
                tail.append((o, j == len(parts) - 1 and len(parts) > 1 and _known(parts[0])))
        else:
            tail.append((tok.lstrip(_BR).strip("-"), False))

    # "Pahe" + "in", "www" + "phonofilm" + "net": a site name after the title is promo, so join it up
    merged: list[tuple[str, bool]] = []
    i = 0
    while i < len(tail):
        tok, hy = tail[i]
        nxt = tail[i + 1][0].lower() if i + 1 < len(tail) else ""
        if nxt in _TLDS and re.search(r"[A-Za-z]", tok) and not _known(tok.lower()) and tok.lower() != "www":
            if merged and merged[-1][0].lower() == "www":
                merged.pop()
            merged.append((f"{tok}.{nxt}", True))
            i += 2
        else:
            merged.append((tok, hy))
            i += 1
    tail = merged

    hy_group = None
    for idx, (tok, hy) in enumerate(tail):
        low = tok.lower()
        flat = low.replace("-", "")
        if resolution is None and (_RES.match(low) or low in ("4k", "uhd")):
            resolution = "2160p" if low in ("4k", "uhd") else low
        elif source is None and flat in _SOURCES:
            source = _SOURCES[flat]
        elif codec is None and flat in _CODECS:
            codec = _CODECS[flat]
        if flat in _LANGS:
            add_lang(_LANGS[flat])
        if flat in _TAGS:
            add_tag(_TAGS[flat])
        if hy and not _known(low) and _valid_group(tok):
            hy_group = tok
        elif not _known(low):
            unknown.append(tok if idx != len(tail) - 1 else tok + "\x02")   # \x02 = was the last token

    if group is None:
        group = hy_group
    if group is None and unknown and unknown[-1].endswith("\x02"):
        cand = unknown[-1][:-1]
        if _valid_group(cand):
            group = cand
    if group is not None and not _valid_group(group) and not _DOMAIN_FULL.fullmatch(group) \
            and not re.fullmatch(r"[\w-]+\.[a-z]{2,5}", group, re.I):
        group = None

    return Parsed(raw=raw, title=_smart_case(title), year=year, kind=kind,
                  season=season, episode=episode, resolution=resolution,
                  source=source, codec=codec, languages=tuple(langs), group=group,
                  tags=tuple(tags), ext=ext)


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
            return Parsed(**{**alt.__dict__, "ext": alt.ext or parsed.ext})
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
             languages: tuple[str, ...] | list[str] = (), codec: str | None = None) -> str:
    """One-line summary: '1080p · WEB-DL · x265 · 1.8 GB · Hindi'."""
    bits = [resolution, source, codec, human_size(size), ", ".join(languages) or None]
    return " · ".join(b for b in bits if b)


def detail_lines(res, src, codec, size, langs, tags, ext, group) -> tuple[str, str]:
    """Two lines for a version: quality/size, then language, tags, format, encoder."""
    if (ext or "").lower() in SUB_EXTS:
        first = " · ".join(b for b in ("Subtitles", ext.upper(), human_size(size)) if b)
    else:
        first = " · ".join(b for b in (res, src, codec, human_size(size)) if b)
    second = " · ".join(b for b in (", ".join(langs) or None, *tags,
                                    ext.upper() if ext and (ext.lower() not in SUB_EXTS) else None,
                                    f"by {group}" if group else None) if b)
    return first, second


def button_label(res, src, codec, size, langs, tags, ext, group) -> str:
    """One line for a version button, most important first so a cut-off end loses only the encoder:
    '895 MB · 720p · BluRay · x265 · Hindi · Dubbed · MKV · GalaxyRG'."""
    if (ext or "").lower() in SUB_EXTS:
        bits = ["Subtitles", ext.upper(), human_size(size) if size else None, ", ".join(langs) or None, group]
    else:
        bits = [human_size(size) if size else None, res, src, codec, ", ".join(langs) or None, *tags,
                ext.upper() if ext else None, group]
    return " · ".join(b for b in bits if b)


def variant_label(p: Parsed, size: int | None) -> str:
    return label_of(p.resolution, p.source, size, p.languages, p.codec)
