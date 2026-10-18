"""Everything the user sees: plain functions that return (html_text, button_rows).
No Telegram imports here, so it's all unit-testable. Callback data stays well
under Telegram's 64-byte limit."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

from parse import detail_lines

PAGE_FILMS = 6
PAGE_VARIANTS = 6


@dataclass(frozen=True)
class Btn:
    text: str
    data: str | None = None
    url: str | None = None


Rows = list[list[Btn]]

DEFAULT_WELCOME = (
    "<b>Welcome, {name}.</b>\n\n"
    "Type the name of any film and I'll find it. Every result shows the resolution, size, language and "
    "who encoded it before you download anything.\n\n"
    "<b>How it works</b>\n"
    "1. Send a title. Adding the year helps (<i>heat 1995</i>).\n"
    "2. Choose the film, then the version you want.\n"
    "3. Forward the file to your Saved Messages. It deletes itself after {mins} minutes.\n\n"
    "Tap <b>Discover</b> to browse instead of searching.")

DEFAULT_ABOUT = (
    "<b>About</b>\n\n"
    "A film library you can search by name: {films} films and subtitles so far.\n\n"
    "Versions are sorted best quality first and labelled honestly. Language, dubbing, codec and "
    "encoder are shown before you download, not after.\n\n"
    "Files are sent to you privately and removed after {mins} minutes.")

HELP = (
    "<b>How it works</b>\n\n"
    "<b>Searching</b>\n"
    "Send a film name. Spelling doesn't have to be perfect and symbols don't matter. "
    "Add the year if a title has remakes.\n\n"
    "<b>Reading the results</b>\n"
    "Each version shows resolution, source, codec and size, then language, format and the encoder "
    "when the file names one.\n"
    "• <b>x265</b> files are smaller, but some built-in players can't play them. Use VLC or MX Player.\n"
    "• <b>Dubbed</b> means the original audio may not be included.\n"
    "• <b>SRT</b> files are subtitles only.\n\n"
    "<b>Getting the file</b>\n"
    "Files delete themselves after {mins} minutes. Forward it to Saved Messages and download it there.\n\n"
    "<b>Something wrong?</b>\n"
    "Tap <b>Report a problem</b> under the file. If a film isn't here, tap <b>Request</b> and "
    "I'll message you when it arrives.")

WARMING = "Waking up, give me a minute and send that again."
EXPIRED = "That search expired. Send the name again."
NOT_YOURS = "That's someone else's search. Send the name yourself."
EMPTY_QUERY = "Send a film name (letters or numbers, not just symbols)."
BANNED = "You can't use this bot."

REASONS = [("l", "Wrong language"), ("d", "Dubbed, not original"), ("a", "No sound / out of sync"),
           ("f", "Wrong film"), ("p", "Won't play"), ("q", "Bad quality"),
           ("s", "Subtitle problem"), ("o", "Something else")]
REASON_NAMES = dict(REASONS)


def fill(text: str, **vals) -> str:
    for k, v in vals.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def pages_of(n: int, size: int) -> int:
    return max(1, -(-n // size))


def pager(page: int, pages: int, prefix: str) -> list[Btn]:
    if pages <= 1:
        return []
    row: list[Btn] = []
    if page > 0:
        row += [Btn("«", f"{prefix}:0"), Btn("‹", f"{prefix}:{page - 1}")]
    row.append(Btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row += [Btn("›", f"{prefix}:{page + 1}"), Btn("»", f"{prefix}:{pages - 1}")]
    return row


def home_screen(s: dict, name: str, films: int) -> tuple[str, Rows]:
    text = fill(s.get("welcome") or DEFAULT_WELCOME, name=escape(name or "there"), mins=s["autodelete_min"] or "a few",
                films=f"{films:,}")
    rows: Rows = [[Btn("Discover", "dc"), Btn("How it works", "nav:help"), Btn("About", "nav:about")]]
    links = [Btn(label, url=url) for label, url in s.get("links", [])]
    for i in range(0, len(links), 2):
        rows.append(links[i:i + 2])
    return text, rows


def help_screen(s: dict) -> tuple[str, Rows]:
    return fill(HELP, mins=s["autodelete_min"] or "a few"), [[Btn("‹ Home", "nav:home")]]


def about_screen(s: dict, films: int) -> tuple[str, Rows]:
    return (fill(s.get("about") or DEFAULT_ABOUT, films=f"{films:,}", mins=s["autodelete_min"] or "a few"),
            [[Btn("‹ Home", "nav:home")]])


def discover_screen(s: dict) -> tuple[str, Rows]:
    rows: Rows = []
    if s.get("picks"):
        rows.append([Btn(s["picks_name"], "dl:pick")])
    rows += [[Btn("Popular this week", "dl:pop")], [Btn("Recently added", "dl:new")], [Btn("Close", "dx")]]
    return "<b>Discover</b>\nBrowse instead of searching.", rows


def _notes(corrected: bool, query: str, year_relaxed: bool, year: int | None) -> str:
    out = ""
    if corrected:
        out += f"Showing results for <b>{escape(query)}</b>.\n"
    if year_relaxed and year:
        out += f"Nothing from {year}, showing other years.\n"
    return out


def results_screen(hits: list[tuple[str, str, int]], page: int, query: str, corrected: bool = False,
                   year_relaxed: bool = False, year: int | None = None, suggest: bool = False,
                   header: str | None = None, back: Btn | None = None, requestable: bool = True) -> tuple[str, Rows]:
    """hits: (film id, label, number of files)."""
    pages = pages_of(len(hits), PAGE_FILMS)
    page = min(max(page, 0), pages - 1)
    if header:
        head = f"<b>{escape(header)}</b>\n"
    else:
        head = ("<b>Did you mean one of these?</b>\n" if suggest else
                f"<b>{len(hits)} film{'s' if len(hits) != 1 else ''}</b> for “{escape(query)}”\n")
    text = _notes(corrected and not suggest and not header, query, year_relaxed, year) + head
    rows: Rows = []
    for fid, label, n in hits[page * PAGE_FILMS:(page + 1) * PAGE_FILMS]:
        rows.append([Btn(f"{label}  ·  {n}" if n > 1 else label, f"f:{fid}")])
    if pager(page, pages, "r"):
        rows.append(pager(page, pages, "r"))
    if back:
        rows.append([back])
    elif requestable:
        rows.append([Btn("Not here? Request it", "rq")])
    return text, rows


def film_screen(title: str, year: int | None, variants: list, page: int, can_back: bool,
                is_admin: bool = False, fid: str = "", note: str = "", mode: str = "",
                picked: bool = False) -> tuple[str, Rows]:
    """variants: FileRec-like objects, best first. mode 'remove' = admin picking a file to delete."""
    pages = pages_of(len(variants), PAGE_VARIANTS)
    page = min(max(page, 0), pages - 1)
    name = f"<b>{escape(title)}</b>" + (f" ({year})" if year else "")
    n = len(variants)
    lines = [f"{note}{name}", f"{n} version{'s' if n != 1 else ''}", ""]
    shown = list(range(page * PAGE_VARIANTS, min(n, (page + 1) * PAGE_VARIANTS)))
    for i in shown:
        v = variants[i]
        l1, l2 = detail_lines(v.res, v.src, v.codec, v.size, v.langs, v.tags, v.ext, v.grp)
        warn = "CAM · " if v.src in ("CAM", "TS", "PreDVD") else ""
        lines.append(f"<b>{i + 1}</b>  {warn}{escape(l1)}")
        if l2:
            lines.append(f"     {escape(l2)}")
        lines.append("")
    if mode == "remove":
        lines.append("<b>Tap the number of the file to remove.</b>")
    else:
        if any(variants[i].codec == "x265" for i in shown):
            lines.append("<i>x265 files need a modern player (VLC or MX Player) on some devices.</i>")
        lines.append("Tap a number to receive that version.")
    text = "\n".join(lines)
    pick = "rd" if mode == "remove" else "s"
    rows: Rows = []
    nums = [Btn(str(i + 1), f"{pick}:{i}") for i in shown]
    for j in range(0, len(nums), 3):
        rows.append(nums[j:j + 3])
    if pager(page, pages, "v"):
        rows.append(pager(page, pages, "v"))
    nav: list[Btn] = []
    if mode == "remove":
        nav.append(Btn("Cancel", f"v:{page}"))
    else:
        if can_back:
            nav.append(Btn("‹ Back", "b"))
        if is_admin and fid:
            nav += [Btn("Remove a file", "rm"), Btn("Unpick" if picked else "Hand-pick", f"pk:{fid}")]
    if nav:
        rows.append(nav)
    return text, rows


def confirm_remove_screen(rec, label_lines: tuple[str, str], page: int) -> tuple[str, Rows]:
    l1, l2 = label_lines
    text = f"Remove this file from the database?\n\n{escape(l1)}\n{escape(l2)}\n\nThe post in your channel stays untouched."
    return text, [[Btn("Yes, remove", f"rmy:{rec.uid}"), Btn("Cancel", f"v:{page}")]]


def not_found_screen(query: str) -> tuple[str, Rows]:
    return (f"Couldn't find “{escape(query)}”.\n"
            "Check the spelling and try again, or request it and I'll message you when it's added.",
            [[Btn("Request this film", "rq")]])


def join_screen(title: str, link: str) -> tuple[str, Rows]:
    return (f"Join <b>{escape(title)}</b> first, then tap the second button.",
            [[Btn("Join", url=link)], [Btn("I've joined", "fsok")]])


def caption(title: str, year: int | None, rec, minutes: int, footer: str | None = None) -> str:
    l1, l2 = detail_lines(rec.res, rec.src, rec.codec, rec.size, rec.langs, rec.tags, rec.ext, rec.grp)
    cap = f"<b>{escape(title)}</b>" + (f" ({year})" if year else "") + f"\n{escape(l1)}"
    if l2:
        cap += f"\n{escape(l2)}"
    tail = ""
    if minutes:
        tail += f"\n\n<i>Deletes in {minutes} min. Forward it to Saved Messages and download it there.</i>"
    if footer:
        tail += f"\n{footer}"
    if rec.name:
        name = f"\n\n<code>{escape(rec.name[:100])}</code>"
        if len(cap) + len(name) + len(tail) < 1000:
            cap += name
    return cap + tail


def report_rows(uid: str) -> Rows:
    rows: Rows = []
    for i in range(0, len(REASONS), 2):
        rows.append([Btn(label, f"rr:{code}:{uid}") for code, label in REASONS[i:i + 2]])
    rows.append([Btn("Cancel", f"rr:x:{uid}")])
    return rows


def settings_screen(s: dict) -> tuple[str, Rows]:
    ad, fs = s["autodelete_min"], s["force_sub"]
    text = ("<b>Settings</b>\n"
            f"Files delete after: {'never' if not ad else f'{ad} min'}\n"
            f"Search results delete after: {s['result_ttl_min']} min\n"
            f"Protect content: {'on' if s['protect'] else 'off'}  (on = nobody can forward files to Saved Messages)\n"
            f"Answer in groups: {'on' if s['groups'] else 'off'}\n"
            f"Index series files: {'on' if s['index_series'] else 'off'}\n"
            f"Force-join: {escape(fs['title']) if fs else 'off'}  (/forcesub)\n\n"
            "Look and feel: /customize")
    return text, [[Btn("Files delete after", "st:ad"), Btn("Results delete after", "st:rt")],
                  [Btn("Protect content", "st:pc"), Btn("Groups on/off", "st:gr")],
                  [Btn("Index series on/off", "st:se")]]


CUSTOMIZE = (
    "<b>Customize</b>\n\n"
    "<code>/setwelcome</code>  reply to a message to use it as the welcome text. "
    "You can use {name}, {mins} and {films}.\n"
    "<code>/setabout</code>  same, for the About page.\n"
    "<code>/setimage</code>  send a photo with this as its caption, or reply to a photo. "
    "Shown on the welcome screen. <code>/clearimage</code> removes it.\n"
    "<code>/setlinks</code>  reply to a message with one button per line: <code>Label | https://link</code>. "
    "<code>/clearlinks</code> removes them.\n"
    "<code>/setfooter</code>  text added under every file (reply to a message, or <code>/setfooter off</code>).\n"
    "<code>/setpicks Name</code>  rename the hand-picked list.\n"
    "<code>/preview</code>  see the welcome screen as users do.\n\n"
    "Add films to the hand-picked list with the <b>Hand-pick</b> button on any film page.")
