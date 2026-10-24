"""Everything the user sees: plain functions that return (html_text, button_rows).
No Telegram imports here, so it's all unit-testable. Callback data stays well
under Telegram's 64-byte limit."""
from __future__ import annotations

from dataclasses import dataclass
import re
from html import escape
from html.parser import HTMLParser

from parse import button_label, detail_lines, human_size
from payload import encode_search

PAGE_FILMS = 6
PAGE_VARIANTS = 8


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
    rows: Rows = [[Btn("Discover", "dc"), Btn("How it works", "nav:help"), Btn("About", "nav:about")],
                  [Btn("My list", "ml")]]
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
        rows.append([Btn(s["picks_name"], "dl:pick"), Btn("Surprise me", "sur")])
    rows += [[Btn("Popular this week", "dl:pop")], [Btn("Recently added", "dl:new")],
             [Btn("‹ Home", "nav:home"), Btn("Close", "dx")]]
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
                   header: str | None = None, back: Btn | None = None, requestable: bool = True,
                   series_group: str | None = None, is_supporter: bool = False, user_name: str = "") -> tuple[str, Rows]:
    """hits: (film id, label, number of files)."""
    pages = pages_of(len(hits), PAGE_FILMS)
    page = min(max(page, 0), pages - 1)
    if header:
        head = f"<b>{escape(header)}</b>\n"
    else:
        head = ("<b>Did you mean one of these?</b>\n" if suggest else
                f"<b>{len(hits)} film{'s' if len(hits) != 1 else ''}</b> for “{escape(query)}”\n")
    badge = f"👤 <b>{escape(user_name)}</b> [⭐ Supporter]\n\n" if (is_supporter and user_name) else ""
    text = badge + _notes(corrected and not suggest and not header, query, year_relaxed, year) + head
    rows: Rows = []
    if series_group:
        rows.append([Btn("Click here for our series group", url=series_group)])
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
                picked: bool = False, watch: bool | None = None, shuffle: str | None = None,
                series_group: str | None = None, is_supporter: bool = False, user_name: str = "") -> tuple[str, Rows]:
    """variants: FileRec-like objects, best first. One full-width button per version.
    mode 'remove' = admin picking a file to delete."""
    pages = pages_of(len(variants), PAGE_VARIANTS)
    page = min(max(page, 0), pages - 1)
    name = f"<b>{escape(title)}</b>" + (f" ({year})" if year else "")
    n = len(variants)
    shown = list(range(page * PAGE_VARIANTS, min(n, (page + 1) * PAGE_VARIANTS)))
    lines = []
    if is_supporter and user_name:
        lines.append(f"👤 <b>{escape(user_name)}</b> [⭐ Supporter]\n")
    lines.extend([f"{note}{name}", f"{n} version{'s' if n != 1 else ''}", ""])
    if mode == "remove":
        lines.append("<b>Tap the file to remove.</b>")
    else:
        if any(variants[i].codec == "x265" for i in shown):
            lines.append("<i>x265 files may need VLC or MX Player on some phones and players.</i>")
        lines.append("Tap a version to receive it.")
    text = "\n".join(lines)
    pick = "rd" if mode == "remove" else "s"
    rows: Rows = []
    if series_group:
        rows.append([Btn("Click here for our series group", url=series_group)])
    rows += [[Btn(button_label(variants[i].res, variants[i].src, variants[i].codec, variants[i].size,
                               variants[i].langs, variants[i].tags, variants[i].ext, variants[i].grp) or "File",
                   f"{pick}:{i}")] for i in shown]
    if pager(page, pages, "v"):
        rows.append(pager(page, pages, "v"))
    nav: list[Btn] = []
    if mode == "remove":
        nav.append(Btn("Cancel", f"v:{page}"))
    else:
        if watch is not None and fid:
            rows.append([Btn("Remove from watchlist" if watch else "Add to watchlist", f"wl:{fid}")])
        if can_back:
            nav.append(Btn("‹ Back", "b"))
        if shuffle:
            nav.append(Btn("Another one", "sur" if shuffle == "picks" else "ml:s"))
        if is_admin and fid:
            nav += [Btn("Remove a file", "rm"), Btn("Unpick" if picked else "Hand-pick", f"pk:{fid}")]
    if nav:
        rows.append(nav)
    return text, rows


def confirm_remove_screen(rec, label_lines: tuple[str, str], page: int) -> tuple[str, Rows]:
    l1, l2 = label_lines
    text = f"Remove this file from the database?\n\n{escape(l1)}\n{escape(l2)}\n\nThe post in your channel stays untouched."
    return text, [[Btn("Yes, remove", f"rmy:{rec.uid}"), Btn("Cancel", f"v:{page}")]]


def not_found_screen(query: str, series_bot: str | None = None) -> tuple[str, Rows]:
    text = (f"Couldn't find “{escape(query)}”.\n"
            "Check the spelling and try again, or request it and I'll message you when it's added.")
    rows: Rows = [[Btn("Request this film", "rq")]]
    if series_bot:
        rows.append([Btn("Open the series bot", url=f"https://t.me/{series_bot}?start={encode_search(query)}")])
    return text, rows


def series_redirect_screen(query: str, series_target: str, is_group: bool = False) -> tuple[str, Rows]:
    if is_group and series_target.startswith("http"):
        return (f"“{escape(query)}” looks like a series. Join our series group below:",
                [[Btn("Click here for our series group", url=series_target)]])
    payload = encode_search(query)
    url = f"https://t.me/{series_target}" + (f"?start={payload}" if payload else "")
    return (f"“{escape(query)}” looks like a series. I only keep films here, but @{escape(series_target)} may have it.",
            [[Btn("Open the series bot", url=url)]])


def join_screen(missing: list[dict], still: bool = False) -> tuple[str, Rows]:
    """missing: the channels this person still has to join (each {title, link}). One button per channel."""
    many = len(missing) != 1
    if still:
        text = "You still need to join " + ("these channels" if many else "this channel") + ", then tap <b>I've joined</b>."
    else:
        text = "To get your film, join " + ("these channels" if many else "this channel") + " first, then tap <b>I've joined</b>."
    rows: Rows = [[Btn(f"Join {c['title']}"[:40], url=c["link"])] for c in missing]
    rows.append([Btn("I've joined", "fsok")])
    return text, rows


def caption(title: str, year: int | None, rec, minutes: int, footer: str | None = None) -> str:
    raw_name = getattr(rec, "name", "") or title
    cap = f"<code>{escape(raw_name)}</code>"
    if minutes:
        cap += f"\n\n<i>Deletes in {minutes} min. Forward it to Saved Messages and download it there.</i>"
    if footer:
        cap += f"\n{footer}"
    return cap


def report_rows(uid: str) -> Rows:
    rows: Rows = []
    for i in range(0, len(REASONS), 2):
        rows.append([Btn(label, f"rr:{code}:{uid}") for code, label in REASONS[i:i + 2]])
    rows.append([Btn("Cancel", f"rr:x:{uid}")])
    return rows


def settings_screen(s: dict) -> tuple[str, Rows]:
    ad, fs = s["autodelete_min"], s.get("force_subs") or []
    stars = s.get("stars_enabled", True)
    survey = s.get("survey_active", True)
    sg = s.get("series_group") or "none"
    sb = s.get("series_bot") or "none"
    text = ("<b>Settings</b>\n"
            f"Files delete after: {'never' if not ad else f'{ad} min'}\n"
            f"Search results delete after: {s['result_ttl_min']} min\n"
            f"Protect content: {'on' if s['protect'] else 'off'}  (no forwarding)\n"
            f"Answer in groups: {'on' if s.get('groups', True) else 'off'}\n"
            f"Telegram Stars: {'on' if stars else 'off'}\n"
            f"Survey active: {'on' if survey else 'off'}\n"
            f"Series group: {sg}\n"
            f"Series bot: @{sb.lstrip('@') if sb != 'none' else 'none'}\n"
            f"Auto-approve join: {'on' if s.get('auto_approve') else 'off'}\n"
            f"Force-join: {f'{len(fs)} channel(s)' if fs else 'off'}  (/forcesub list)\n\n"
            "Group options: /groupsettings | Donor tiers: /donorsettings")
    return text, [
        [Btn(f"Files TTL: {'never' if not ad else f'{ad}m'}", "st:ad"), Btn(f"Results TTL: {s['result_ttl_min']}m", "st:rt")],
        [Btn(f"Protect: {'ON' if s['protect'] else 'OFF'}", "st:pc"), Btn(f"Groups: {'ON' if s.get('groups', True) else 'OFF'}", "st:gr")],
        [Btn(f"Stars: {'ON' if stars else 'OFF'}", "st:stars"), Btn(f"Survey: {'ON' if survey else 'OFF'}", "st:sv")],
        [Btn("👥 Group Settings", "st:to_groups"), Btn(f"Auto-Approve: {'ON' if s.get('auto_approve') else 'OFF'}", "st:ap")]
    ]


def group_settings_screen(s: dict) -> tuple[str, Rows]:
    text = (
        "<b>Group Settings</b>\n\n"
        f"Group Replies: <b>{'ON' if s.get('groups', True) else 'OFF'}</b>\n"
        f"Mention Only: <b>{'ON' if s.get('group_mention_only', False) else 'OFF'}</b>\n"
        f"Silent on No Match: <b>{'ON' if s.get('group_silent_miss', True) else 'OFF'}</b>\n"
        f"Max Query Length: <b>{s.get('group_max_len', 60)} chars</b>\n"
        f"Series Group: <b>{escape(s.get('series_group') or 'Not set')}</b>\n\n"
        "To set series group: <code>/setseriesgroup https://t.me/...</code>"
    )
    rows = [
        [Btn(f"Replies: {'ON' if s.get('groups', True) else 'OFF'}", "gst:grp"),
         Btn(f"Mention-Only: {'ON' if s.get('group_mention_only', False) else 'OFF'}", "gst:men")],
        [Btn(f"Silent Miss: {'ON' if s.get('group_silent_miss', True) else 'OFF'}", "gst:sil"),
         Btn(f"Max Len: {s.get('group_max_len', 60)}", "gst:len")],
        [Btn("‹ Settings", "st:refresh"), Btn("Close", "dx")]
    ]
    return text, rows


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


SKIP_KINDS = {"u": ("unreadable", "Unreadable titles", "Files whose name gave no readable title."),
              "s": ("series", "Series", "Files that look like TV episodes (S01E02, Season 2). Skipped because this is a movie bot."),
              "l": ("lookalike", "Look-alikes", "Same film and exactly the same size as a copy already saved. The first copy is kept.")}
SKIP_PAGE = 8


def post_link(ch: int, mid: int) -> str | None:
    s = str(ch)
    return f"https://t.me/c/{s[4:]}/{mid}" if s.startswith("-100") and mid else None


def skipped_menu(counts: dict[str, int], cap: int) -> tuple[str, Rows]:
    text = "<b>Skipped files</b>\nFiles the bot saw but did not add. Tap a list to check them."
    if any(v >= cap for v in counts.values()):
        text += f"\n<i>Each list keeps the latest {cap:,}.</i>"
    rows = [[Btn(f"{label} ({counts[kind]:,})", f"sk:{code}:0")] for code, (kind, label, _) in SKIP_KINDS.items()]
    return text, rows


def skip_list_screen(code: str, entries: list[dict], page: int, total: int) -> tuple[str, Rows]:
    """entries: {name, size, ch, mid, kept_name, kept_ch, kept_mid, kept_gone} for this page."""
    kind, label, blurb = SKIP_KINDS[code]
    pages = pages_of(total, SKIP_PAGE)
    page = min(max(page, 0), pages - 1)
    head = f"<b>{label}</b>  ({total:,})\n{blurb}\n"
    body: list[str] = []
    for i, e in enumerate(entries, start=page * SKIP_PAGE + 1):
        link = post_link(e["ch"], e["mid"])
        line = f"\n{i}. <code>{escape((e['name'] or '(no name)')[:55])}</code>"
        if e.get("size"):
            line += f" · {human_size(e['size'])}"
        if link:
            line += f'\n    <a href="{link}">open post</a>'
        else:
            line += f"\n    chat {e['ch']}, message {e['mid']}"
        if kind == "lookalike":
            if e.get("kept_gone"):
                line += "\n    kept copy has since been removed"
            else:
                klink = post_link(e.get("kept_ch", 0), e.get("kept_mid", 0))
                line += f"\n    kept: <code>{escape((e.get('kept_name') or '?')[:45])}</code>"
                if klink:
                    line += f' <a href="{klink}">open</a>'
        body.append(line)
    text = head
    for b in body:
        if len(text) + len(b) > 3800:
            text += "\n…"
            break
        text += b
    if not body:
        text += "\nNothing here."
    rows: Rows = []
    if pager(page, pages, f"sk:{code}"):
        rows.append(pager(page, pages, f"sk:{code}"))
    rows.append([Btn("Send full list (.txt)", f"skt:{code}")])
    rows.append([Btn("‹ Back", "sk:m")])
    return text, rows


MYLIST_PAGE = 6


def mylist_screen(films: list[tuple[str, str]], page: int, cap: int, requests: list[str], searches: int,
                  badge: str = "") -> tuple[str, Rows]:
    """films: (film id, label) of the watchlist; requests: the person's open request texts."""
    pages = pages_of(len(films), MYLIST_PAGE)
    page = min(max(page, 0), pages - 1)
    lines = [f"<b>My list</b>{badge}", "", f"<b>Watchlist</b>  ({len(films)} of {cap})"]
    lines.append("Tap a film to open it." if films else "Nothing saved yet. Open any film and tap Add to watchlist.")
    if requests:
        lines += ["", "<b>Your requests</b>"] + [f"• {escape(t)}" for t in requests]
        lines.append("I'll message you here when they're added.")
    lines += ["", f"You've searched {searches:,} time{'s' if searches != 1 else ''}."]
    rows: Rows = []
    for fid, label in films[page * MYLIST_PAGE:(page + 1) * MYLIST_PAGE]:
        rows.append([Btn(label[:40], f"f:{fid}"), Btn("Remove", f"ml:r:{fid}")])
    if pager(page, pages, "ml:p"):
        rows.append(pager(page, pages, "ml:p"))
    if films:
        rows.append([Btn("Shuffle", "ml:s")])
    for i, t in enumerate(requests):
        rows.append([Btn(f"Cancel request: {t[:24]}", f"ml:c:{i}")])
    rows.append([Btn("‹ Home", "nav:home")])
    return "\n".join(lines), rows


def daily_summary(day: str, d: dict, open_requests: list[dict], extra: list[str] | None = None) -> str:
    lines = [f"<b>Yesterday ({day})</b>",
             f"Active users: {d.get('u', 0):,}  (new: {d.get('n', 0):,})",
             f"Searches: {d.get('s', 0):,}  (no result: {d.get('m', 0):,})",
             f"Files sent: {d.get('f', 0):,}",
             f"New requests: {d.get('r', 0):,}",
             f"Problem reports: {d.get('p', 0):,}"]
    lines += extra or []
    if open_requests:
        lines += ["", "<b>Open requests</b>"] + [f"• {escape(r['text'])}  ({len(r['users'])})" for r in open_requests[:5]]
    return "\n".join(lines)


_OK_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote"}


class _Clean(HTMLParser):
    """Keeps only the tags Telegram understands, closes anything left open, drops everything else."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in _OK_TAGS:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if not re.match(r"(?i)^(https?://|tg://|mailto:)", href):
                return
            self.out.append(f'<a href="{escape(href, quote=True)}">')
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        self.out.append(escape(data, quote=False))

    def close(self):
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")


def safe_html(text: str) -> str:
    """Admin-written text (welcome, about, footer): links, bold, italic and the like survive; anything
    Telegram can't show is removed and unclosed tags are closed, so a page can never break."""
    p = _Clean()
    p.feed(text or "")
    p.close()
    return "".join(p.out)


# ---------------------------------------------------------------- donate & perks
def donate_screen(s: dict, methods: list[dict], wall: list[str]) -> tuple[str, Rows]:
    text = (
        "<b>Thank you for keeping Éric Rohmer going.</b>\n\n"
        "Éric Rohmer is free to use, but hosting the bot, maintaining its database and expanding the film library all cost money. "
        "Contributions help keep the service running and make room for a larger, better-maintained collection.\n\n"
        "Depending on the amount and type of support, available benefits may include priority film requests, a larger watchlist, "
        "a supporter badge, extended file availability, file messages without the standard footer, and—at the qualifying level—"
        "bypassing force-join requirements. Not every contribution includes every benefit.\n\n"
        "Thank you for considering a contribution, whatever amount is comfortable for you."
    )
    rows: Rows = []
    if s.get("stars_enabled", True):
        rows.append([Btn("⭐ Support with Telegram Stars", "dn:stars")])
    for i, m in enumerate(methods):
        title = m.get("title") or f"Method {i+1}"
        m_type = m.get("type", "")
        icon = "🏦 " if m_type == "bank" else ("🪙 " if m_type == "crypto" else "💳 ")
        rows.append([Btn(f"{icon}{title}", f"dn:m:{i}")])
    if wall:
        rows.append([Btn(f"🌟 Supporters Wall ({len(wall)})", "dn:wall")])
    rows.append([Btn("‹ Home", "nav:home")])
    return text, rows


def _copyable_details(details: str) -> str:
    """Render each payment detail's value in Telegram's tap-to-copy monospace style."""
    lines = []
    for raw in (details or "").splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if ":" in line:
            label, value = line.split(":", 1)
            value = value.strip()
            lines.append(f"<b>{escape(label.strip())}:</b> <code>{escape(value)}</code>" if value else f"<b>{escape(label.strip())}:</b>")
        else:
            lines.append(f"<code>{escape(line)}</code>")
    return "\n".join(lines)


def donate_method_screen(m: dict, method_index: int | None = None, uid: int | None = None) -> tuple[str, Rows]:
    title = m.get("title", "Donation Method")
    m_type = m.get("type", "")
    details = m.get("details", "")
    warn = ""
    if m_type == "crypto" and "BEP20" in details.upper():
        warn = "\n\n⚠️ <b>IMPORTANT:</b> Send via Binance Smart Chain (BEP20) only! Sending via any other network may result in permanent loss."
    user_id = f"\n\n<b>Your Telegram ID (tap to copy):</b> <code>{uid}</code>" if uid is not None else ""
    text = (
        f"<b>{escape(title)}</b>\n\n"
        f"{_copyable_details(details)}{warn}{user_id}\n\n"
        "After paying, tap the button below and send your receipt, transaction screenshot or reference. "
        "Please include your Telegram ID in the message or receipt caption so the payment can be matched to your account. "
        "You can also write a note; if anything needs clarifying, you and the admin can continue the conversation here."
    )
    claim_action = f"dn:claimidx:{method_index}" if method_index is not None else f"dn:claim:{m.get('title', 'donation')[:20]}"
    rows: Rows = [
        [Btn("✅ I've Donated (Submit Receipt)", claim_action)],
        [Btn("‹ All Methods", "dn:start")]
    ]
    return text, rows


def claim_admin_rows(cid: str) -> Rows:
    return [
        [Btn("Basic (30d)", f"clm:30:{cid}"), Btn("Full (60d)", f"clm:60:{cid}"), Btn("Full (90d)", f"clm:90:{cid}")],
        [Btn("✍️ Approve · custom days", f"clm:custom:{cid}")],
        [Btn("⭐ Approve + join bypass", f"clm:bypass:{cid}"), Btn("↩ Reply to donor", f"clm:reply:{cid}")],
        [Btn("❌ Decline", f"clm:dec:{cid}")]
    ]


def stars_screen(uid: int | None = None) -> tuple[str, Rows]:
    user_id = f"\n\n<b>Your Telegram ID (tap to copy):</b> <code>{uid}</code>" if uid is not None else ""
    text = (
        "⭐ <b>Support Éric Rohmer with Telegram Stars</b>\n\n"
        "Contributions help pay for hosting, database capacity and adding more films. Thank you for helping keep the library available.\n\n"
        "Choose one of the suggested amounts or enter your own amount. Your Telegram ID is shown below in case you need it when contacting the admin."
        f"{user_id}"
    )
    rows: Rows = [
        [Btn("⭐ 50 Stars", "dn:st:50"), Btn("⭐ 100 Stars", "dn:st:100")],
        [Btn("⭐ 250 Stars", "dn:st:250"), Btn("⭐ 500 Stars", "dn:st:500")],
        [Btn("✍️ Custom amount", "dn:stcustom")],
        [Btn("‹ Back", "dn:start")]
    ]
    return text, rows

def wall_screen(wall: list[str]) -> tuple[str, Rows]:
    text = "🌟 <b>Supporters Wall</b>\n\nA massive thank you to our incredible supporters:\n\n"
    if not wall:
        text += "<i>No supporters listed yet. Be the first!</i>"
    else:
        text += "\n".join(f"• {escape(name)}" for name in wall[:50])
    rows: Rows = [[Btn("‹ Back to Donate", "dn:start")]]
    return text, rows


# ---------------------------------------------------------------- demographics survey
SURVEY_COUNTRIES = [
    "India", "Nigeria", "Pakistan", "South Africa", "Indonesia", "Bangladesh", "Ghana"
]

ALL_COUNTRIES = [
    "Algeria", "Argentina", "Australia", "Bangladesh", "Brazil", "Canada", "Chile", "China", "Colombia",
    "Egypt", "Ethiopia", "France", "Germany", "Ghana", "India", "Indonesia", "Italy", "Jamaica", "Japan",
    "Kenya", "Malaysia", "Mexico", "Morocco", "Netherlands", "New Zealand", "Nigeria", "Pakistan",
    "Philippines", "Poland", "Russia", "Saudi Arabia", "Singapore", "South Africa", "South Korea", "Spain",
    "Tanzania", "Turkey", "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom", "United States",
    "Vietnam", "Zimbabwe"
]


def survey_screen(step: int, extra: str = "") -> tuple[str, Rows]:
    if step == 0:
        text = (
            "📊 <b>Community Survey (1/4) — Gender</b>\n\n"
            "Help us understand our community to serve you better! Your answers are completely anonymous."
        )
        rows: Rows = [
            [Btn("Male", "sv:0:Male"), Btn("Female", "sv:0:Female")],
            [Btn("Prefer not to say", "sv:0:Prefer not to say")],
            [Btn("‹ Cancel", "nav:home")]
        ]
        return text, rows
    elif step == 1:
        text = "📊 <b>Community Survey (2/4) — Age Group</b>\n\nSelect your age range:"
        rows = [
            [Btn("Under 18", "sv:1:Under 18"), Btn("18–24", "sv:1:18-24")],
            [Btn("25–34", "sv:1:25-34"), Btn("35+", "sv:1:35+")],
            [Btn("Prefer not to say", "sv:1:Prefer not to say")]
        ]
        return text, rows
    elif step == 2:
        if extra.startswith("az:"):
            letter = extra.split("az:")[1].upper()
            cands = [c for c in ALL_COUNTRIES if c.upper().startswith(letter)]
            text = f"📊 <b>Community Survey (3/4) — Country ({letter})</b>\n\nSelect your country:"
            rows = []
            for i in range(0, len(cands), 2):
                pair = [Btn(c, f"sv:2:{c}") for c in cands[i:i + 2]]
                rows.append(pair)
            rows.append([Btn("‹ Common Countries", "sv:2:main")])
            return text, rows
        text = "📊 <b>Community Survey (3/4) — Country</b>\n\nWhere are you tuning in from?"
        rows = []
        for i in range(0, len(SURVEY_COUNTRIES), 2):
            rows.append([Btn(c, f"sv:2:{c}") for c in SURVEY_COUNTRIES[i:i + 2]])
        rows.append([Btn("🌍 Browse All Countries (A–Z)", "sv:2:az")])
        rows.append([Btn("Other / Prefer not to say", "sv:2:Other")])
        return text, rows
    elif step == 3:
        text = "📊 <b>Community Survey (4/4) — Discovery</b>\n\nHow did you discover Éric Rohmer?"
        rows = [
            [Btn("Telegram Search", "sv:3:Telegram Search")],
            [Btn("Friend / Word of Mouth", "sv:3:Friend")],
            [Btn("Telegram Channel / Group", "sv:3:Channel or Group")],
            [Btn("TikTok / Instagram / X", "sv:3:Social Media")],
            [Btn("Other", "sv:3:Other")]
        ]
        return text, rows
    else:
        text = (
            "✅ <b>Thank you for participating!</b>\n\n"
            "Your anonymous answers help us understand where to optimize server routes, what content to curate, "
            "and how to grow our movie collection for you."
        )
        rows = [[Btn("‹ Home", "nav:home")]]
        return text, rows


def survey_az_buckets_screen() -> tuple[str, Rows]:
    text = "🌍 <b>Select Country First Letter:</b>"
    buckets = [
        ["A", "B", "C", "D"],
        ["E", "F", "G", "I"],
        ["J", "K", "M", "N"],
        ["P", "R", "S", "T"],
        ["U", "V", "Z"]
    ]
    rows: Rows = []
    for b_row in buckets:
        rows.append([Btn(letter, f"sv:2:az:{letter.lower()}") for letter in b_row])
    rows.append([Btn("‹ Back to Common Countries", "sv:2:main")])
    return text, rows


def survey_completed_screen() -> tuple[str, Rows]:
    text = (
        "✅ <b>Survey Already Completed</b>\n\n"
        "You have already shared your feedback! We record one submission per user to keep community statistics accurate.\n\n"
        "Thank you for helping us improve Éric Rohmer."
    )
    rows: Rows = [[Btn("‹ Home", "nav:home")]]
    return text, rows


def survey_closed_screen() -> tuple[str, Rows]:
    text = (
        "📊 <b>Community Survey Closed</b>\n\n"
        "The community survey is currently closed. Thank you for your interest!"
    )
    rows: Rows = [[Btn("‹ Home", "nav:home")]]
    return text, rows

