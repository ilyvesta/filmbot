"""Telegram wiring (Kurigram / Pyrogram API). Logic that doesn't need Telegram
lives in index.py, ui.py and store.py."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from html import escape

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import ChannelInvalid, ChannelPrivate, FloodWait, RPCError, UserNotParticipant
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import ui
from ctx import Ctx, Session
from index import film_id
from normalize import core_key, normalize
from parse import is_junk, label_of, parse_best, quality_key_of
from store import FileRec
from ui import Btn

log = logging.getLogger("moviebot")
HTML = ParseMode.HTML
GROUPS = (ChatType.GROUP, ChatType.SUPERGROUP)
ADMIN_CMDS = ["stats", "forcesub", "settings", "index", "indexstop", "requests",
              "misses", "alias", "ban", "unban"]
AD_CHOICES = [0, 5, 15, 30, 60]
REQUEST_PINGS = {5, 10, 25, 50, 100, 250}


# ---------------------------------------------------------------- helpers
def kb(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(b.text, callback_data=b.data, url=b.url)
                                  for b in row] for row in rows])


def reply_to(m):
    async def show(text, rows=None):
        return await m.reply_text(text, parse_mode=HTML, reply_markup=kb(rows))
    return show


def edit_of(msg):
    async def show(text, rows=None):
        try:
            return await msg.edit_text(text, parse_mode=HTML, reply_markup=kb(rows))
        except RPCError as e:                      # "message not modified" etc.
            log.debug("edit skipped: %s", e)
            return msg
    return show


def rss_mb() -> int | None:
    try:
        with open("/proc/self/status") as f:
            return round(int(re.search(r"VmRSS:\s+(\d+)", f.read()).group(1)) / 1024)
    except Exception:
        return None


async def notify_admins(ctx: Ctx, client, text: str, rows=None) -> None:
    targets = [ctx.cfg.log_channel] if ctx.cfg.log_channel else list(ctx.cfg.admins)
    for t in targets:
        try:
            await client.send_message(t, text, parse_mode=HTML, reply_markup=kb(rows))
        except Exception as e:
            log.warning("couldn't notify %s: %s", t, e)


async def load_variants(ctx: Ctx, fid: str) -> list[FileRec]:
    files = await ctx.store.film_files(fid)
    files.sort(key=lambda r: (quality_key_of(r.res, r.src), r.size), reverse=True)
    return files


# ---------------------------------------------------------------- force-join
async def gate(ctx: Ctx, client, uid: int, show) -> bool:
    """True = user is blocked (join prompt already shown)."""
    fs = (await ctx.settings())["force_sub"]
    if not fs or ctx.is_admin(uid):
        return False
    try:
        mem = await client.get_chat_member(fs["chat"], uid)
        joined = mem.status != ChatMemberStatus.LEFT      # banned there? don't lock them out here
    except UserNotParticipant:
        joined = False
    except Exception as e:                                # never lock everyone out because of a glitch
        log.warning("force-join check failed: %s", e)
        if ctx.first_time("fs-fail"):
            await notify_admins(ctx, client, "Force-join check is failing, so I'm letting everyone in. "
                                             "Is the bot still an admin in that channel? (/forcesub)")
        return False
    if joined:
        return False
    text, rows = ui.join_screen(fs["title"], fs["link"])
    await show(text, rows)
    return True


# ---------------------------------------------------------------- delivering files
async def _delete_later(client, chat_id: int, msg_id: int, seconds: int) -> None:
    await asyncio.sleep(seconds)
    try:
        await client.delete_messages(chat_id, msg_id)
    except Exception:
        pass


async def deliver(ctx: Ctx, client, chat_id: int, rec: FileRec) -> bool:
    film = ctx.index.get(rec.film)
    s = await ctx.settings()
    minutes = s["autodelete_min"]
    cap = ui.caption(film.title if film else "File", film.year if film else None,
                     label_of(rec.res, rec.src, rec.size, rec.langs), minutes)
    for attempt in (1, 2):
        try:
            src = await client.get_messages(rec.ch, rec.mid)
            if not src or src.empty or not (src.document or src.video or src.audio):
                await _drop(ctx, rec)
                await client.send_message(chat_id, "That file is gone. Try another version.")
                return False
            sent = await src.copy(chat_id, caption=cap, parse_mode=HTML, protect_content=bool(s["protect"]),
                                  reply_markup=kb([[Btn("Report a problem", f"rep:{rec.uid}")]]))
            break
        except FloodWait as e:
            if attempt == 2:
                await client.send_message(chat_id, "Busy right now, try again in a moment.")
                return False
            await asyncio.sleep(e.value + 1)
        except (ChannelInvalid, ChannelPrivate):
            if ctx.first_time(f"ch-{rec.ch}"):
                await notify_admins(ctx, client, f"I can't read channel <code>{rec.ch}</code>. "
                                                 "Was I removed from it?")
            await client.send_message(chat_id, "I can't get that file right now. Try again later.")
            return False
        except RPCError as e:
            log.warning("copy failed: %s", e)
            await client.send_message(chat_id, "Couldn't send that one. Try another version.")
            return False
    await ctx.store.bump_hit(rec.film)
    ctx.index.record_open(rec.film)
    if minutes:
        asyncio.create_task(_delete_later(client, chat_id, sent.id, minutes * 60))
    return True


async def _drop(ctx: Ctx, rec: FileRec) -> None:
    if await ctx.store.remove_file(rec.uid):
        ctx.index.adjust_files(rec.film, -1)


# ---------------------------------------------------------------- search
async def run_search(ctx: Ctx, show, owner: int, text: str) -> None:
    res = await asyncio.to_thread(ctx.index.search, text, 30)     # up to 5 pages of films
    if res.kind == "empty":
        await show(ui.EMPTY_QUERY)
        return
    if res.kind == "none":
        await ctx.store.add_miss(res.query)
        t, rows = ui.not_found_screen(text)
        sent = await show(t, rows)
        ctx.sessions.put(sent.chat.id, sent.id, Session(owner, text, "none", [], qkey=res.query))
        return
    hits = [(h.film.id, h.film.label, h.film.file_count) for h in res.hits]
    sess = Session(owner, text, res.kind, hits, res.corrected, res.year, res.year_relaxed, qkey=res.query)
    if res.kind == "match":
        f = res.hits[0].film
        sess.film, sess.variants = (f.id, f.title, f.year), await load_variants(ctx, f.id)
        t, rows = _film_view(ctx, sess)
    elif res.kind == "suggest":
        t, rows = ui.results_screen(hits, 0, res.query, suggest=True)
    else:
        t, rows = ui.results_screen(hits, 0, res.query, res.corrected, res.year_relaxed, res.year)
    sent = await show(t, rows)
    ctx.sessions.put(sent.chat.id, sent.id, sess)


def _film_view(ctx: Ctx, s: Session):
    fid, title, year = s.film
    note = ""
    if s.corrected:
        note += f"Showing results for <b>{escape(s.qkey)}</b>.\n"
    if s.year_relaxed and s.year:
        note += f"No {s.year} version here.\n"
    return ui.film_screen(title, year, s.variants, s.vpage, can_back=len(s.hits) > 1,
                          is_admin=ctx.is_admin(s.owner), fid=fid, note=note)


async def on_text(ctx: Ctx, client, m) -> None:
    if not m.from_user or m.from_user.id in ctx.banned:
        return
    uid, text = m.from_user.id, (m.text or "").strip()
    group = m.chat.type in GROUPS
    if group:
        s = await ctx.settings()
        if not s["groups"] or not 3 <= len(text) <= 60 or len(text.split()) > 8:
            return
    else:
        await ctx.store.touch_user(uid)
    if ctx.throttled(uid):
        return
    show = reply_to(m)
    if not ctx.ready:
        await show(ui.WARMING)
        return
    if await gate(ctx, client, uid, show):
        return
    await run_search(ctx, show, uid, text)


async def cmd_start(ctx: Ctx, client, m) -> None:
    uid = m.from_user.id
    if uid in ctx.banned:
        await m.reply_text(ui.BANNED)
        return
    await ctx.store.touch_user(uid)
    arg = m.command[1] if len(m.command) > 1 else ""
    if arg.startswith("f_"):
        if await gate(ctx, client, uid, reply_to(m)):
            return
        rec = await ctx.store.get_file(arg[2:])
        if rec:
            await deliver(ctx, client, m.chat.id, rec)
        else:
            await m.reply_text("That file isn't available any more.")
        return
    await m.reply_text(ui.WELCOME, parse_mode=HTML)


# ---------------------------------------------------------------- callbacks
async def on_cb(ctx: Ctx, client, cb) -> None:
    uid, data = cb.from_user.id, cb.data or ""
    if uid in ctx.banned:
        await cb.answer(ui.BANNED, show_alert=True)
        return
    head, _, arg = data.partition(":")
    if head == "noop":
        await cb.answer()
        return
    if head == "fsok":
        if await gate(ctx, client, uid, edit_of(cb.message)):
            await cb.answer("Not joined yet.", show_alert=True)
        else:
            await edit_of(cb.message)("You're in. Send the film name.")
            await cb.answer()
        return
    if head == "rep":
        await _report(ctx, client, cb, arg)
        return
    if head in ("st", "rmf", "rmy", "rmfile"):
        if not ctx.is_admin(uid):
            await cb.answer("Admins only.", show_alert=True)
            return
        await _admin_cb(ctx, client, cb, head, arg)
        return

    msg = cb.message
    sess = ctx.sessions.get(msg.chat.id, msg.id)
    if not sess:
        await cb.answer(ui.EXPIRED, show_alert=True)
        return
    if sess.owner != uid:
        await cb.answer(ui.NOT_YOURS, show_alert=True)
        return
    show = edit_of(msg)

    if head == "r" and arg.isdigit():
        sess.page = int(arg)
        t, rows = ui.results_screen(sess.hits, sess.page, sess.qkey, sess.corrected, sess.year_relaxed,
                                    sess.year, suggest=sess.kind == "suggest")
        await show(t, rows)
    elif head == "b":
        t, rows = ui.results_screen(sess.hits, sess.page, sess.qkey, sess.corrected, sess.year_relaxed,
                                    sess.year, suggest=sess.kind == "suggest")
        await show(t, rows)
    elif head == "f":
        film = ctx.index.get(arg)
        if not film:
            await cb.answer("That one's gone.", show_alert=True)
            return
        sess.film, sess.vpage = (film.id, film.title, film.year), 0
        sess.variants = await load_variants(ctx, film.id)
        t, rows = _film_view(ctx, sess)
        await show(t, rows)
    elif head == "v" and arg.isdigit() and sess.film:
        sess.vpage = int(arg)
        t, rows = _film_view(ctx, sess)
        await show(t, rows)
    elif head == "s" and arg.isdigit() and sess.film and int(arg) < len(sess.variants):
        rec = sess.variants[int(arg)]
        if msg.chat.type in GROUPS:
            await cb.answer(url=ctx.deep_link(rec.uid))      # opens the bot in private, then delivers
            return
        await cb.answer("Sending…")
        await deliver(ctx, client, msg.chat.id, rec)
        return
    elif head == "rq":
        await _request(ctx, client, cb, sess, msg.chat.type in GROUPS)
        return
    await cb.answer()


async def _request(ctx: Ctx, client, cb, sess: Session, group: bool) -> None:
    qkey = core_key(normalize(sess.query)) or sess.qkey
    count, new = await ctx.store.add_request(qkey, sess.query[:80], cb.from_user.id)
    extra = " Open me in private and press Start, or I can't message you." if group else ""
    await cb.answer("Requested. I'll message you when it's added." + extra, show_alert=True)
    if new or count in REQUEST_PINGS:
        await notify_admins(ctx, client, f"Request: <b>{escape(sess.query)}</b>  ({count} "
                                         f"{'person' if count == 1 else 'people'})")


async def _report(ctx: Ctx, client, cb, uid_arg: str) -> None:
    rec = await ctx.store.get_file(uid_arg)
    if not rec:
        await cb.answer("Thanks, already handled.")
        return
    film = ctx.index.get(rec.film)
    await cb.answer("Thanks, reported.", show_alert=True)
    await notify_admins(ctx, client, f"Reported: <b>{escape(film.label if film else rec.film)}</b>\n"
                                     f"{escape(label_of(rec.res, rec.src, rec.size, rec.langs))}",
                        [[Btn("Remove this file", f"rmfile:{rec.uid}")]])


async def _admin_cb(ctx: Ctx, client, cb, head: str, arg: str) -> None:
    show = edit_of(cb.message)
    if head == "st":
        s = await ctx.settings()
        if arg == "ad":
            nxt = AD_CHOICES[(AD_CHOICES.index(s["autodelete_min"]) + 1) % len(AD_CHOICES)] \
                if s["autodelete_min"] in AD_CHOICES else 0
            await ctx.set_setting("autodelete_min", nxt)
        elif arg == "pc":
            await ctx.set_setting("protect", not s["protect"])
        elif arg == "gr":
            await ctx.set_setting("groups", not s["groups"])
        t, rows = ui.settings_screen(await ctx.settings())
        await show(t, rows)
    elif head == "rmf":
        film = ctx.index.get(arg)
        if not film:
            await cb.answer("Already gone.", show_alert=True)
            return
        await show(f"Remove <b>{escape(film.label)}</b> and its {film.file_count} file(s) from the database?\n"
                   "The posts in your channel stay untouched.",
                   [[Btn("Yes, remove", f"rmy:{arg}"), Btn("Cancel", "noop")]])
    elif head == "rmy":
        n = await ctx.store.remove_film(arg)
        ctx.index.remove_film(arg)
        await show(f"Removed {n} file(s).")
    elif head == "rmfile":
        rec = await ctx.store.remove_file(arg)
        if rec:
            ctx.index.adjust_files(rec.film, -1)
        await show("File removed.")
    await cb.answer()


# ---------------------------------------------------------------- indexing
@dataclass
class IndexSummary:
    seen: int = 0
    added: int = 0
    dupes: int = 0
    series: int = 0
    unreadable: int = 0

    def add(self, o: "IndexSummary") -> None:
        for k in ("seen", "added", "dupes", "series", "unreadable"):
            setattr(self, k, getattr(self, k) + getattr(o, k))


async def index_messages(ctx: Ctx, msgs) -> IndexSummary:
    s = IndexSummary()
    recs: list[FileRec] = []
    films: dict[str, tuple] = {}
    for m in msgs:
        media = m.document or m.video or m.audio
        if not media:
            continue
        s.seen += 1
        p = parse_best(getattr(media, "file_name", None), m.caption)
        if is_junk(p.title) or not normalize(p.title):
            s.unreadable += 1
            continue
        if p.kind == "series":
            s.series += 1
            continue
        fid = film_id(normalize(p.title), p.year)
        films[fid] = (p.title, p.year)
        recs.append(FileRec(media.file_unique_id, fid, m.chat.id, m.id, media.file_size or 0,
                            p.resolution, p.source, p.languages, (getattr(media, "file_name", "") or "")[:80]))
    new = await ctx.store.add_files(recs, films)
    s.added, s.dupes = len(new), len(recs) - len(new)
    per: dict[str, int] = {}
    for r in new:
        per[r.film] = per.get(r.film, 0) + 1
    if per:
        await asyncio.to_thread(ctx.index.add_films,
                                [(films[f][0], films[f][1], n, 0, ()) for f, n in per.items()])
    return s


async def on_channel_file(ctx: Ctx, client, m) -> None:
    if m.chat.id in ctx.cfg.file_channels:
        await index_messages(ctx, [m])


async def backfill(ctx: Ctx, client, status, ch: int, first: int, last: int) -> None:
    job = ctx.index_job = {"stop": False}
    total, batches = IndexSummary(), 0
    start = first
    try:
        while start <= last and not job["stop"]:
            ids = list(range(start, min(start + 200, last + 1)))
            try:
                msgs = await client.get_messages(ch, ids)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                continue
            total.add(await index_messages(ctx, [x for x in msgs if x and not x.empty]))
            start, batches = ids[-1] + 1, batches + 1
            if batches % 15 == 0:
                try:
                    await status.edit_text(f"Indexing… at message {start} of {last}\n"
                                           f"{total.added:,} files added so far")
                except RPCError:
                    pass
        await ctx.store.set_setting("index_progress", {"ch": ch, "next": start})
        verb = "Stopped" if job["stop"] else "Done"
        await status.reply_text(
            f"{verb}. Looked at {total.seen:,} files.\nAdded {total.added:,}, already had {total.dupes:,}.\n"
            f"Skipped {total.series:,} that look like series and {total.unreadable:,} I couldn't read a title from.\n"
            f"Next message to continue from: {start}")
    except Exception as e:
        log.exception("backfill failed")
        await status.reply_text(f"Indexing stopped with an error: {e}\nResume from message {start}.")
    finally:
        ctx.index_job = None


# ---------------------------------------------------------------- admin commands
async def on_admin_cmd(ctx: Ctx, client, m) -> None:
    cmd, args = m.command[0].lower(), m.command[1:]
    say = lambda t, rows=None: m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))   # noqa: E731

    if cmd == "stats":
        c = await ctx.store.counts()
        mem = rss_mb()
        fs = (await ctx.settings())["force_sub"]
        lines = [f"Files: {c['files']:,}", f"Films: {c['films']:,}  (search index: {len(ctx.index):,})",
                 f"Users: {c['users']:,}", f"Open requests: {c['requests']}"]
        if c["db_mb"] is not None:
            lines.append(f"Database: {c['db_mb']} MB of 512")
        if mem:
            lines.append(f"Bot memory: {mem} MB of 512" + ("  (getting tight)" if mem > 420 else ""))
        lines.append(f"Force-join: {escape(fs['title']) if fs else 'off'}")
        await say("\n".join(lines))
    elif cmd == "settings":
        t, rows = ui.settings_screen(await ctx.settings())
        await say(t, rows)
    elif cmd == "forcesub":
        await _forcesub(ctx, client, m, args, say)
    elif cmd == "index":
        if len(args) < 2 or not all(re.fullmatch(r"-?\d+", a) for a in args):
            await say("Usage: <code>/index CHANNEL_ID LAST_MESSAGE_ID [FIRST_MESSAGE_ID]</code>\n"
                      "Get the last id from the newest post's link (t.me/c/…/<b>12345</b>).")
            return
        ch, last = int(args[0]), int(args[1])
        first = int(args[2]) if len(args) > 2 else 1
        if ch not in ctx.cfg.file_channels:
            await say("That channel isn't in FILE_CHANNELS.")
        elif ctx.index_job:
            await say("An indexing run is already going. /indexstop first.")
        else:
            status = await say(f"Indexing {ch} from message {first} to {last}…")
            asyncio.create_task(backfill(ctx, client, status, ch, first, last))
    elif cmd == "indexstop":
        if ctx.index_job:
            ctx.index_job["stop"] = True
            await say("Stopping after the current batch.")
        else:
            await say("Nothing is running.")
    elif cmd == "requests":
        rows = await ctx.store.open_requests(15)
        await say("\n".join(f"• {escape(r['text'])}  ({len(r['users'])})" for r in rows) or "No open requests.")
    elif cmd == "misses":
        rows = await ctx.store.top_misses(15)
        await say("Searches that found nothing:\n" + "\n".join(f"• {escape(k)}  ({n})" for k, n in rows)
                  if rows else "Nothing missed yet.")
    elif cmd == "alias":
        await _alias(ctx, m.text, say)
    elif cmd in ("ban", "unban") and args and re.fullmatch(r"\d+", args[0]):
        flag = cmd == "ban"
        await ctx.store.set_banned(int(args[0]), flag)
        (ctx.banned.add if flag else ctx.banned.discard)(int(args[0]))
        await say(f"{'Banned' if flag else 'Unbanned'} {args[0]}.")
    else:
        await say("Check the command and try again.")


async def _forcesub(ctx: Ctx, client, m, args, say) -> None:
    s = await ctx.settings()
    if not args:
        fs = s["force_sub"]
        await say(f"Force-join is {'on: ' + escape(fs['title']) if fs else 'off'}.\n"
                  "<code>/forcesub CHANNEL_ID_or_@username [invite link]</code> to set, "
                  "<code>/forcesub off</code> to remove.")
        return
    if args[0].lower() == "off":
        await ctx.set_setting("force_sub", None)
        await say("Force-join is off.")
        return
    ref = int(args[0]) if re.fullmatch(r"-?\d+", args[0]) else args[0]
    try:
        chat = await client.get_chat(ref)
        me = await client.get_chat_member(chat.id, "me")
    except RPCError:
        await say("I can't see that channel. Add me as an admin there first.")
        return
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await say("Make me an admin in that channel first. I need it to check who has joined.")
        return
    link = args[1] if len(args) > 1 else (f"https://t.me/{chat.username}" if chat.username else chat.invite_link)
    if not link:
        try:
            link = await client.export_chat_invite_link(chat.id)
        except RPCError:
            await say("I couldn't make an invite link. Send it yourself: "
                      "<code>/forcesub CHANNEL_ID https://t.me/+…</code>")
            return
    await ctx.set_setting("force_sub", {"chat": chat.id, "link": link, "title": chat.title or str(chat.id)})
    await say(f"Force-join is on: <b>{escape(chat.title or str(chat.id))}</b>.")


async def _alias(ctx: Ctx, text: str, say) -> None:
    body = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
    if "=" not in body:
        await say("Usage: <code>/alias parasite 2019 = Gisaengchung</code>")
        return
    left, alias = (x.strip() for x in body.split("=", 1))
    film = ctx.index.get(left) if re.fullmatch(r"[0-9a-f]{10}", left) else None
    if film is None:
        res = await asyncio.to_thread(ctx.index.search, left)
        if res.kind != "match":
            names = "\n".join(f"{h.film.label}  <code>{h.film.id}</code>" for h in res.hits[:5])
            await say("I need exactly one film. Repeat with its id:\n" + (names or "(no matches)"))
            return
        film = res.hits[0].film
    await ctx.store.add_alias(film.id, alias)
    await asyncio.to_thread(ctx.index.add_alias, film.id, alias)
    await say(f"“{escape(alias)}” now finds <b>{escape(film.label)}</b>.")


# ---------------------------------------------------------------- background job
async def requests_job(ctx: Ctx, client, every: int = 600) -> None:
    """Tell requesters when their film shows up (exact title matches only)."""
    while True:
        await asyncio.sleep(every)
        if not ctx.ready:
            continue
        try:
            for req in await ctx.store.open_requests(200):
                res = await asyncio.to_thread(ctx.index.search, req["text"])
                if res.kind == "match" and res.hits[0].match == "exact" and not res.corrected \
                        and not res.year_relaxed:
                    label = res.hits[0].film.label
                    for uid in req["users"]:
                        try:
                            await client.send_message(uid, f"<b>{escape(label)}</b> is now available. "
                                                           "Send me the name to get it.", parse_mode=HTML)
                        except FloodWait as e:
                            await asyncio.sleep(e.value + 1)
                        except Exception:
                            pass                              # blocked the bot, etc.
                    await ctx.store.close_request(req["key"])
        except Exception:
            log.exception("requests job failed")


# ---------------------------------------------------------------- registration
def _bind(ctx: Ctx, fn):
    async def callback(client, obj):
        try:
            await fn(ctx, client, obj)
        except Exception:
            log.exception("handler %s failed", fn.__name__)
    return callback


def register(client, ctx: Ctx) -> None:
    admins = filters.user(list(ctx.cfg.admins))
    add = client.add_handler
    add(MessageHandler(_bind(ctx, cmd_start), filters.command("start") & filters.private))
    add(MessageHandler(_bind(ctx, on_admin_cmd), filters.command(ADMIN_CMDS) & filters.private & admins))
    add(MessageHandler(_bind(ctx, on_text), filters.text & ~filters.regex(r"^/") & ~filters.bot
                       & (filters.private | filters.group)))
    add(MessageHandler(_bind(ctx, on_channel_file),
                       filters.channel & (filters.document | filters.video | filters.audio)))
    add(CallbackQueryHandler(_bind(ctx, on_cb)))
