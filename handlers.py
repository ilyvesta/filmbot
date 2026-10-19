"""Telegram wiring (Kurigram / Pyrogram API). Logic that doesn't need Telegram
lives in index.py, ui.py, parse.py and store.py."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from dataclasses import dataclass
from html import escape

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import ChannelInvalid, ChannelPrivate, FloodWait, RPCError, UserNotParticipant
from pyrogram.handlers import CallbackQueryHandler, ChatJoinRequestHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import ui
from ctx import Ctx, Session
from index import film_id
from normalize import core_key, normalize
from parse import SUB_EXTS, VIDEO_EXTS, detail_lines, is_junk, label_of, parse_best, quality_key_of
from store import DEDUPE_BATCH, SKIP_CAP, FileRec
from ui import Btn

log = logging.getLogger("moviebot")
HTML = ParseMode.HTML
GROUPS = (ChatType.GROUP, ChatType.SUPERGROUP)
ADMIN_CMDS = ["stats", "forcesub", "settings", "index", "indexstop", "requests", "misses", "alias", "ban",
              "unban", "dedupe", "skipped", "picks", "customize", "setwelcome", "setabout", "setimage",
              "clearimage", "setlinks", "clearlinks", "setfooter", "setpicks", "preview"]
AD_CHOICES = [0, 5, 10, 15, 30, 60]
TTL_CHOICES = [2, 3, 4, 6, 10]
REQUEST_PINGS = {5, 10, 25, 50, 100, 250}
REPORT_PINGS = {3, 10, 25}


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
    """Edit a message in place (text or photo caption)."""
    async def show(text, rows=None):
        try:
            if getattr(msg, "photo", None):
                return await msg.edit_caption(text, parse_mode=HTML, reply_markup=kb(rows))
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


async def _send(client, target: int, text: str, rows=None) -> None:
    try:
        await client.send_message(target, text, parse_mode=HTML, reply_markup=kb(rows))
    except Exception as e:
        log.warning("couldn't message %s: %s", target, e)


async def notify_admins(ctx: Ctx, client, text: str, rows=None) -> None:
    """Alerts about the bot itself: always a direct message to every admin."""
    for a in ctx.cfg.admins:
        await _send(client, a, text, rows)


async def notify_inbox(ctx: Ctx, client, text: str, rows=None) -> None:
    """Requests and file reports: the inbox channel if set, otherwise DM the admins."""
    if ctx.cfg.inbox_channel:
        await _send(client, ctx.cfg.inbox_channel, text, rows)
    else:
        await notify_admins(ctx, client, text, rows)


async def load_variants(ctx: Ctx, fid: str) -> list[FileRec]:
    files = await ctx.store.film_files(fid)
    files.sort(key=lambda r: ((r.ext or "").lower() not in SUB_EXTS, quality_key_of(r.res, r.src), r.size),
               reverse=True)
    return files


def rec_lines(rec: FileRec) -> tuple[str, str]:
    return detail_lines(rec.res, rec.src, rec.codec, rec.size, rec.langs, rec.tags, rec.ext, rec.grp)


# ---------------------------------------------------------------- force-join (up to 4 channels)
async def _member(ctx: Ctx, client, uid: int, chan: dict, fresh: bool = False) -> bool:
    """True if this person has joined (or we can't tell, in which case they're let in)."""
    cid = chan["chat"]
    if not fresh and ctx.joined_recently(uid, cid):
        return True
    for attempt in (1, 2):
        try:
            mem = await client.get_chat_member(cid, uid)
            ok = mem.status != ChatMemberStatus.LEFT      # banned there? don't lock them out here
            break
        except UserNotParticipant:
            return False                                   # also what a pending join request looks like
        except FloodWait as e:
            if attempt == 2:
                return True                                # can't check right now: let them through
            await asyncio.sleep(min(e.value + 1, 5))
        except Exception as e:                             # glitch, or the bot lost its admin rights
            log.warning("force-join check failed for %s: %s", cid, e)
            if ctx.first_time(f"fs-fail-{cid}"):
                await notify_admins(ctx, client, f"I can't check who joined <b>{escape(chan['title'])}</b>, so I'm "
                                                 "letting people through. Am I still an admin there? (/forcesub list)")
            return True
    if ok:
        ctx.mark_joined(uid, cid)
    return ok


async def missing_channels(ctx: Ctx, client, uid: int, only: set[int] | None = None, fresh: bool = False) -> list[dict]:
    """The force-join channels this person still has to join. All checked at the same time."""
    if ctx.is_admin(uid):
        return []
    subs = [c for c in await ctx.subs() if only is None or c["chat"] in only]
    if not subs:
        return []
    results = await asyncio.gather(*[_member(ctx, client, uid, c, fresh) for c in subs])
    return [c for c, ok in zip(subs, results) if not ok]


async def require_join(ctx: Ctx, client, uid: int, show, action: dict) -> bool:
    """True = blocked. Shows only the missing channels and remembers what the person was trying to do."""
    missing = await missing_channels(ctx, client, uid)
    if not missing:
        return False
    ctx.set_pending(uid, action, {c["chat"] for c in missing})
    t, rows = ui.join_screen(missing)
    sent = await show(t, rows)
    ctx.watch(sent.chat.id, sent.id, (await ctx.settings())["result_ttl_min"])
    return True


async def _joined(ctx: Ctx, client, cb) -> None:
    """The "I've joined" button: re-check only what was missing, then carry on with what they wanted."""
    uid, msg = cb.from_user.id, cb.message
    pend = ctx.get_pending(uid)
    if not pend:
        await cb.answer("That expired. Send the film name again.", show_alert=True)
        return
    missing = await missing_channels(ctx, client, uid, only=pend["missing"], fresh=True)
    if missing:
        pend["missing"] = {c["chat"] for c in missing}
        t, rows = ui.join_screen(missing, still=True)
        await edit_of(msg)(t, rows)
        await cb.answer("Not all joined yet.")
        return
    ctx.pending.pop(uid, None)
    await cb.answer()
    if pend["type"] == "search":
        await run_search(ctx, edit_of(msg), uid, pend["text"], pend.get("group", False))
        return
    rec = await ctx.store.get_file(pend["uid"])
    if not rec:
        await edit_of(msg)("That file isn't available any more.")
        return
    await edit_of(msg)("Thanks. Sending your file…")
    await deliver(ctx, client, msg.chat.id, rec)


async def on_join_request(ctx: Ctx, client, req) -> None:
    """Channels that ask people to request to join: let them in, so the force-join check can pass."""
    if not (await ctx.settings())["auto_approve"]:
        return
    if any(c["chat"] == req.chat.id for c in await ctx.subs()):
        try:
            await client.approve_chat_join_request(req.chat.id, req.from_user.id)
        except Exception as e:
            log.warning("couldn't approve join request: %s", e)
            if ctx.first_time(f"approve-{req.chat.id}"):
                await notify_admins(ctx, client, "I couldn't approve a join request. In that channel I need the "
                                                 "admin right to invite users.")


# ---------------------------------------------------------------- delivering files
async def deliver(ctx: Ctx, client, chat_id: int, rec: FileRec) -> bool:
    film = ctx.index.get(rec.film)
    s = await ctx.settings()
    minutes = s["autodelete_min"]
    cap = ui.caption(film.title if film else "File", film.year if film else None, rec, minutes, s["footer"])
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
    if minutes:                                   # stored in the database, so it survives restarts
        await ctx.store.schedule_delete(chat_id, sent.id, time.time() + minutes * 60)
    return True


async def _drop(ctx: Ctx, rec: FileRec) -> None:
    if await ctx.store.remove_file(rec.uid):
        ctx.index.adjust_files(rec.film, -1)


async def sweeper(ctx: Ctx, client, tick: int = 15) -> None:
    """Deletes expired search messages and delivered files."""
    n, last_prune = 0, 0.0
    while True:
        await asyncio.sleep(tick)
        n += 1
        try:
            for chat, msg in ctx.due():
                try:
                    await client.delete_messages(chat, msg)
                except Exception:
                    pass
            if n % 2 == 0:
                due = await ctx.store.due_deletes(time.time())
                for _, chat, msg in due:
                    try:
                        await client.delete_messages(chat, msg)
                    except Exception:
                        pass
                await ctx.store.clear_deletes([k for k, _, _ in due])
            if time.time() - last_prune > 86400:
                last_prune = time.time()
                await ctx.store.prune_daily()
                await ctx.store.prune_skips()
        except Exception:
            log.exception("sweeper failed")


# ---------------------------------------------------------------- search
def _results_view(ctx: Ctx, s: Session):
    back = Btn("‹ Discover", s.back) if s.back else None
    return ui.results_screen(s.hits, s.page, s.qkey, s.corrected, s.year_relaxed, s.year,
                             suggest=s.kind == "suggest", header=s.header, back=back, requestable=not s.header)


async def _film_view(ctx: Ctx, s: Session):
    fid, title, year = s.film
    st = await ctx.settings()
    note = ""
    if s.corrected:
        note += f"Showing results for <b>{escape(s.qkey)}</b>.\n"
    if s.year_relaxed and s.year:
        note += f"No {s.year} version here.\n"
    return ui.film_screen(title, year, s.variants, s.vpage, can_back=len(s.hits) > 1 or bool(s.header),
                          is_admin=ctx.is_admin(s.owner) and not s.group, fid=fid, note=note, mode=s.mode,
                          picked=fid in st["picks"])


async def _track(ctx: Ctx, sent, sess: Session) -> None:
    ctx.sessions.put(sent.chat.id, sent.id, sess)
    ctx.watch(sent.chat.id, sent.id, (await ctx.settings())["result_ttl_min"])


async def run_search(ctx: Ctx, show, owner: int, text: str, group: bool = False) -> None:
    res = await asyncio.to_thread(ctx.index.search, text, 30)     # up to 5 pages of films
    if res.kind == "empty":
        await show(ui.EMPTY_QUERY)
        return
    if res.kind == "none":
        await ctx.store.add_miss(res.query)
        t, rows = ui.not_found_screen(text)
        await _track(ctx, await show(t, rows), Session(owner, text, "none", [], qkey=res.query, group=group))
        return
    hits = [(h.film.id, h.film.label, h.film.file_count) for h in res.hits]
    sess = Session(owner, text, res.kind, hits, res.corrected, res.year, res.year_relaxed, qkey=res.query,
                   group=group)
    if res.kind == "match":
        f = res.hits[0].film
        sess.film, sess.variants = (f.id, f.title, f.year), await load_variants(ctx, f.id)
        t, rows = await _film_view(ctx, sess)
    else:
        t, rows = _results_view(ctx, sess)
    await _track(ctx, await show(t, rows), sess)


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
    if await require_join(ctx, client, uid, show, {"type": "search", "text": text, "group": group}):
        return
    await run_search(ctx, show, uid, text, group)


# ---------------------------------------------------------------- start / help / discover
async def send_home(ctx: Ctx, m) -> None:
    s = await ctx.settings()
    text, rows = ui.home_screen(s, getattr(m.from_user, "first_name", "") or "", len(ctx.index) if ctx.ready else 0)
    if s["image"]:
        try:
            await m.reply_photo(s["image"], caption=text, parse_mode=HTML, reply_markup=kb(rows))
            return
        except RPCError as e:
            log.warning("welcome image failed, sending text: %s", e)
    await m.reply_text(text, parse_mode=HTML, reply_markup=kb(rows))


async def cmd_start(ctx: Ctx, client, m) -> None:
    uid = m.from_user.id
    if uid in ctx.banned:
        await m.reply_text(ui.BANNED)
        return
    await ctx.store.touch_user(uid)
    arg = m.command[1] if len(m.command) > 1 else ""
    if arg.startswith("f_"):
        if await require_join(ctx, client, uid, reply_to(m), {"type": "file", "uid": arg[2:]}):
            return
        rec = await ctx.store.get_file(arg[2:])
        if rec:
            await deliver(ctx, client, m.chat.id, rec)
        else:
            await m.reply_text("That file isn't available any more.")
        return
    await send_home(ctx, m)


async def cmd_info(ctx: Ctx, client, m) -> None:
    """/help /about /discover for everyone."""
    if not m.from_user or m.from_user.id in ctx.banned:
        return
    cmd, s = m.command[0].lower(), await ctx.settings()
    if cmd == "help":
        t, rows = ui.help_screen(s)
        await reply_to(m)(t, rows)
    elif cmd == "about":
        t, rows = ui.about_screen(s, len(ctx.index))
        await reply_to(m)(t, rows)
    else:
        t, rows = ui.discover_screen(s)
        sent = await reply_to(m)(t, rows)
        await _track(ctx, sent, Session(m.from_user.id, "", "discover", [], group=m.chat.type in GROUPS))


async def _discover_hits(ctx: Ctx, which: str) -> tuple[str, list[tuple[str, str, int]]]:
    s = await ctx.settings()
    if which == "pick":
        name, fids = s["picks_name"], list(s["picks"])
    elif which == "pop":
        name = "Popular this week"
        if ctx.top_cache is None or time.monotonic() - ctx.top_cache[0] > 600:
            ctx.top_cache = (time.monotonic(), [f for f, _ in await ctx.store.top_films(7, 30)])
        fids = ctx.top_cache[1]
    else:
        name, fids = "Recently added", await ctx.store.recent_films(30)
    hits = []
    for fid in fids:
        f = ctx.index.get(fid)
        if f:
            hits.append((f.id, f.label, f.file_count))
    return name, hits


# ---------------------------------------------------------------- callbacks
async def on_cb(ctx: Ctx, client, cb) -> None:
    uid, data = cb.from_user.id, cb.data or ""
    if uid in ctx.banned:
        await cb.answer(ui.BANNED, show_alert=True)
        return
    head, _, arg = data.partition(":")
    msg = cb.message
    if head == "noop":
        await cb.answer()
        return
    if head == "fsok":
        await _joined(ctx, client, cb)
        return
    if head == "nav":
        s = await ctx.settings()
        t, rows = (ui.help_screen(s) if arg == "help" else ui.about_screen(s, len(ctx.index)) if arg == "about"
                   else ui.home_screen(s, cb.from_user.first_name or "", len(ctx.index)))
        await edit_of(msg)(t, rows)
        await cb.answer()
        return
    if head == "rep":
        await msg.edit_reply_markup(kb(ui.report_rows(arg)))
        await cb.answer()
        return
    if head == "rr":
        await _report(ctx, client, cb, arg)
        return
    if head in ("st", "rmfile", "rdis", "ixstop", "ddy", "ddn", "sk", "skt"):
        if not ctx.is_admin(uid):
            await cb.answer("Admins only.", show_alert=True)
            return
        await _admin_cb(ctx, client, cb, head, arg)
        return
    if head == "dc":
        s = await ctx.settings()
        t, rows = ui.discover_screen(s)
        sess = ctx.sessions.get(msg.chat.id, msg.id)
        if sess and (sess.owner == uid or sess.group):         # already inside a Discover message
            sess.kind, sess.hits, sess.header, sess.back, sess.film = "discover", [], None, None, None
            sent = await edit_of(msg)(t, rows)
        else:                                                  # opened from the welcome message
            sent = await msg.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
        await _track(ctx, sent, Session(uid, "", "discover", [], group=msg.chat.type in GROUPS))
        await cb.answer()
        return

    sess = ctx.sessions.get(msg.chat.id, msg.id)
    if not sess:
        await cb.answer(ui.EXPIRED, show_alert=True)
        return
    if sess.owner != uid and not sess.group:               # in groups anyone can tap; files go to their own DM
        await cb.answer(ui.NOT_YOURS, show_alert=True)
        return
    ctx.watch(msg.chat.id, msg.id, (await ctx.settings())["result_ttl_min"])      # activity pushes expiry back
    show = edit_of(msg)

    if head == "dx":
        if sess.group and uid != sess.owner and not ctx.is_admin(uid):
            await cb.answer("Only the person who opened this can close it.", show_alert=True)
            return
        ctx.expiry.pop((msg.chat.id, msg.id), None)
        ctx.sessions.drop(msg.chat.id, msg.id)
        try:
            await msg.delete()
        except RPCError:
            pass
        await cb.answer()
        return
    if head == "dl":
        name, hits = await _discover_hits(ctx, arg)
        sess.kind, sess.hits, sess.header, sess.back, sess.page, sess.film = "list", hits, name, "dc", 0, None
        if hits:
            t, rows = _results_view(ctx, sess)
        else:
            t, rows = f"<b>{escape(name)}</b>\nNothing here yet.", [[Btn("‹ Discover", "dc")]]
        await show(t, rows)
    elif head == "r" and arg.isdigit():
        sess.page = int(arg)
        t, rows = _results_view(ctx, sess)
        await show(t, rows)
    elif head == "b":
        sess.film, sess.mode = None, ""
        t, rows = _results_view(ctx, sess)
        await show(t, rows)
    elif head == "f":
        film = ctx.index.get(arg)
        if not film:
            await cb.answer("That one's gone.", show_alert=True)
            return
        sess.film, sess.vpage, sess.mode = (film.id, film.title, film.year), 0, ""
        sess.variants = await load_variants(ctx, film.id)
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
    elif head == "v" and arg.isdigit() and sess.film:
        sess.vpage, sess.mode = int(arg), ""
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
    elif head == "s" and arg.isdigit() and sess.film and int(arg) < len(sess.variants):
        rec = sess.variants[int(arg)]
        if msg.chat.type in GROUPS:
            await cb.answer(url=ctx.deep_link(rec.uid))      # opens the bot in private, then delivers
            return
        if await require_join(ctx, client, uid, reply_to(msg), {"type": "file", "uid": rec.uid}):
            await cb.answer()
            return
        await cb.answer("Sending…")
        await deliver(ctx, client, msg.chat.id, rec)
        return
    elif head == "rq":
        await _request(ctx, client, cb, sess, msg.chat.type in GROUPS)
        return
    elif head in ("rm", "rd", "rmy", "pk") and sess.film:
        if not ctx.is_admin(uid):
            await cb.answer("Admins only.", show_alert=True)
            return
        await _film_admin(ctx, cb, sess, head, arg, show)
        return
    await cb.answer()


async def _film_admin(ctx: Ctx, cb, sess: Session, head: str, arg: str, show) -> None:
    if head == "rm":
        sess.mode = "remove"
    elif head == "rd" and arg.isdigit() and int(arg) < len(sess.variants):
        t, rows = ui.confirm_remove_screen(sess.variants[int(arg)], rec_lines(sess.variants[int(arg)]), sess.vpage)
        await show(t, rows)
        await cb.answer()
        return
    elif head == "rmy":
        rec = await ctx.store.remove_file(arg)
        if rec:
            ctx.index.adjust_files(rec.film, -1)
        sess.variants = [v for v in sess.variants if v.uid != arg]
        sess.mode = ""
    elif head == "pk":
        picks = list((await ctx.settings())["picks"])
        if arg in picks:
            picks.remove(arg)
            await cb.answer("Removed from the list.")
        else:
            picks.append(arg)
            await cb.answer("Added to the list.")
        await ctx.set_setting("picks", picks)
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
        return
    t, rows = await _film_view(ctx, sess)
    await show(t, rows)
    await cb.answer()


async def _request(ctx: Ctx, client, cb, sess: Session, group: bool) -> None:
    qkey = core_key(normalize(sess.query)) or sess.qkey
    count, new = await ctx.store.add_request(qkey, sess.query[:80], cb.from_user.id)
    extra = " Open me in private and press Start, or I can't message you." if group else ""
    await cb.answer("Requested. I'll message you when it's added." + extra, show_alert=True)
    if new or count in REQUEST_PINGS:
        await notify_inbox(ctx, client, f"Request: <b>{escape(sess.query)}</b>  ({count} "
                                        f"{'person' if count == 1 else 'people'})")


async def _report(ctx: Ctx, client, cb, arg: str) -> None:
    code, _, uid_arg = arg.partition(":")
    if code == "x":                                             # cancelled
        await cb.message.edit_reply_markup(kb([[Btn("Report a problem", f"rep:{uid_arg}")]]))
        await cb.answer()
        return
    if code not in ui.REASON_NAMES:
        await cb.answer()
        return
    rec = await ctx.store.get_file(uid_arg)
    await cb.message.edit_reply_markup(kb([[Btn("Reported, thanks", "noop")]]))
    await cb.answer("Thanks, I've passed it on.", show_alert=True)
    if not rec:
        return
    total, reasons, fresh = await ctx.store.add_report(uid_arg, code, cb.from_user.id)
    if fresh and (total == 1 or total in REPORT_PINGS):
        film = ctx.index.get(rec.film)
        l1, l2 = rec_lines(rec)
        why = ", ".join(f"{ui.REASON_NAMES.get(c, c)} ×{n}" for c, n in reasons.items())
        await notify_inbox(
            ctx, client,
            f"Problem report ({total}): <b>{escape(film.label if film else rec.film)}</b>\n"
            f"{escape(l1)}\n{escape(l2)}\n\n<b>Reason:</b> {escape(why)}",
            [[Btn("Remove this file", f"rmfile:{rec.uid}"), Btn("Dismiss", f"rdis:{rec.uid}")]])


async def _admin_cb(ctx: Ctx, client, cb, head: str, arg: str) -> None:
    show = edit_of(cb.message)
    if head == "st":
        s = await ctx.settings()
        if arg == "ad":
            cur = s["autodelete_min"]
            await ctx.set_setting("autodelete_min", AD_CHOICES[(AD_CHOICES.index(cur) + 1) % len(AD_CHOICES)]
                                  if cur in AD_CHOICES else 0)
        elif arg == "rt":
            cur = s["result_ttl_min"]
            await ctx.set_setting("result_ttl_min", TTL_CHOICES[(TTL_CHOICES.index(cur) + 1) % len(TTL_CHOICES)]
                                  if cur in TTL_CHOICES else 4)
        elif arg == "pc":
            await ctx.set_setting("protect", not s["protect"])
        elif arg == "gr":
            await ctx.set_setting("groups", not s["groups"])
        elif arg == "se":
            await ctx.set_setting("index_series", not s["index_series"])
        elif arg == "ap":
            await ctx.set_setting("auto_approve", not s["auto_approve"])
        t, rows = ui.settings_screen(await ctx.settings())
        await show(t, rows)
    elif head == "rmfile":
        rec = await ctx.store.remove_file(arg)
        if rec:
            ctx.index.adjust_files(rec.film, -1)
        await ctx.store.clear_reports(arg)
        await show("File removed from the database.")
    elif head == "rdis":
        await ctx.store.clear_reports(arg)
        await show("Dismissed.")
    elif head == "ixstop":
        if ctx.index_job:
            ctx.index_job["stop"] = True
        await cb.answer("Stopping after this batch.")
        return
    elif head == "ddy":
        items, ctx.pending_dedupe = ctx.pending_dedupe or [], None
        if not items:
            await show("That list expired. Run /dedupe again.")
        else:
            await show(f"Removing {len(items):,} copies…")
            n = await ctx.store.delete_files(items)
            per: dict[str, int] = {}
            for i in items:
                per[i["film"]] = per.get(i["film"], 0) + 1
            for f, c in per.items():
                ctx.index.adjust_files(f, -c)
            await show(f"Removed {n:,} look-alike copies and kept one of each. "
                       "They're listed under /skipped, Look-alikes, with the copy that was kept.")
    elif head == "ddn":
        ctx.pending_dedupe = None
        await show("Okay, nothing removed.")
    elif head == "sk":
        await _skipped_view(ctx, show, arg)
    elif head == "skt":
        await _skipped_file(ctx, client, cb.message.chat.id, arg)
    await cb.answer()


# ---------------------------------------------------------------- indexing
@dataclass
class IndexSummary:
    seen: int = 0
    added: int = 0
    known: int = 0
    lookalike: int = 0
    series: int = 0
    unreadable: int = 0
    other: int = 0

    def add(self, o: "IndexSummary") -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(o, k))


def _media_of(m):
    """(media object, extension) for files worth indexing, else (None, None)."""
    media = m.document or m.video
    if not media:
        return None, None
    name = getattr(media, "file_name", None) or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if m.video or ext in VIDEO_EXTS or ext in SUB_EXTS or str(getattr(media, "mime_type", "") or "").startswith("video/"):
        return media, ext
    return None, None


async def index_messages(ctx: Ctx, msgs) -> IndexSummary:
    s = IndexSummary()
    cfg = await ctx.store.settings()
    recs: list[FileRec] = []
    films: dict[str, tuple] = {}
    notes: list[dict] = []
    for m in msgs:
        if not (m.document or m.video or m.audio):
            continue
        s.seen += 1
        media, _ = _media_of(m)
        if media is None:
            s.other += 1
            continue
        fname = getattr(media, "file_name", None)
        size = media.file_size or 0
        p = parse_best(fname, m.caption)
        if is_junk(p.title) or not normalize(p.title):
            s.unreadable += 1
            notes.append({"kind": "unreadable", "uid": media.file_unique_id, "name": fname or m.caption or "(no name)",
                          "ch": m.chat.id, "mid": m.id, "size": size})
            continue
        if p.kind == "series" and not cfg["index_series"]:
            s.series += 1
            notes.append({"kind": "series", "uid": media.file_unique_id, "name": fname or p.raw,
                          "ch": m.chat.id, "mid": m.id, "size": size})
            continue
        fid = film_id(normalize(p.title), p.year)
        films[fid] = (p.title, p.year)
        recs.append(FileRec(media.file_unique_id, fid, m.chat.id, m.id, size, p.resolution,
                            p.source, p.languages, (fname or "")[:120], p.codec, p.group, p.tags, p.ext))
    new, known, look = await ctx.store.add_files(recs, films)
    s.added, s.known, s.lookalike = len(new), known, len(look)
    for r, kept in look:
        notes.append({"kind": "lookalike", "uid": r.uid, "name": r.name, "ch": r.ch, "mid": r.mid,
                      "size": r.size, "keep": kept})
    await ctx.store.note_skips(notes)
    skipped_now = {r.uid for r, _ in look}
    await ctx.store.clear_skips([r.uid for r in recs if r.uid not in skipped_now])   # readable now: off the list
    per: dict[str, int] = {}
    for r in new:
        per[r.film] = per.get(r.film, 0) + 1
    if per:
        await asyncio.to_thread(ctx.index.add_films, [(films[f][0], films[f][1], n, 0, ()) for f, n in per.items()])
    return s


async def on_channel_file(ctx: Ctx, client, m) -> None:
    if m.chat.id in ctx.cfg.file_channels:
        await index_messages(ctx, [m])


EMPTY_BLOCKS_TO_STOP = 25        # 25 x 200 = 5,000 empty ids in a row after the last post we know about


async def _fetch(client, ch: int, ids: list[int]):
    while True:
        try:
            return await client.get_messages(ch, ids)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)


async def find_last_id(client, ch: int, floor: int = 1) -> int:
    """Newest message id in a channel, found by probing (bots can't ask Telegram for it)."""
    async def any_in(start: int, n: int = 100) -> bool:
        return any(x and not x.empty for x in await _fetch(client, ch, list(range(start, start + n))))
    lo, hi = max(floor, 1), max(floor, 1) + 1000
    while await any_in(hi):
        lo, hi = hi, hi * 2
    while hi - lo > 100:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if await any_in(mid) else (lo, mid)
    best = lo
    for start in range(lo, hi + 100, 200):
        for x in await _fetch(client, ch, list(range(start, min(start + 200, hi + 100)))):
            if x and not x.empty:
                best = max(best, x.id)
    return best


def _progress_text(ch: int, pos: int, first: int, last: int, tot: IndexSummary, started: float, state: str) -> str:
    done = max(pos - first, 0)
    span = max(last - first + 1, 1)
    pct = min(100, round(100 * done / span))
    rate = done / max(time.monotonic() - started, 1)
    eta = f" · about {round((last - pos) / rate / 60)} min left" if rate > 0 and state == "running" and pos < last else ""
    return (f"<b>Indexing</b> <code>{ch}</code>\n"
            f"Message {pos:,} of {last:,}  ({pct}%){eta}\n\n"
            f"Added <b>{tot.added:,}</b>\n"
            f"Already had {tot.known:,} · look-alikes skipped {tot.lookalike:,}\n"
            f"Skipped: {tot.series:,} series-looking · {tot.unreadable:,} unreadable\n"
            + (f"\n<b>{state}</b>" if state != "running" else ""))


async def run_index(ctx: Ctx, client, status, targets: list[tuple[int, int, int | None]]) -> None:
    """targets: (channel, first message id, last id or None to detect). Updates `status` live."""
    job = ctx.index_job = {"stop": False}
    await ctx.store.set_setting("index_job", {"targets": [[c, f, l] for c, f, l in targets]})
    stop_rows = [[Btn("Stop", "ixstop")]]
    try:
        for ch, first, last in targets:
            if job["stop"]:
                break
            await _edit(status, f"Looking for the newest post in <code>{ch}</code>…", stop_rows)
            last = last or await find_last_id(client, ch, first)
            tot, started, tick, pos, empty_run = IndexSummary(), time.monotonic(), time.monotonic(), first, 0
            while not job["stop"] and not (pos > last and empty_run >= EMPTY_BLOCKS_TO_STOP):
                ids = list(range(pos, pos + 200))
                live = [x for x in await _fetch(client, ch, ids) if x and not x.empty]
                empty_run = 0 if live else empty_run + 1
                if live:
                    last = max(last, max(x.id for x in live))
                    tot.add(await index_messages(ctx, live))
                pos = ids[-1] + 1
                if time.monotonic() - tick >= 5:
                    tick = time.monotonic()
                    await ctx.store.set_setting(f"cp_{ch}", pos)
                    await _edit(status, _progress_text(ch, min(pos, last), first, last, tot, started, "running"), stop_rows)
            pos = min(pos, last + 1)
            await ctx.store.set_setting(f"cp_{ch}", pos)
            state = "Stopped. Run /index to continue from here." if job["stop"] else "Done."
            await _edit(status, _progress_text(ch, min(pos, last), first, last, tot, started, state), None)
        await ctx.store.set_setting("index_job", None)
    except Exception as e:
        log.exception("indexing failed")
        await _edit(status, f"Indexing stopped with an error: {escape(str(e))}\nRun /index to resume.", None)
    finally:
        ctx.index_job = None


async def _edit(status, text: str, rows) -> None:
    try:
        await status.edit_text(text, parse_mode=HTML, reply_markup=kb(rows))
    except FloodWait as e:
        await asyncio.sleep(min(e.value, 10))
    except RPCError:
        pass


async def resume_index(ctx: Ctx, client) -> None:
    """If the bot was restarted mid-index, carry on automatically."""
    job = (await ctx.store.settings()).get("index_job")
    if not job or not job.get("targets") or not ctx.cfg.admins:
        return
    cps = await ctx.store.settings()
    targets = [(c, int(cps.get(f"cp_{c}") or f), l) for c, f, l in job["targets"]]
    status = await client.send_message(sorted(ctx.cfg.admins)[0], "I restarted while indexing. Resuming where I left off…")
    asyncio.create_task(run_index(ctx, client, status, targets))


# ---------------------------------------------------------------- admin commands
def _reply_html(m) -> str | None:
    src = m.reply_to_message
    if not src:
        return None
    for field in (src.text, src.caption):
        if field:
            return getattr(field, "html", None) or str(field)
    return None


def _inline_arg(m) -> str:
    parts = (m.text or m.caption or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _skip_entries(ctx: Ctx, kind: str, rows: list[dict]) -> list[dict]:
    kept = await ctx.store.files_by_uid([r["keep"] for r in rows if r.get("keep")]) if kind == "lookalike" else {}
    out = []
    for r in rows:
        e = {"name": r["n"], "size": r["z"], "ch": r["c"], "mid": r["m"]}
        if kind == "lookalike":
            k = kept.get(r.get("keep"))
            e.update(kept_gone=k is None, kept_name=k.name if k else "", kept_ch=k.ch if k else 0, kept_mid=k.mid if k else 0)
        out.append(e)
    return out


async def _skipped_view(ctx: Ctx, show, arg: str) -> None:
    if arg == "m" or arg.partition(":")[0] not in ui.SKIP_KINDS:
        t, rows = ui.skipped_menu(await ctx.store.skip_counts(), SKIP_CAP)
        await show(t, rows)
        return
    code, _, pg = arg.partition(":")
    page = int(pg) if pg.isdigit() else 0
    kind = ui.SKIP_KINDS[code][0]
    total, rows = await ctx.store.skips_page(kind, page, ui.SKIP_PAGE)
    t, btns = ui.skip_list_screen(code, await _skip_entries(ctx, kind, rows), page, total)
    await show(t, btns)


async def _skipped_file(ctx: Ctx, client, chat_id: int, code: str) -> None:
    if code not in ui.SKIP_KINDS:
        return
    kind, label, blurb = ui.SKIP_KINDS[code]
    rows = await ctx.store.skips_all(kind)
    kept = await ctx.store.files_by_uid([r["keep"] for r in rows if r.get("keep")]) if kind == "lookalike" else {}

    def where(ch: int, mid: int) -> str:
        return ui.post_link(ch, mid) or f"chat {ch} message {mid}"
    lines = [f"{label} ({len(rows)} listed, newest first)", blurb, ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['n'] or '(no name)'} | {r['z']} bytes | {where(r['c'], r['m'])}")
        if kind == "lookalike":
            k = kept.get(r.get("keep"))
            lines.append("   kept: " + (f"{k.name or k.uid} | {where(k.ch, k.mid)}" if k else "(removed since)"))
    data = io.BytesIO("\n".join(lines).encode("utf-8"))
    data.name = f"skipped-{kind}.txt"
    await client.send_document(chat_id, data, caption=f"{label}: {len(rows):,} listed.")


async def on_admin_cmd(ctx: Ctx, client, m) -> None:
    cmd, args = m.command[0].lower(), m.command[1:]
    say = lambda t, rows=None: m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))   # noqa: E731

    if cmd == "stats":
        c = await ctx.store.counts()
        mem, s = rss_mb(), await ctx.settings()
        fs = s["force_sub"]
        lines = [f"Files: {c['files']:,}", f"Films: {c['films']:,}  (search index: {len(ctx.index):,})",
                 f"Users: {c['users']:,}", f"Open requests: {c['requests']}"]
        if c["db_mb"] is not None:
            lines.append(f"Database: {c['db_mb']} MB of 512")
        if mem:
            lines.append(f"Bot memory: {mem} MB of 512" + ("  (getting tight)" if mem > 420 else ""))
        lines.append(f"Force-join: {escape(fs['title']) if fs else 'off'}")
        top = []
        for fid, n in (await ctx.store.top_films(7, 5)):
            f = ctx.index.get(fid)
            if f:
                top.append(f"• {escape(f.label)}  ({n})")
        if top:
            lines += ["", "Most received this week:"] + top
        miss = await ctx.store.top_misses(5)
        if miss:
            lines += ["", "Most searched but not found:"] + [f"• {escape(k)}  ({n})" for k, n in miss]
        await say("\n".join(lines))
    elif cmd == "settings":
        t, rows = ui.settings_screen(await ctx.settings())
        await say(t, rows)
    elif cmd == "customize":
        await say(ui.CUSTOMIZE)
    elif cmd == "preview":
        await send_home(ctx, m)
    elif cmd == "forcesub":
        await _forcesub(ctx, client, m, args, say)
    elif cmd == "index":
        await _index_cmd(ctx, client, m, args, say)
    elif cmd == "indexstop":
        if ctx.index_job:
            ctx.index_job["stop"] = True
            await say("Stopping after the current batch.")
        else:
            await say("Nothing is running.")
    elif cmd == "dedupe":
        await say("Scanning for look-alike files. This can take a few minutes…")
        items = await ctx.store.lookalikes()
        if not items:
            await say("No look-alikes found.")
        else:
            ctx.pending_dedupe = items
            more = (f"\nThat's one round of {DEDUPE_BATCH:,}. If there are more, run /dedupe again afterwards."
                    if len(items) >= DEDUPE_BATCH else "")
            await say(f"Found <b>{len(items):,}</b> extra copies: same film, exactly the same size, uploaded separately. "
                      f"One of each stays. Remove the extras? You can review them afterwards in /skipped.{more}",
                      [[Btn("Yes, remove them", "ddy"), Btn("No", "ddn")]])
    elif cmd == "skipped":
        t, rows = ui.skipped_menu(await ctx.store.skip_counts(), SKIP_CAP)
        await say(t, rows)
    elif cmd == "picks":
        s = await ctx.settings()
        lines = []
        for fid in s["picks"]:
            f = ctx.index.get(fid)
            lines.append(f"• {escape(f.label)}" if f else f"• (missing) {fid}")
        await say(f"<b>{escape(s['picks_name'])}</b>\n" + ("\n".join(lines) or "Empty. Use the Hand-pick button on a film page."))
    elif cmd == "setwelcome" or cmd == "setabout":
        key = "welcome" if cmd == "setwelcome" else "about"
        if args and args[0].lower() == "reset":
            await ctx.set_setting(key, None)
            await say("Back to the default text.")
            return
        html = _reply_html(m)
        if not html:
            await say(f"Reply to a message containing the text with <code>/{cmd}</code>.")
            return
        await ctx.set_setting(key, html)
        await say("Saved. /preview to see it.")
    elif cmd == "setimage":
        photo = m.photo or (m.reply_to_message.photo if m.reply_to_message else None)
        if not photo:
            await say("Send a photo with <code>/setimage</code> as its caption, or reply to a photo with it.")
            return
        await ctx.set_setting("image", photo.file_id)
        await say("Image saved. /preview to see it.")
    elif cmd == "clearimage":
        await ctx.set_setting("image", None)
        await say("Image removed.")
    elif cmd == "setlinks":
        raw = _reply_html(m) or _inline_arg(m)
        links = []
        for line in re.sub(r"<[^>]+>", "", raw or "").splitlines():
            if "|" in line:
                label, url = (x.strip() for x in line.split("|", 1))
                url = url if url.startswith(("http://", "https://", "tg://")) else "https://" + url.lstrip("@/")
                if label and "." in url:
                    links.append([label[:30], url])
        if not links:
            await say("Reply to a message with one button per line, like:\n<code>Join our channel | https://t.me/yourchannel</code>")
            return
        await ctx.set_setting("links", links[:8])
        await say(f"Saved {len(links[:8])} button(s). /preview to see them.")
    elif cmd == "clearlinks":
        await ctx.set_setting("links", [])
        await say("Buttons removed.")
    elif cmd == "setfooter":
        if args and args[0].lower() == "off":
            await ctx.set_setting("footer", "")
            await say("Footer removed.")
            return
        html = _reply_html(m) or _inline_arg(m)
        if not html:
            await say("Reply to a message with <code>/setfooter</code>, or <code>/setfooter off</code>.")
            return
        await ctx.set_setting("footer", html)
        await say("Footer saved. It shows under every file.")
    elif cmd == "setpicks":
        name = _inline_arg(m)[:30]
        if not name:
            await say("Usage: <code>/setpicks Name</code>")
            return
        await ctx.set_setting("picks_name", name)
        await say(f"The list is now called <b>{escape(name)}</b>.")
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


async def _index_cmd(ctx: Ctx, client, m, args, say) -> None:
    if ctx.index_job:
        await say("An indexing run is already going. /indexstop to stop it.")
        return
    cps = await ctx.store.settings()
    nums = [a for a in args if re.fullmatch(r"-?\d+", a)]
    if len(nums) != len(args):
        await say("Usage: <code>/index</code> (everything, resuming)\n<code>/index CHANNEL</code>\n"
                  "<code>/index CHANNEL FROM</code> (start at a message number)")
        return
    if not args:
        targets = [(c, int(cps.get(f"cp_{c}") or 1), None) for c in sorted(ctx.cfg.file_channels)]
    else:
        ch = int(args[0])
        if ch not in ctx.cfg.file_channels:
            await say("That channel isn't in FILE_CHANNELS.")
            return
        first = int(args[1]) if len(args) > 1 else int(cps.get(f"cp_{ch}") or 1)
        targets = [(ch, first, int(args[2]) if len(args) > 2 else None)]
    status = await say("Starting…")
    asyncio.create_task(run_index(ctx, client, status, targets))


def _subs_list(subs: list[dict]) -> str:
    if not subs:
        return "Force-join is off."
    return "\n".join(f"{i}. <b>{escape(c['title'])}</b>  <code>{c['chat']}</code>\n    {escape(c['link'])}"
                     for i, c in enumerate(subs, 1))


async def _forcesub(ctx: Ctx, client, m, args, say) -> None:
    subs = await ctx.subs()
    verb = args[0].lower() if args else "list"
    if verb not in ("add", "remove", "list", "off"):
        verb, args = "add", ["add", *args]                     # old style: /forcesub CHANNEL
    if verb == "list":
        await say(_subs_list(subs) + "\n\n<code>/forcesub add CHANNEL [invite link]</code>\n"
                  "<code>/forcesub remove CHANNEL</code> (id, @username or its number above)\n"
                  "<code>/forcesub off</code>\nUp to 4 channels.")
    elif verb == "off":
        await ctx.set_setting("force_subs", [])
        await say("Force-join is off.")
    elif verb == "remove":
        ref = args[1] if len(args) > 1 else ""
        hit = None
        for i, c in enumerate(subs, 1):
            tail = c["link"].rstrip("/").rsplit("/", 1)[-1].lower()
            if ref == str(i) or ref == str(c["chat"]) or ref.lower().lstrip("@") == tail:
                hit = c
                break
        if not hit:
            await say("I couldn't find that one. See <code>/forcesub list</code>.")
            return
        await ctx.set_setting("force_subs", [c for c in subs if c is not hit])
        await say(f"Removed <b>{escape(hit['title'])}</b>.")
    else:
        await _forcesub_add(ctx, client, args[1:], subs, say)


async def _forcesub_add(ctx: Ctx, client, args, subs: list[dict], say) -> None:
    if not args:
        await say("Usage: <code>/forcesub add CHANNEL_ID_or_@username [invite link]</code>")
        return
    ref = int(args[0]) if re.fullmatch(r"-?\d+", args[0]) else args[0]
    try:
        chat = await client.get_chat(ref)
        me = await client.get_chat_member(chat.id, "me")
    except RPCError:
        await say("I can't see that channel. Add me as an admin there first.")
        return
    title = chat.title or str(chat.id)
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await say(f"I'm not an admin in <b>{escape(title)}</b>. Make me an admin there first. "
                  "Telegram only lets me check who has joined if I am.")
        return
    already = any(c["chat"] == chat.id for c in subs)
    if len(subs) >= ctx.MAX_FORCE_SUB and not already:
        await say(f"You can have up to {ctx.MAX_FORCE_SUB} force-join channels, and you already have that many. "
                  "More than that makes joining feel like a chore for people, and every extra channel is one more "
                  "check on every film request, which the free Render plan handles badly. "
                  "Remove one first: <code>/forcesub remove 1</code>")
        return
    link = args[1] if len(args) > 1 else (f"https://t.me/{chat.username}" if chat.username else chat.invite_link)
    if not link:
        try:
            link = await client.export_chat_invite_link(chat.id)
        except RPCError:
            await say("I couldn't make an invite link. Send it yourself: "
                      "<code>/forcesub add CHANNEL_ID https://t.me/+…</code>")
            return
    entry = {"chat": chat.id, "link": link, "title": title}
    await ctx.set_setting("force_subs", [entry if c["chat"] == chat.id else c for c in subs] if already else [*subs, entry])
    note = ""
    if not chat.username:
        note = ("\n\nPrivate channel: if its invite link asks you to approve new people, I approve them "
                "automatically while Auto-approve is on (/settings). That needs the right to invite users.")
    await say(f"Added <b>{escape(title)}</b>.{note}")


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
    add(MessageHandler(_bind(ctx, cmd_info), filters.command(["help", "about", "discover"])))
    add(MessageHandler(_bind(ctx, on_admin_cmd), filters.command(ADMIN_CMDS) & filters.private & admins))
    add(MessageHandler(_bind(ctx, on_text), filters.text & ~filters.regex(r"^/") & ~filters.bot
                       & (filters.private | filters.group)))
    add(MessageHandler(_bind(ctx, on_channel_file),
                       filters.channel & (filters.document | filters.video | filters.audio)))
    add(CallbackQueryHandler(_bind(ctx, on_cb)))
    add(ChatJoinRequestHandler(_bind(ctx, on_join_request)))
