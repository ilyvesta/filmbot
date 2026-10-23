"""Telegram wiring (Kurigram / Pyrogram API). Logic that doesn't need Telegram
lives in index.py, ui.py, parse.py and store.py."""
from __future__ import annotations

import asyncio
import html as htmllib
import io
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from html import escape

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import ChannelInvalid, ChannelPrivate, FloodWait, RPCError, UserNotParticipant
from pyrogram.handlers import CallbackQueryHandler, ChatJoinRequestHandler, MessageHandler, PreCheckoutQueryHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

import ui
from ctx import Ctx, Session, lagos_day, lagos_now
from index import film_id
from normalize import core_key, normalize
from parse import SUB_EXTS, VIDEO_EXTS, detail_lines, is_junk, label_of, looks_like_series, parse_best, parse_filename, quality_key_of
from payload import decode_search
from store import DEDUPE_BATCH, SKIP_CAP, FileRec
from ui import Btn

log = logging.getLogger("moviebot")
HTML = ParseMode.HTML
GROUPS = (ChatType.GROUP, ChatType.SUPERGROUP)
ADMIN_CMDS = ["setseries", "setseriesgroup", "groupsettings", "donorsettings", "delreq", "setsummary", "purgeseries", "stats", "forcesub", "settings", "index", "indexstop", "requests", "misses", "alias", "ban",
              "unban", "dedupe", "skipped", "picks", "customize", "setwelcome", "setabout", "setimage",
              "clearimage", "setlinks", "clearlinks", "setfooter", "setpicks", "preview",
              "donor", "setdonate", "images", "survey"]
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


def _plain(text: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", "", text))


def _markup_error(e: Exception) -> bool:
    return isinstance(e, ValueError) or any(w in str(e).upper() for w in ("ENTIT", "PARSE", "MARKUP"))


def reply_to(m, photo=None):
    async def show(text, rows=None):
        if photo:
            try:
                return await m.reply_photo(photo, caption=text, parse_mode=HTML, reply_markup=kb(rows))
            except Exception as e:
                log.warning("reply_photo failed: %s, fallback to text", e)
        try:
            return await m.reply_text(text, parse_mode=HTML, reply_markup=kb(rows))
        except (RPCError, ValueError) as e:
            if not _markup_error(e):
                raise
            return await m.reply_text(_plain(text), parse_mode=ParseMode.DISABLED, reply_markup=kb(rows))
    return show


def edit_of(msg, new_photo=None):
    """Edit a message in place (text or photo caption, or change media); bad markup falls back to plain text."""
    async def show(text, rows=None):
        for mode, body in ((HTML, text), (ParseMode.DISABLED, _plain(text))):
            try:
                if getattr(msg, "photo", None):
                    cur_photo = getattr(msg.photo, "file_id", None)
                    if new_photo and cur_photo and new_photo != cur_photo and hasattr(msg, "edit_media"):
                        try:
                            from pyrogram.types import InputMediaPhoto
                            return await msg.edit_media(InputMediaPhoto(new_photo, caption=body, parse_mode=mode), reply_markup=kb(rows))
                        except Exception:
                            pass
                    return await msg.edit_caption(body, parse_mode=mode, reply_markup=kb(rows))
                return await msg.edit_text(body, parse_mode=mode, reply_markup=kb(rows))
            except (RPCError, ValueError) as e:        # "message not modified" etc.
                if mode == HTML and _markup_error(e):
                    continue
                log.debug("edit skipped: %s", e)
                return msg
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
    if ctx.is_admin(uid) or await ctx.is_supporter(uid):
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
    is_sup = await ctx.is_supporter(chat_id)
    minutes = 45 if is_sup else s["autodelete_min"]
    footer = "" if is_sup else s["footer"]
    cap = ui.caption(film.title if film else "File", film.year if film else None, rec, minutes, footer)
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
    ctx.stats.add("f")
    if minutes:                                   # stored in the database, so it survives restarts
        await ctx.store.schedule_delete(chat_id, sent.id, time.time() + minutes * 60)
    return True


async def _drop(ctx: Ctx, rec: FileRec) -> None:
    if await ctx.store.remove_file(rec.uid):
        ctx.index.adjust_files(rec.film, -1)
        if await ctx.store.remove_film_if_empty(rec.film):
            await asyncio.to_thread(ctx.index.remove_film, rec.film)


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
    st = ctx.store.cfg
    series_grp = st.get("series_group") if s.group else None
    return ui.results_screen(s.hits, s.page, s.qkey, s.corrected, s.year_relaxed, s.year,
                             suggest=s.kind == "suggest", header=s.header, back=back, requestable=not s.header,
                             series_group=series_grp)


async def _film_view(ctx: Ctx, s: Session):
    fid, title, year = s.film
    st = await ctx.settings()
    note, s.note = s.note, ""
    if s.corrected:
        note += f"Showing results for <b>{escape(s.qkey)}</b>.\n"
    if s.year_relaxed and s.year:
        note += f"No {s.year} version here.\n"
    watch = None if s.group else fid in await ctx.watchlist(s.owner)      # a shared group message has no single owner
    series_grp = st.get("series_group") if s.group else None
    return ui.film_screen(title, year, s.variants, s.vpage,
                          can_back=len(s.hits) > 1 or bool(s.header) or s.kind in ("surprise", "mylist"),
                          is_admin=ctx.is_admin(s.owner) and not s.group, fid=fid, note=note, mode=s.mode,
                          picked=fid in st["picks"], watch=watch, shuffle=s.shuffle,
                          series_group=series_grp)


async def _track(ctx: Ctx, sent, sess: Session) -> None:
    ctx.sessions.put(sent.chat.id, sent.id, sess)
    ctx.watch(sent.chat.id, sent.id, (await ctx.settings())["result_ttl_min"])


async def run_search(ctx: Ctx, show, owner: int, text: str, group: bool = False) -> None:
    res = await asyncio.to_thread(ctx.index.search, text, 30)     # up to 5 pages of films
    if res.kind == "empty":
        if not group:
            await show(ui.EMPTY_QUERY)
        return
    ctx.count_search(owner)
    if res.kind == "none":
        # Search retry (Item 10)
        from parse import clean_query_retry
        for cand in clean_query_retry(text):
            r2 = await asyncio.to_thread(ctx.index.search, cand, 30)
            if r2.kind != "none" and r2.hits:
                res = r2
                res.corrected = True
                break

    if res.kind == "none":
        await ctx.store.add_miss(res.query)
        ctx.stats.add("m")
        st = await ctx.settings()
        if group:
            has_year = bool(re.search(r'\b(1[89]\d\d|20\d\d)\b', text))
            if st.get("group_silent_miss", True) and not has_year:
                return  # Silent miss in groups: do not spam group chats when configured
        t, rows = ui.not_found_screen(text, st.get("series_bot"))
        sent = await show(t, rows)
        await _track(ctx, sent, Session(owner, text, "none", [], qkey=res.query, group=group))
        if group:
            ctx.watch(sent.chat.id, sent.id, 1)
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


async def handle_query(ctx: Ctx, client, show, uid: int, text: str, group: bool = False) -> None:
    """One film search, from a typed message or from a hand-over link: series check, join check, search."""
    if looks_like_series(text):                          # not a film request, so no join check either
        st = await ctx.settings()
        target = (st.get("series_group") if group else None) or st.get("series_bot")
        t, rows = ui.series_redirect_screen(text, target, is_group=group)
        sent = await show(t, rows)
        ctx.watch(sent.chat.id, sent.id, 1 if group else st["result_ttl_min"])
        return
    if not ctx.ready:
        if not group:
            await show(ui.WARMING)
        return
    if await require_join(ctx, client, uid, show, {"type": "search", "text": text, "group": group}):
        return
    await run_search(ctx, show, uid, text, group)


async def on_text(ctx: Ctx, client, m) -> None:
    if not m.from_user or m.from_user.id in ctx.banned:
        return
    uid, text = m.from_user.id, (m.text or "").strip()
    if m.reply_to_message and ctx.is_admin(uid):
        rep_msg = m.reply_to_message
        rep_text = rep_msg.text or rep_msg.caption or ""
        qkey = None
        m_key = re.search(r"Key:\s*<code>([^<]+)</code>", rep_text)
        if m_key:
            qkey = m_key.group(1).strip()
        elif rep_msg.reply_markup and getattr(rep_msg.reply_markup, "inline_keyboard", None):
            for row in rep_msg.reply_markup.inline_keyboard:
                for b in row:
                    if b.callback_data and (b.callback_data.startswith("ar:done:") or b.callback_data.startswith("rq:done:")):
                        qkey = b.callback_data.split(":", 2)[2]
                        break
        if qkey:
            req = await ctx.store.get_request(qkey)
            if req and req.get("users"):
                for u in req["users"]:
                    await _send(client, u, f"💬 <b>Update on your request for '{escape(req['text'])}':</b>\n\n{escape(text)}")
                await m.reply_text(f"✅ Delivered your reply to {len(req['users'])} requester(s).")
                return
    group = m.chat.type in GROUPS
    if group:
        s = await ctx.settings()
        if not s.get("groups", True):
            return
        if getattr(m, "forward_date", None) or getattr(m, "forward_from", None) or getattr(m, "forward_sender_name", None):
            return
        if getattr(m, "photo", None) or getattr(m, "video", None) or getattr(m, "document", None) or getattr(m, "audio", None) or getattr(m, "animation", None) or getattr(m, "sticker", None) or getattr(m, "voice", None) or getattr(m, "video_note", None):
            return
        if re.search(r"https?://|t\.me/|@\w+", text):
            return
        max_len = s.get("group_max_len", 60)
        if not 3 <= len(text) <= max_len or len(text.split()) > 8:
            return
        if s.get("group_mention_only", False):
            me = getattr(client, "me", None)
            bot_user = (me.username if me else ctx.bot_username) or ""
            if bot_user and f"@{bot_user.lower()}" not in text.lower():
                return
    await ctx.track_user(uid)
    if ctx.throttled(uid):
        return
    await handle_query(ctx, client, reply_to(m), uid, text, group)


# ---------------------------------------------------------------- start / help / discover
async def send_home(ctx: Ctx, m) -> None:
    s = await ctx.settings()
    text, rows = ui.home_screen(s, getattr(m.from_user, "first_name", "") or "", len(ctx.index) if ctx.ready else 0)
    p_imgs = s.get("page_images") or {}
    img = p_imgs.get("welcome") or s.get("image")
    if img:
        try:
            await m.reply_photo(img, caption=text, parse_mode=HTML, reply_markup=kb(rows))
            return
        except RPCError as e:
            log.warning("welcome image failed, sending text: %s", e)
    await m.reply_text(text, parse_mode=HTML, reply_markup=kb(rows))


async def cmd_start(ctx: Ctx, client, m) -> None:
    uid = m.from_user.id
    if uid in ctx.banned:
        await m.reply_text(ui.BANNED)
        return
    await ctx.track_user(uid)
    arg = m.command[1] if len(m.command) > 1 else ""
    shared = decode_search(arg)
    if shared:                                           # sent here by the other bot with a search
        await handle_query(ctx, client, reply_to(m), uid, shared)
        return
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
    """/help /about /discover /donate /survey for everyone."""
    if not m.from_user or m.from_user.id in ctx.banned:
        return
    cmd, s = m.command[0].lower(), await ctx.settings()
    p_imgs = s.get("page_images") or {}
    img = p_imgs.get(cmd) or p_imgs.get("welcome") or s.get("image")
    show = reply_to(m, photo=img)
    if cmd == "help":
        t, rows = ui.help_screen(s)
        await show(t, rows)
    elif cmd == "about":
        t, rows = ui.about_screen(s, len(ctx.index))
        await show(t, rows)
    elif cmd == "donate":
        methods = s.get("donate_methods", [])
        wall = await ctx.store.get_supporters_wall()
        t, rows = ui.donate_screen(s, methods, wall)
        await show(t, rows)
    elif cmd == "survey":
        t, rows = ui.survey_screen(0)
        await show(t, rows)
    else:
        t, rows = ui.discover_screen(s)
        sent = await show(t, rows)
        await _track(ctx, sent, Session(m.from_user.id, "", "discover", [], group=m.chat.type in GROUPS))


async def _discover_hits(ctx: Ctx, which: str, sess: Session) -> tuple[str, list[tuple[str, str, int]]]:
    s = await ctx.settings()
    if which == "pick":
        name = s["picks_name"]
        fids = [f for f in s["picks"] if ctx.index.get(f)]
        if not sess.pick_order:
            random.shuffle(fids)                         # a fresh random order for each viewer...
        else:                                            # ...kept the same while they page through it
            fids = [f for f in sess.pick_order if f in fids] + [f for f in fids if f not in sess.pick_order]
        sess.pick_order = fids
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


async def _shuffle(ctx: Ctx, sess: Session, uid: int, source: str) -> bool:
    """Surprise me ('picks') or Shuffle ('wl'): a random film that hasn't come up yet in this session."""
    if source == "picks":
        ids = [f for f in (await ctx.settings())["picks"] if ctx.index.get(f)]
    else:
        ids = [f for f in await ctx.watchlist(uid) if ctx.index.get(f)]
    if not ids:
        return False
    pool = [f for f in ids if f not in sess.seen]
    if not pool:                                          # seen them all: start over
        sess.seen.clear()
        pool = ids
        sess.note = "That was all of them. Starting again.\n" if len(ids) > 1 else ""
    fid = random.choice(pool)
    sess.seen.add(fid)
    film = ctx.index.get(fid)
    sess.film, sess.vpage, sess.mode, sess.shuffle = (film.id, film.title, film.year), 0, "", source
    sess.variants = await load_variants(ctx, film.id)
    sess.kind = "surprise" if source == "picks" else "mylist"
    return True


async def _mylist_view(ctx: Ctx, uid: int, sess: Session, page: int = 0):
    wl = await ctx.watchlist(uid)
    films = [f for f in (ctx.index.get(x) for x in wl) if f]
    if len(films) != len(wl):                             # a film left the library: quietly drop it
        await ctx.save_watchlist(uid, [f.id for f in films])
    reqs = await ctx.store.user_requests(uid)
    user = await ctx.store.get_user(uid)
    sess.kind, sess.reqs, sess.page, sess.film, sess.shuffle = "mylist", [r["key"] for r in reqs], page, None, None
    sess.hits = [(f.id, f.label, f.file_count) for f in films]        # so Back from a film returns here
    return ui.mylist_screen([(f.id, f.label) for f in films], page, ctx.cap_for(user), [r["text"] for r in reqs],
                            user.get("sc", 0) + ctx.search_pending.get(uid, 0))


# ---------------------------------------------------------------- callbacks
async def on_cb(ctx: Ctx, client, cb) -> None:
    uid, data = cb.from_user.id, cb.data or ""
    if uid in ctx.banned:
        await cb.answer(ui.BANNED, show_alert=True)
        return
    await ctx.track_user(uid)
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
        p_imgs = s.get("page_images") or {}
        img = p_imgs.get(arg) or p_imgs.get("welcome") or s.get("image")
        if arg == "help":
            t, rows = ui.help_screen(s)
        elif arg == "about":
            t, rows = ui.about_screen(s, len(ctx.index))
        elif arg == "donate":
            methods = s.get("donate_methods", [])
            wall = await ctx.store.get_supporters_wall()
            t, rows = ui.donate_screen(s, methods, wall)
        elif arg == "discover":
            t, rows = ui.discover_screen(s)
        elif arg == "survey":
            t, rows = ui.survey_screen(0)
        else:
            t, rows = ui.home_screen(s, cb.from_user.first_name or "", len(ctx.index))
            img = p_imgs.get("welcome") or s.get("image")
        await edit_of(msg, new_photo=img)(t, rows)
        await cb.answer()
        return
    if head == "rep":
        await msg.edit_reply_markup(kb(ui.report_rows(arg)))
        await cb.answer()
        return
    if head == "rr":
        await _report(ctx, client, cb, arg)
        return
    if head in ("st", "rmfile", "rdis", "ixstop", "ddy", "ddn", "sk", "skt", "psy", "psn", "gst", "clm", "ar", "rqpg"):
        if not ctx.is_admin(uid):
            await cb.answer("Admins only.", show_alert=True)
            return
        await _admin_cb(ctx, client, cb, head, arg)
        return
    if head == "dn":
        s = await ctx.settings()
        methods = s.get("donate_methods", [])
        if arg == "stars":
            t, rows = ui.stars_screen()
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        elif arg.startswith("st:"):
            stars_amt = int(arg.split(":")[1])
            try:
                await client.send_invoice(
                    chat_id=msg.chat.id,
                    title="MovieBot Supporter",
                    description=f"Support MovieBot with {stars_amt} Stars and enjoy 90 days of Supporter perks!",
                    payload=f"stars_{uid}_{stars_amt}",
                    currency="XTR",
                    prices=[LabeledPrice(f"Support ({stars_amt} Stars)", stars_amt)]
                )
                await cb.answer("Invoice sent above! Tap to pay.")
            except Exception as e:
                log.exception("send_invoice failed: %s", e)
                await cb.answer("Could not send Stars invoice. Try another method.", show_alert=True)
            return
        elif arg == "myid":
            await cb.answer(f"Your User ID is: {uid}\n(Send this to the admin to claim perks)", show_alert=True)
            return
        elif arg.startswith("claim:"):
            method_name = arg.split(":", 1)[1]
            user_name = cb.from_user.first_name or f"User {uid}"
            ok, cid = await ctx.store.add_claim(uid, user_name, method_name)
            if not ok:
                await cb.answer("You already have a pending donation claim. Please wait for admin review.", show_alert=True)
                return
            await cb.answer("Claim submitted! Admin will verify and activate your perks.", show_alert=True)
            admin_text = (
                f"💰 <b>New Donation Claim!</b>\n\n"
                f"• <b>User:</b> {escape(user_name)} (<code>{uid}</code>)\n"
                f"• <b>Method:</b> {escape(method_name)}\n"
                f"• <b>Claim ID:</b> <code>{cid}</code>\n"
                f"• <b>Date:</b> {_today()}\n\n"
                f"Select supporter duration to approve or decline:"
            )
            await notify_inbox(ctx, client, admin_text, ui.claim_admin_rows(cid))
            return
        elif arg.startswith("m:") and arg[2:].isdigit():
            idx = int(arg[2:])
            if idx < len(methods):
                m_info = methods[idx]
                t, rows = ui.donate_method_screen(m_info)
                qr = m_info.get("qr")
                if qr:
                    try:
                        await msg.reply_photo(qr, caption=t, parse_mode=HTML, reply_markup=kb(rows))
                        await cb.answer()
                        return
                    except Exception:
                        pass
                await edit_of(msg)(t, rows)
                await cb.answer()
                return
        elif arg == "wall":
            wall = await ctx.store.get_supporters_wall()
            t, rows = ui.wall_screen(wall)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        wall = await ctx.store.get_supporters_wall()
        t, rows = ui.donate_screen(s, methods, wall)
        await edit_of(msg)(t, rows)
        await cb.answer()
        return
    if head == "wall":
        flag = arg in ("opt:yes", "1", "yes")
        await ctx.store.set_wall_opt_in(uid, flag)
        await cb.answer("Saved! Your preference has been updated.", show_alert=True)
        return
    if head == "sv":
        step_str, _, choice = arg.partition(":")
        step = int(step_str) if step_str.isdigit() else 0
        if choice == "az":
            t, rows = ui.survey_az_buckets_screen()
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        elif choice.startswith("az:"):
            t, rows = ui.survey_screen(2, extra=choice)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        elif choice == "main":
            t, rows = ui.survey_screen(2)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        elif choice:
            sess = ctx.sessions.get(msg.chat.id, msg.id)
            if not sess:
                sess = Session(uid, "", "survey", [])
                ctx.sessions.put(msg.chat.id, msg.id, sess)
            if not hasattr(sess, "sv_answers"):
                sess.sv_answers = {}
            q_keys = ["gender", "age", "country", "discovery"]
            if step < len(q_keys):
                sess.sv_answers[q_keys[step]] = choice
            next_step = step + 1
            if next_step >= 4:
                await ctx.store.record_survey(uid, sess.sv_answers)
                ctx.stats.add("sv")
                t, rows = ui.survey_screen(4)
            else:
                t, rows = ui.survey_screen(next_step)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        t, rows = ui.survey_screen(0)
        await edit_of(msg)(t, rows)
        await cb.answer()
        return
    if head == "dc":
        s = await ctx.settings()
        t, rows = ui.discover_screen(s)
        sess = ctx.sessions.get(msg.chat.id, msg.id)
        if sess and (sess.owner == uid or sess.group):         # already inside a Discover message
            sess.kind, sess.hits, sess.header, sess.back, sess.film, sess.shuffle = "discover", [], None, None, None, None
            sent = await edit_of(msg)(t, rows)
            ctx.watch(msg.chat.id, msg.id, (await ctx.settings())["result_ttl_min"])
            await cb.answer()
            return
        else:                                                  # opened from the welcome message
            sent = await msg.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
        await _track(ctx, sent, Session(uid, "", "discover", [], group=msg.chat.type in GROUPS))
        await cb.answer()
        return

    sess = ctx.sessions.get(msg.chat.id, msg.id)
    if head == "ml" and arg == "" and not (sess and sess.kind == "mylist"):      # My list, opened from the home page
        mine = Session(uid, "", "mylist", [], group=msg.chat.type in GROUPS)
        t, rows = await _mylist_view(ctx, uid, mine)
        await _track(ctx, await msg.reply_text(t, parse_mode=HTML, reply_markup=kb(rows)), mine)
        await cb.answer()
        return
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
        name, hits = await _discover_hits(ctx, arg, sess)
        sess.kind, sess.hits, sess.header, sess.back, sess.page, sess.film, sess.shuffle = \
            "list", hits, name, "dc", 0, None, None
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
        sess.film, sess.mode, sess.shuffle = None, "", None
        if sess.kind == "surprise":                            # Back from Surprise me goes to Discover
            sess.kind, sess.hits, sess.header, sess.back = "discover", [], None, None
            t, rows = ui.discover_screen(await ctx.settings())
        elif sess.kind == "mylist":
            t, rows = await _mylist_view(ctx, uid, sess, sess.page)
        else:
            t, rows = _results_view(ctx, sess)
        await show(t, rows)
    elif head == "f":
        film = ctx.index.get(arg)
        if not film:
            await cb.answer("That one's gone.", show_alert=True)
            return
        sess.film, sess.vpage, sess.mode, sess.shuffle = (film.id, film.title, film.year), 0, "", None
        sess.variants = await load_variants(ctx, film.id)
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
    elif head == "sur":
        if not await _shuffle(ctx, sess, uid, "picks"):
            await cb.answer("There's nothing in the list yet.", show_alert=True)
            return
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
    elif head == "wl" and sess.film and not sess.group:
        wl = list(await ctx.watchlist(uid))
        if arg in wl:
            wl.remove(arg)
            await ctx.save_watchlist(uid, wl)
            await cb.answer("Removed from your watchlist.")
        else:
            cap = await ctx.watchlist_cap(uid)
            if len(wl) >= cap:
                await cb.answer(f"Your watchlist is full ({cap} films). Remove one in My list first.", show_alert=True)
                return
            await ctx.save_watchlist(uid, wl + [arg])
            await cb.answer("Added to your watchlist.")
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
        return
    elif head == "ml":
        kind, _, rest = arg.partition(":")
        if kind == "s":
            if not await _shuffle(ctx, sess, uid, "wl"):
                await cb.answer("Add a film to your watchlist first.", show_alert=True)
                return
            t, rows = await _film_view(ctx, sess)
            await show(t, rows)
            return
        page = sess.page
        if kind == "p" and rest.isdigit():
            page = int(rest)
        elif kind == "r":
            await ctx.save_watchlist(uid, [f for f in await ctx.watchlist(uid) if f != rest])
        elif kind == "c" and rest.isdigit() and int(rest) < len(sess.reqs):
            await ctx.store.cancel_request(sess.reqs[int(rest)], uid)
        t, rows = await _mylist_view(ctx, uid, sess, page)
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
            if await ctx.store.remove_film_if_empty(rec.film):
                await asyncio.to_thread(ctx.index.remove_film, rec.film)
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
    count, new, added = await ctx.store.add_request(qkey, sess.query[:80], cb.from_user.id)
    if added:
        ctx.stats.add("r")
    is_sup = await ctx.is_supporter(cb.from_user.id)
    sup_tag = "⭐ [SUPPORTER] " if is_sup else ""
    extra = " Open me in private and press Start, or I can't message you." if group else ""
    await cb.answer("Requested. I'll message you when it's added." + extra, show_alert=True)
    if new or count in REQUEST_PINGS or is_sup:
        rq_rows = [
            [Btn("✅ Done", f"ar:done:{qkey}"), Btn("💬 Reply", f"ar:rep:{qkey}"), Btn("🗑️ Delete", f"ar:rm:{qkey}")]
        ]
        await notify_inbox(ctx, client, f"{sup_tag}Request: <b>{escape(sess.query)}</b>  ({count} "
                                        f"{'person' if count == 1 else 'people'})\nKey: <code>{qkey}</code>", rq_rows)


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
    if fresh:
        ctx.stats.add("p")
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
        elif arg == "stars":
            await ctx.set_setting("stars_enabled", not s.get("stars_enabled", True))
        elif arg == "sv":
            await ctx.set_setting("survey_active", not s.get("survey_active", True))
        elif arg == "to_groups":
            t, rows = ui.group_settings_screen(s)
            await show(t, rows)
            return
        elif arg == "refresh":
            t, rows = ui.settings_screen(s)
            await show(t, rows)
            return
        t, rows = ui.settings_screen(await ctx.settings())
        await show(t, rows)
    elif head == "gst":
        s = await ctx.settings()
        if arg == "grp":
            await ctx.set_setting("groups", not s.get("groups", True))
        elif arg == "men":
            await ctx.set_setting("group_mention_only", not s.get("group_mention_only", False))
        elif arg == "sil":
            await ctx.set_setting("group_silent_miss", not s.get("group_silent_miss", True))
        elif arg == "len":
            lens = [40, 60, 80, 100]
            cur = s.get("group_max_len", 60)
            nxt = lens[(lens.index(cur) + 1) % len(lens)] if cur in lens else 60
            await ctx.set_setting("group_max_len", nxt)
        t, rows = ui.group_settings_screen(await ctx.settings())
        await show(t, rows)
    elif head == "clm":
        action, _, cid = arg.partition(":")
        claim = await ctx.store.get_claim(cid)
        if not claim or claim.get("status") != "pending":
            await cb.answer(f"Claim already {claim.get('status', 'resolved') if claim else 'not found'}.", show_alert=True)
            return
        if action == "dec":
            await ctx.store.resolve_claim(cid, "declined")
            await show(f"❌ <b>Claim Declined</b> (by {escape(cb.from_user.first_name)})\nUser: <code>{claim['uid']}</code>")
            await cb.answer("Claim declined.")
            await _send(client, claim["uid"], "⚠️ Your donation claim could not be verified by an admin. If you believe this is an error, please reach out.")
        elif action in ("30", "60", "90"):
            days = int(action)
            new_date = await ctx.store.donor_add(claim["uid"], days=days, name=claim["name"])
            await ctx.store.resolve_claim(cid, f"approved_{days}d")
            await show(f"✅ <b>Claim Approved ({days} days)</b> by {escape(cb.from_user.first_name)}\nUser: <code>{claim['uid']}</code>\nExpires: {new_date}")
            await cb.answer(f"Approved {days} days!")
            await _send(client, claim["uid"], f"🎉 <b>Donation Verified!</b>\n\nYour Supporter perks have been activated for <b>{days} days</b> (valid until <b>{new_date}</b>).\n\nEnjoy unlimited watchlists, prioritized requests, and your exclusive Supporter badge!")
        return
    elif head == "ar":
        action, _, qkey = arg.partition(":")
        req = await ctx.store.get_request(qkey)
        if not req or not req.get("open"):
            await cb.answer("Request not found or already closed.", show_alert=True)
            return
        if action == "done":
            await ctx.store.close_request(qkey)
            film_name = req["text"]
            for u in req.get("users", []):
                await _send(client, u, f"🎬 <b>Good news!</b> Your requested title <b>{escape(film_name)}</b> has been added to the library! You can search and stream/download it now.")
            admin_name = cb.from_user.first_name or "Admin"
            orig = cb.message.text or cb.message.caption or ""
            await show(f"{orig}\n\n✅ <b>Completed by {escape(admin_name)}</b>", rows=[[Btn("✅ Completed", "noop")]])
            await cb.answer(f"Marked done! Notified {len(req.get('users', []))} user(s).")
        elif action == "rm":
            await ctx.store.delete_request(qkey)
            await show(f"🗑️ <b>Request deleted by {escape(cb.from_user.first_name)}</b>")
            await cb.answer("Request deleted.")
        elif action == "rep":
            await cb.answer("To reply to requesters: simply Reply to this message in Telegram with your text!", show_alert=True)
        return
    elif head == "rqpg":
        page = int(arg) if arg.isdigit() else 0
        all_reqs = await ctx.store.open_requests(500)
        total = len(all_reqs)
        per_page = 10
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, pages - 1))
        slice_reqs = all_reqs[page * per_page:(page + 1) * per_page]
        lines = [f"<b>Open Requests ({total} total, page {page + 1}/{pages}):</b>\n"]
        for r in slice_reqs:
            lines.append(f"• <b>{escape(r['text'])}</b> ({len(r['users'])} users) — <code>/delreq {r['key']}</code>")
        rows = []
        nav = []
        if page > 0:
            nav.append(Btn("‹ Prev", f"rqpg:{page - 1}"))
        if page < pages - 1:
            nav.append(Btn("Next ›", f"rqpg:{page + 1}"))
        if nav:
            rows.append(nav)
        await show("\n".join(lines), rows=rows)
        await cb.answer()
        return
    elif head == "rmfile":
        rec = await ctx.store.remove_file(arg)
        if rec:
            ctx.index.adjust_files(rec.film, -1)
            if await ctx.store.remove_film_if_empty(rec.film):
                await asyncio.to_thread(ctx.index.remove_film, rec.film)
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
    elif head == "psy":
        items, ctx.pending_purge = ctx.pending_purge or [], None
        if not items:
            await show("That list expired. Run /purgeseries again.")
        elif ctx.maintenance:
            await show("Another big job is running. Try again when it's done.")
        else:
            await show(f"Deleting {len(items):,} files in small batches…")
            asyncio.create_task(_purge_delete(ctx, cb.message, items))
    elif head == "psn":
        ctx.pending_purge = None
        await show("Okay, nothing deleted.")
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


PURGE_ROUND = 50000
PURGE_PAUSE = 0.3          # seconds between delete batches, so Render and Mongo get a breather


def _series_only(rows: list[dict]) -> list[dict]:
    return [r for r in rows if parse_filename(r["name"]).kind == "series"]


async def _purge_scan(ctx: Ctx, status) -> None:
    """Dry run: read every saved file name, count the ones that are really series episodes. Deletes nothing."""
    ctx.maintenance = "purge"
    found, scanned, chunk, tick = [], 0, [], time.monotonic()
    try:
        async for r in ctx.store.iter_file_names():
            chunk.append(r)
            if len(chunk) >= 500:
                found += await asyncio.to_thread(_series_only, chunk)       # off the main loop; keeps the bot responsive
                scanned, chunk = scanned + len(chunk), []
                if len(found) >= PURGE_ROUND:
                    break
                if time.monotonic() - tick >= 5:
                    tick = time.monotonic()
                    await _edit(status, f"Scanning… {scanned:,} files checked, {len(found):,} look like series so far.", None)
        if chunk and len(found) < PURGE_ROUND:
            found += await asyncio.to_thread(_series_only, chunk)
            scanned += len(chunk)
        found = found[:PURGE_ROUND]
        if not found:
            await _edit(status, f"Checked {scanned:,} files. None look like series episodes.", None)
            return
        ctx.pending_purge = found
        sample = "\n".join(f"<code>{escape(r['name'][:70])}</code>" for r in found[:10])
        more = f"\nThat's one round of {PURGE_ROUND:,}. Run it again afterwards for the rest." if len(found) >= PURGE_ROUND else ""
        await _edit(status, f"Checked {scanned:,} files. <b>{len(found):,}</b> look like series episodes "
                            f"(S01E01, 1x01, Season 2 or S01 anywhere in the name).{more}\n\nFirst 10:\n{sample}\n\n"
                            "Delete them from the database? Films left with no files are removed too. "
                            "Your channel posts aren't touched.",
                    [[Btn("Yes, delete them", "psy"), Btn("Cancel", "psn")]])
    except Exception as e:
        log.exception("purge scan failed")
        await _edit(status, f"The scan stopped with an error: {escape(str(e))}", None)
    finally:
        ctx.maintenance = None


async def _purge_delete(ctx: Ctx, status, items: list[dict]) -> None:
    """Delete in small batches with short pauses, so Render's memory and Mongo don't spike."""
    ctx.maintenance = "purge"
    removed, touched, tick = 0, {}, time.monotonic()
    try:
        for i in range(0, len(items), 500):
            batch = items[i:i + 500]
            removed += await ctx.store.delete_files(batch, kind="series")
            for it in batch:
                touched[it["film"]] = touched.get(it["film"], 0) + 1
            if time.monotonic() - tick >= 5:
                tick = time.monotonic()
                await _edit(status, f"Deleting… {removed:,} of {len(items):,} files removed.", None)
            await asyncio.sleep(PURGE_PAUSE)
        films_gone = 0
        for n, (fid, cnt) in enumerate(touched.items(), 1):
            if await ctx.store.remove_film_if_empty(fid):
                await asyncio.to_thread(ctx.index.remove_film, fid)
                films_gone += 1
            else:
                await asyncio.to_thread(ctx.index.adjust_files, fid, -cnt)
            if n % 200 == 0:
                await asyncio.sleep(0.1)
        await _edit(status, f"Done. Removed <b>{removed:,}</b> series files and <b>{films_gone:,}</b> films that were left "
                            f"empty. {len(touched) - films_gone:,} films lost some files but still have others. "
                            "The names are listed under /skipped, Series.", None)
    except Exception as e:
        log.exception("purge failed")
        await _edit(status, f"Deleting stopped with an error after {removed:,} files: {escape(str(e))}\n"
                            "Run /purgeseries again to continue.", None)
    finally:
        ctx.maintenance = None


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
        mem, subs = rss_mb(), await ctx.subs()
        day = lagos_day()
        saved, waiting = await ctx.store.stats_get(day), ctx.stats.peek(day)
        t = {k: saved.get(k, 0) + waiting.get(k, 0) for k in set(saved) | set(waiting)}
        lines = [f"Files: {c['files']:,}", f"Films: {c['films']:,}  (search index: {len(ctx.index):,})",
                 f"Users: {c['users']:,}", f"Supporters: {c.get('supporters', 0):,}", f"Open requests: {c['requests']}"]
        if c["db_mb"] is not None:
            lines.append(f"Database: {c['db_mb']} MB of 512")
        if mem:
            lines.append(f"Bot memory: {mem} MB of 512" + ("  (getting tight)" if mem > 420 else ""))
        lines.append("Force-join: " + (f"{len(subs)} channel{'s' if len(subs) != 1 else ''} ("
                                       + ", ".join(escape(x['title']) for x in subs) + ")" if subs else "off"))
        lines += ["", f"<b>Today</b>  ({day}, Lagos time)", f"Users active: {t.get('u', 0):,}",
                  f"New users: {t.get('n', 0):,}", f"Searches: {t.get('s', 0):,}", f"Files sent: {t.get('f', 0):,}",
                  f"Requests: {t.get('r', 0):,}"]
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
    elif cmd == "setseries":
        name = (args[0] if args else "").lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", name):
            cur = (await ctx.settings())["series_bot"]
            await say(f"The series bot is @{escape(cur)}. To change it: <code>/setseries @username</code>")
            return
        await ctx.set_setting("series_bot", name)
        await say(f"Series bot is now @{escape(name)}. Series searches and 'not found' messages point there.")
    elif cmd == "setseriesgroup":
        url = args[0].strip() if args else ""
        if not (url.startswith("https://t.me/") or url.startswith("http://t.me/") or url.lower() == "off"):
            cur = (await ctx.settings()).get("series_group")
            await say(f"Current series group: {escape(cur or 'Not set')}.\n"
                      f"To change: <code>/setseriesgroup https://t.me/...</code> or <code>/setseriesgroup off</code>")
            return
        val = None if url.lower() == "off" else url
        await ctx.set_setting("series_group", val)
        await say("Series group link removed." if not val else f"Series group set to {escape(val)}.")
    elif cmd == "groupsettings":
        t, rows = ui.group_settings_screen(await ctx.settings())
        await say(t, rows)
    elif cmd == "setsummary":
        arg = (args[0] if args else "").lower()
        if arg == "off" or re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", arg):
            await ctx.set_setting("summary_time", None if arg == "off" else arg)
            await say("Daily summary is off." if arg == "off" else f"Daily summary will arrive around {arg} Lagos time.")
        else:
            cur = (await ctx.settings())["summary_time"]
            await say(f"Daily summary: {cur + ' Lagos time' if cur else 'off'}. Change it with "
                      "<code>/setsummary 08:00</code> or <code>/setsummary off</code>.")
    elif cmd == "purgeseries":
        await say("The /purgeseries command has been disabled to prevent accidental removal of legitimate movies.")
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
        await ctx.set_setting(key, ui.safe_html(html))
        await say("Saved. /preview to see it.")
    elif cmd == "images":
        s = await ctx.settings()
        p_imgs = s.get("page_images") or {}
        img = s.get("image")
        pages = ["welcome", "donate", "discover", "help", "about", "survey"]
        lines = ["<b>Configured Images:</b>"]
        for p in pages:
            val = p_imgs.get(p) or (img if p == "welcome" else None)
            lines.append(f"• {p.title()}: {'Set' if val else 'None'}")
        lines.append("\nSet page image: reply to a photo with <code>/setimage [page]</code> (welcome, donate, discover, help, about, survey).")
        await say("\n".join(lines))
    elif cmd == "setimage":
        photo = m.photo or (m.reply_to_message.photo if m.reply_to_message else None)
        if not photo:
            await say("Send a photo with <code>/setimage [page]</code> as its caption, or reply to a photo with it.\nPages: welcome, donate, discover, help, about, survey")
            return
        page = args[0].lower() if args else "welcome"
        s = await ctx.settings()
        p_imgs = dict(s.get("page_images") or {})
        p_imgs[page] = photo.file_id
        if page == "welcome":
            await ctx.set_setting("image", photo.file_id)
        await ctx.set_setting("page_images", p_imgs)
        await say(f"Image for '<b>{escape(page)}</b>' saved. /preview to see it.")
    elif cmd == "clearimage":
        page = args[0].lower() if args else "welcome"
        s = await ctx.settings()
        p_imgs = dict(s.get("page_images") or {})
        p_imgs.pop(page, None)
        if page == "welcome":
            await ctx.set_setting("image", None)
        await ctx.set_setting("page_images", p_imgs)
        await say(f"Image for '<b>{escape(page)}</b>' removed.")
    elif cmd == "donorsettings":
        s = await ctx.settings()
        stars_on = s.get("stars_enabled", True)
        donors = await ctx.store.donor_list()
        lines = [
            "<b>Donor Settings & Tiers</b>\n",
            "• <b>Basic Supporter:</b> 30 days perks",
            "• <b>Standard Supporter:</b> 60 days perks",
            "• <b>Full / Star Supporter:</b> 90 days perks",
            f"• <b>Telegram Stars:</b> {'Enabled' if stars_on else 'Disabled'} (toggle via <code>/settings</code>)",
            f"• <b>Active Supporters:</b> {len(donors)} users",
            "\nManual perk command:\n<code>/donor add UID DAYS NOTE</code>"
        ]
        await say("\n".join(lines))
    elif cmd == "donor":
        sub = args[0].lower() if args else ""
        if sub == "add" and len(args) >= 2 and args[1].isdigit():
            duid = int(args[1])
            days = int(args[2]) if len(args) >= 3 and args[2].isdigit() else 30
            note = " ".join(args[3:]) if len(args) >= 4 else "manual"
            await ctx.store.donor_add(duid, days=days, name=note)
            await say(f"Granted {days} days supporter perks to <code>{duid}</code> ({escape(note)}).")
        elif sub == "remove" and len(args) >= 2 and args[1].isdigit():
            duid = int(args[1])
            await ctx.store.donor_remove(duid)
            await say(f"Removed supporter perks from <code>{duid}</code>.")
        elif sub == "list":
            donors = await ctx.store.donor_list()
            if not donors:
                await say("No active supporters.")
            else:
                lines = [f"<b>Active Supporters ({len(donors)}):</b>"]
                for d in donors[:30]:
                    lines.append(f"• <code>{d.get('uid')}</code> until {d.get('su')} ({escape(d.get('name', ''))})")
                await say("\n".join(lines))
        else:
            await say("Usage:\n<code>/donor add UID [DAYS] [NOTE]</code>\n<code>/donor remove UID</code>\n<code>/donor list</code>")
    elif cmd == "setdonate":
        sub = args[0].lower() if args else ""
        s = await ctx.settings()
        methods = list(s.get("donate_methods", []))
        if sub == "reset":
            await ctx.set_setting("donate_methods", [])
            await say("Donation methods cleared.")
        elif sub == "list":
            if not methods:
                await say("No donation methods configured.")
            else:
                lines = ["<b>Configured Donation Methods:</b>"]
                for i, mth in enumerate(methods):
                    lines.append(f"{i+1}. <b>{escape(mth.get('title',''))}</b> ({mth.get('type','')}): {escape(mth.get('details',''))}")
                await say("\n".join(lines))
        elif sub == "add":
            body = " ".join(args[1:])
            parts = [p.strip() for p in body.split("|")]
            if len(parts) >= 3:
                m_type, m_title, m_details = parts[0], parts[1], parts[2]
                qr = parts[3] if len(parts) > 3 else None
                methods.append({"type": m_type, "title": m_title, "details": m_details, "qr": qr})
                await ctx.set_setting("donate_methods", methods)
                await say(f"Added donation method: <b>{escape(m_title)}</b>")
            else:
                await say("Usage: <code>/setdonate add TYPE | TITLE | DETAILS [| QR_FILE_ID]</code>\nTypes: bank, crypto, link")
        else:
            await say("Usage:\n<code>/setdonate add TYPE | TITLE | DETAILS [| QR]</code>\n<code>/setdonate list</code>\n<code>/setdonate reset</code>")
    elif cmd == "survey":
        sub = args[0].lower() if args else "stats"
        if sub == "on":
            await ctx.set_setting("survey_active", True)
            await say("Survey activated.")
        elif sub == "off":
            await ctx.set_setting("survey_active", False)
            await say("Survey deactivated.")
        elif sub == "stats":
            stats = await ctx.store.get_survey_stats()
            total = stats.get("_meta", {}).get("count", 0)
            if not total:
                await say("No survey responses yet.")
            else:
                lines = [f"<b>Survey Responses ({total:,} total users):</b>\n"]
                if "gender" in stats:
                    lines.append("<b>Gender:</b>")
                    for k, v in stats.get("gender", {}).items():
                        lines.append(f"  • {escape(k)}: {v:,} users")
                if "age" in stats:
                    lines.append("\n<b>Age:</b>")
                    for k, v in stats.get("age", {}).items():
                        lines.append(f"  • {escape(k)}: {v:,} users")
                if "country" in stats:
                    lines.append("\n<b>Top Countries:</b>")
                    countries = sorted(stats.get("country", {}).items(), key=lambda x: x[1], reverse=True)[:5]
                    for k, v in countries:
                        lines.append(f"  • {escape(k)}: {v:,} users")
                if "discovery" in stats:
                    lines.append("\n<b>Discovery Source:</b>")
                    for k, v in stats.get("discovery", {}).items():
                        lines.append(f"  • {escape(k)}: {v:,} users")
                cohorts = stats.get("_cohorts", {})
                if cohorts:
                    lines.append("\n<b>Top Demographic Cohorts:</b>")
                    top_c = sorted(cohorts.items(), key=lambda x: x[1], reverse=True)[:8]
                    for cohort, cnt in top_c:
                        clean_cohort = cohort.replace("_", ".")
                        lines.append(f"  • {escape(clean_cohort)}: {cnt:,} users")
                await say("\n".join(lines))
        else:
            await say("Usage: <code>/survey [stats|on|off]</code>")
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
        await ctx.set_setting("footer", ui.safe_html(html))
        await say("Footer saved. It shows under every file.")
    elif cmd == "setpicks":
        name = _inline_arg(m)[:30]
        if not name:
            await say("Usage: <code>/setpicks Name</code>")
            return
        await ctx.set_setting("picks_name", name)
        await say(f"The list is now called <b>{escape(name)}</b>.")
    elif cmd == "requests":
        page = int(args[0]) - 1 if args and args[0].isdigit() else 0
        all_reqs = await ctx.store.open_requests(500)
        total = len(all_reqs)
        per_page = 10
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, pages - 1))
        slice_reqs = all_reqs[page * per_page:(page + 1) * per_page]
        if not slice_reqs:
            await say("No open requests.")
            return
        lines = [f"<b>Open Requests ({total} total, page {page + 1}/{pages}):</b>\n"]
        for r in slice_reqs:
            lines.append(f"• <b>{escape(r['text'])}</b> ({len(r['users'])} users) — <code>/delreq {r['key']}</code>")
        rows = []
        nav = []
        if page > 0:
            nav.append(Btn("‹ Prev", f"rqpg:{page - 1}"))
        if page < pages - 1:
            nav.append(Btn("Next ›", f"rqpg:{page + 1}"))
        if nav:
            rows.append(nav)
        await say("\n".join(lines), rows=rows)
    elif cmd == "delreq":
        if not args:
            await say("Usage: <code>/delreq &lt;key&gt;</code>")
            return
        key = args[0]
        ok = await ctx.store.delete_request(key)
        await say(f"Request <code>{escape(key)}</code> {'deleted.' if ok else 'not found.'}")
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


# ---------------------------------------------------------------- counters and daily summary
async def maybe_send_summary(ctx: Ctx, client) -> None:
    s = await ctx.settings()
    at = s["summary_time"]
    if not at:
        return
    now = lagos_now()
    today = now.date().isoformat()
    if s["summary_last"] is None:                        # first time ever: start tomorrow, don't send an empty report
        await ctx.set_setting("summary_last", today)
        return
    if s["summary_last"] == today or now.strftime("%H:%M") < at:
        return
    await ctx.flush_stats()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    text = ui.daily_summary(yesterday, await ctx.store.stats_get(yesterday), await ctx.store.open_requests(5))
    await notify_inbox(ctx, client, text)
    await ctx.set_setting("summary_last", today)         # remembered in the database, so a restart never repeats or skips it


async def stats_job(ctx: Ctx, client, tick: int = 60) -> None:
    """Counters live in memory and are written once a minute, as one small update per day."""
    while True:
        await asyncio.sleep(tick)
        try:
            await ctx.flush_stats()
            await maybe_send_summary(ctx, client)
        except Exception:
            log.exception("stats job failed")


# ---------------------------------------------------------------- registration
async def on_pre_checkout(ctx: Ctx, client, query) -> None:
    try:
        await query.answer(ok=True)
    except Exception as e:
        log.warning("failed to answer pre-checkout: %s", e)


async def on_successful_payment(ctx: Ctx, client, m) -> None:
    uid = m.from_user.id
    payment = getattr(m, "successful_payment", None)
    stars = getattr(payment, "total_amount", 50) if payment else 50
    days = 90
    name = getattr(m.from_user, "first_name", "") or str(uid)
    await ctx.store.donor_add(uid, days=days, name=name)

    text = (f"⭐ <b>Thank you for your support!</b>\n\n"
            f"You have been granted <b>{days} days</b> of Supporter perks:\n"
            f"• Force-join channel check bypass\n"
            f"• 45-minute file auto-delete\n"
            f"• Priority requests\n\n"
            f"Would you like your name (<b>{escape(name)}</b>) featured on our Supporters Wall?")
    rows = [
        [Btn("Yes, feature my name", "wall:opt:yes"), Btn("No, keep private", "wall:opt:no")]
    ]
    await m.reply_text(text, parse_mode=HTML, reply_markup=kb(rows))


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
    add(MessageHandler(_bind(ctx, cmd_info), filters.command(["help", "about", "discover", "donate", "survey"])))
    add(MessageHandler(_bind(ctx, on_admin_cmd), filters.command(ADMIN_CMDS) & filters.private & admins))
    add(MessageHandler(_bind(ctx, on_successful_payment), filters.successful_payment & filters.private))
    add(PreCheckoutQueryHandler(_bind(ctx, on_pre_checkout)))
    add(MessageHandler(_bind(ctx, on_text), filters.text & ~filters.regex(r"^/") & ~filters.bot
                       & (filters.private | filters.group)))
    add(MessageHandler(_bind(ctx, on_channel_file),
                       filters.channel & (filters.document | filters.video | filters.audio)))
    add(CallbackQueryHandler(_bind(ctx, on_cb)))
    add(ChatJoinRequestHandler(_bind(ctx, on_join_request)))
