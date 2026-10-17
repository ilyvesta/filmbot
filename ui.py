"""Everything the user sees: plain functions that return (html_text, button_rows).
No Telegram imports here, so it's all unit-testable. Callback data stays well
under Telegram's 64-byte limit (largest is 'rmf:' + 10 hex chars)."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

from parse import label_of

PAGE_FILMS = 6
PAGE_VARIANTS = 8


@dataclass(frozen=True)
class Btn:
    text: str
    data: str | None = None
    url: str | None = None


Rows = list[list[Btn]]

WELCOME = ("<b>Send me a film name.</b>\n"
           "Spelling doesn't have to be perfect, and you can add the year "
           "(<i>heat 1995</i>) if there are remakes.")
WARMING = "Waking up, give me a minute and send that again."
EXPIRED = "That search expired. Send the name again."
NOT_YOURS = "That's someone else's search. Send the name yourself."
EMPTY_QUERY = "Send a film name (letters or numbers, not just symbols)."
BANNED = "You can't use this bot."


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


def _notes(corrected: bool, query: str, year_relaxed: bool, year: int | None) -> str:
    out = ""
    if corrected:
        out += f"Showing results for <b>{escape(query)}</b>.\n"
    if year_relaxed and year:
        out += f"Nothing from {year}, showing other years.\n"
    return out


def results_screen(hits: list[tuple[str, str, int]], page: int, query: str, corrected: bool = False,
                   year_relaxed: bool = False, year: int | None = None, suggest: bool = False) -> tuple[str, Rows]:
    """hits: (film id, label, number of files)."""
    pages = pages_of(len(hits), PAGE_FILMS)
    page = min(max(page, 0), pages - 1)
    head = ("<b>Did you mean one of these?</b>\n" if suggest else
            f"<b>{len(hits)} film{'s' if len(hits) != 1 else ''}</b> for “{escape(query)}”\n")
    text = _notes(corrected and not suggest, query, year_relaxed, year) + head
    rows: Rows = []
    for fid, label, n in hits[page * PAGE_FILMS:(page + 1) * PAGE_FILMS]:
        rows.append([Btn(f"{label}  ·  {n}" if n > 1 else label, f"f:{fid}")])
    if pager(page, pages, "r"):
        rows.append(pager(page, pages, "r"))
    rows.append([Btn("Not here? Request it", "rq")])
    return text, rows


def film_screen(title: str, year: int | None, variants: list, page: int, can_back: bool,
                is_admin: bool = False, fid: str = "", note: str = "") -> tuple[str, Rows]:
    """variants: FileRec-like objects (res, src, size, langs), best first."""
    pages = pages_of(len(variants), PAGE_VARIANTS)
    page = min(max(page, 0), pages - 1)
    name = f"<b>{escape(title)}</b>" + (f" ({year})" if year else "")
    n = len(variants)
    text = f"{note}{name}\n{n} version{'s' if n != 1 else ''}. Tap one to receive it."
    rows: Rows = []
    for i in range(page * PAGE_VARIANTS, min(len(variants), (page + 1) * PAGE_VARIANTS)):
        v = variants[i]
        warn = "CAM · " if v.src in ("CAM", "TS", "PreDVD") else ""
        rows.append([Btn(warn + (label_of(v.res, v.src, v.size, v.langs) or "File"), f"s:{i}")])
    if pager(page, pages, "v"):
        rows.append(pager(page, pages, "v"))
    nav = [Btn("‹ Back", "b")] if can_back else []
    if is_admin and fid:
        nav.append(Btn("Remove film", f"rmf:{fid}"))
    if nav:
        rows.append(nav)
    return text, rows


def not_found_screen(query: str) -> tuple[str, Rows]:
    return (f"Couldn't find “{escape(query)}”.\n"
            "Check the spelling and try again, or request it and I'll message you when it's added.",
            [[Btn("Request this film", "rq")]])


def join_screen(title: str, link: str) -> tuple[str, Rows]:
    return (f"Join <b>{escape(title)}</b> first, then tap the second button.",
            [[Btn("Join", url=link)], [Btn("I've joined", "fsok")]])


def caption(title: str, year: int | None, label: str, minutes: int) -> str:
    cap = f"<b>{escape(title)}</b>" + (f" ({year})" if year else "") + f"\n{escape(label)}"
    if minutes:
        cap += f"\n\n<i>This message deletes itself in {minutes} min. Save it elsewhere first.</i>"
    return cap


def settings_screen(s: dict) -> tuple[str, Rows]:
    ad = s["autodelete_min"]
    fs = s["force_sub"]
    text = ("<b>Settings</b>\n"
            f"Auto-delete files: {'off' if not ad else f'{ad} min'}\n"
            f"Protect content (no forwarding/saving): {'on' if s['protect'] else 'off'}\n"
            f"Answer in groups: {'on' if s['groups'] else 'off'}\n"
            f"Force-join: {escape(fs['title']) if fs else 'off'}  (/forcesub to change)")
    return text, [[Btn("Auto-delete", "st:ad")], [Btn("Protect content", "st:pc")],
                  [Btn("Groups on/off", "st:gr")]]
