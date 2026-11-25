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
from datetime import date, datetime, timedelta
from html import escape

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import ChannelInvalid, ChannelPrivate, FloodWait, RPCError, UserNotParticipant
from pyrogram.handlers import (CallbackQueryHandler, ChatJoinRequestHandler, ChatMemberUpdatedHandler,
                               MessageHandler, PreCheckoutQueryHandler)
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

import ui
from ctx import Ctx, LAGOS, Session, lagos_day, lagos_now
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
              "unban", "dedupe", "skipped", "reparse", "picks", "curated", "customize", "setwelcome", "setabout", "setimage",
              "clearimage", "setlinks", "clearlinks", "setfooter", "setpicks", "preview",
              "donor", "setdonate", "setcontact", "addpromo", "delpromo", "promos", "underscoreaudit", "digest", "setupdatechannel", "recoversizes", "images", "survey", "parserrules", "testparse", "settitle", "setyear", "locktitle", "unlocktitle", "reparsefile", "reparsefilm", "dbsize", "catalogueaudit", "duplicatepreview", "filmids"]
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
        text = _preserve_group_badge(msg, text)
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


async def _send(client, target: int, text: str, rows=None) -> bool:
    """Return True only when Telegram accepts the message."""
    try:
        await client.send_message(target, text, parse_mode=HTML, reply_markup=kb(rows))
        return True
    except Exception as e:
        log.warning("couldn't message %s: %s", target, e)
        return False


async def notify_admins(ctx: Ctx, client, text: str, rows=None) -> None:
    """Alerts about the bot itself: always a direct message to every admin."""
    for a in ctx.cfg.admins:
        await _send(client, a, text, rows)


async def notify_inbox(ctx: Ctx, client, text: str, rows=None) -> bool:
    """Requests and file reports: the inbox channel if set, otherwise DM the admins."""
    if ctx.cfg.inbox_channel:
        return await _send(client, ctx.cfg.inbox_channel, text, rows)
    delivered = False
    for admin in ctx.cfg.admins:
        delivered = await _send(client, admin, text, rows) or delivered
    return delivered


async def deliver_donation_claim(ctx: Ctx, client, claim: dict, receipt_message=None, note: str = "") -> bool:
    """Send an identifiable claim card and a receipt labelled with the donor/claim identity."""
    cid = str(claim.get("id", claim.get("_id", "")))
    uid = int(claim["uid"])
    receipt_status = "Receipt attached below." if receipt_message else "Text/reference supplied by donor."
    admin_text = (
        "💰 <b>Éric Rohmer donation claim</b>\n\n"
        f"• <b>Donor:</b> {escape(claim.get('name') or 'Telegram user')}\n"
        f"• <b>Telegram ID (tap to copy):</b> <code>{uid}</code>\n"
        f"• <b>Payment method:</b> {escape(claim.get('method') or 'Not specified')}\n"
        f"• <b>Claim ID:</b> <code>{escape(cid)}</code>\n"
        f"• <b>Date:</b> {escape(str(claim.get('date', lagos_day())))}\n"
        f"• <b>Submission:</b> {escape(receipt_status)}\n"
        + (f"• <b>Donor's note/reference:</b> {escape(note)}\n" if note else "")
        + "\nReply to this message or the labelled receipt to message the donor. Review the payment before approving perks."
    )
    rows = ui.claim_admin_rows(cid)
    rows.append([Btn("↗ Message donor", url=f"tg://user?id={uid}")])
    try:
        if not await notify_inbox(ctx, client, admin_text, rows):
            return False
    except Exception as e:
        log.warning("couldn't deliver donation claim card: %s", e)
        return False
    if receipt_message is not None and any(getattr(receipt_message, key, None) for key in ("photo", "document", "video", "animation")):
        # copy_message can override the caption so a receipt never arrives in the inbox without its claim identity.
        copied = False
        for target in ([ctx.cfg.inbox_channel] if ctx.cfg.inbox_channel else list(ctx.cfg.admins)):
            try:
                await client.copy_message(
                    chat_id=target, from_chat_id=receipt_message.chat.id, message_id=receipt_message.id,
                    caption=f"🧾 Receipt for claim <code>{escape(cid)}</code>\nTelegram ID: <code>{uid}</code>",
                    parse_mode=HTML,
                )
                copied = True
            except Exception as e:
                log.warning("couldn't copy donation receipt to %s: %s", target, e)
        if not copied:
            return False
    return True


async def deliver_donation_followup(ctx: Ctx, client, claim: dict, message, from_admin: bool = False) -> bool:
    """Relay follow-up messages between the donor and admin inbox with claim identity attached."""
    cid = str(claim.get("id", claim.get("_id", "")))
    uid = int(claim["uid"])
    body = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if from_admin:
        return await _send(client, uid, f"💬 <b>Message from Éric Rohmer's admin</b>\nClaim: <code>{escape(cid)}</code>\n\n{escape(body)}", [[Btn("↩ Reply about this claim", f"dn:contact:{cid}")]])
    header = (f"💬 <b>Donor follow-up</b>\nTelegram ID: <code>{uid}</code>\n"
              f"Claim ID: <code>{escape(cid)}</code>\nPayment method: {escape(claim.get('method') or 'Not specified')}\n\n")
    try:
        followup_text = header + (escape(body) if body else "The donor sent an attachment; see the attached message.")
        if not await notify_inbox(ctx, client, followup_text, [[Btn("↩ Reply to donor", f"clm:reply:{cid}")]]):
            return False
        if any(getattr(message, k, None) for k in ("media", "photo", "document", "video", "animation")):
            targets = [ctx.cfg.inbox_channel] if ctx.cfg.inbox_channel else list(ctx.cfg.admins)
            copied = False
            for target in targets:
                try:
                    await client.copy_message(chat_id=target, from_chat_id=message.chat.id, message_id=message.id,
                                              caption=f"Follow-up for claim <code>{escape(cid)}</code> | Telegram ID <code>{uid}</code>",
                                              parse_mode=HTML)
                    copied = True
                except Exception as e:
                    log.warning("couldn't copy donation follow-up to %s: %s", target, e)
            return copied
        return True
    except Exception:
        log.exception("couldn't relay donation follow-up")
        return False


# Explicit replies to a bot-authored private prompt only. No persistent chat mode.
# The prompt itself survives a restart; ownership is checked against the claim.
async def _send_reply_prompt(client, uid, text):
    try:
        from pyrogram.types import ForceReply
        markup = ForceReply()
    except ImportError:  # local SDK stubs
        markup = None
    await client.send_message(uid, text, parse_mode=HTML, reply_markup=markup)


async def _claim_prompt(client, uid, cid, receipt):
    label = "Donation receipt" if receipt else "Donation follow-up"
    action = "your receipt, screenshot or payment reference" if receipt else "your message or attachment about this donation"
    await _send_reply_prompt(client, uid, f"<b>{label}</b> · <code>{escape(cid)}</code>\n\nReply to THIS message with {action}. Ordinary searches are not forwarded. Ignore this prompt to cancel.")


def _approval_rows(cid=None):
    rows = [[Btn("Feature my name", "wall:opt:yes"), Btn("Keep off the wall", "wall:opt:no")],
            [Btn("🌟 Supporters Wall", "dn:wall"), Btn("⭐ Custom requests", "customreq:start")]]
    if cid:
        rows.append([Btn("💬 Discuss this claim", f"dn:contact:{cid}")])
    return rows


def _film_request_key(title):
    import hashlib
    key = core_key(normalize(title))
    # Leave room for Telegram's 64-byte callback-data limit.
    return key if len(key.encode('utf-8')) <= 50 else 'rq-' + hashlib.sha1(key.encode('utf-8')).hexdigest()


async def _custom_request_prompt(ctx, client, uid):
    if not await ctx.is_supporter(uid):
        await _send(client, uid, "Custom multi-title requests are an active supporter benefit. You can still request missing titles from normal searches.", [[Btn("Support / Donate", "nav:donate")]])
        return
    await _send_reply_prompt(client, uid, "<b>Supporter custom requests</b>\n\nReply to THIS message with one title per line (up to 20 titles per submission, 80 characters each). Include years or season/episode numbers if needed. You may submit further batches. Only request titles you could not find. Ignore this prompt to cancel.")


async def cmd_supporter(ctx, client, m):
    if not m.from_user or m.from_user.id in ctx.banned or m.chat.type != ChatType.PRIVATE:
        return
    uid = m.from_user.id
    cmd = m.command[0].lower()
    if cmd == 'supporters':
        t, rows = ui.wall_screen(await ctx.store.get_supporters_wall())
        await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
    elif cmd == 'request':
        await _custom_request_prompt(ctx, client, uid)
    elif cmd == 'donationclaim':
        claim = await ctx.store.pending_claim_for_receipt(uid)
        if claim:
            await _claim_prompt(client, uid, str(claim.get('id', claim.get('_id'))), not claim.get('receipt_submitted'))
        else:
            await m.reply_text('No pending donation claim. Open Donate to submit a new claim, or use the Discuss button on a previous claim.')


async def _supporter_reply(ctx, client, m):
    if m.chat.type != ChatType.PRIVATE:
        return False
    rep = getattr(m, 'reply_to_message', None)
    author = getattr(rep, 'from_user', None)
    me = getattr(client, 'me', None)
    bot_id = getattr(ctx, "bot_id", None) or getattr(me, "id", None)
    # Never trust a forwarded/copied prompt or a user-authored look-alike.
    if not rep or not author or not bot_id or author.id != bot_id:
        return False
    prompt = str(getattr(rep, 'text', '') or '')
    match = re.match(r'^(Donation receipt|Donation follow-up|Donation admin reply) · (cl_[a-zA-Z0-9_-]+)\n', prompt)
    custom = prompt.startswith('Supporter custom requests\n\nReply to THIS message')
    if not match and not custom:
        return False
    uid = m.from_user.id
    text = (getattr(m, 'text', None) or getattr(m, 'caption', None) or '').strip()
    if text.lower() in ('cancel', 'stop', '/cancel'):
        await m.reply_text('Cancelled. Nothing was forwarded.')
        return True
    if custom:
        if not await ctx.is_supporter(uid):
            await m.reply_text('Your supporter access is no longer active. Use normal search requests instead.')
            return True
        titles = [line.strip() for line in text.splitlines() if line.strip()]
        if not titles or len(titles) > 20 or any(len(t) > 80 or not core_key(normalize(t)) or t.startswith('/') for t in titles):
            await m.reply_text('Reply to the custom request prompt with 1–20 valid titles, one per line, up to 80 characters each. Nothing was submitted.')
            return True
        seen = set(); submitted = duplicates = notification_failed = failed = 0
        for title in titles:
            if getattr(ctx, 'series_catalog', None) is not None:
                from series_handlers import _request_key
                key = _request_key(title)
            else:
                key = _film_request_key(title)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            try:
                count, new, added = await ctx.store.add_request(key, title, uid)
            except Exception:
                log.exception("custom request save failed")
                failed += 1
                continue
            if not added:
                duplicates += 1
                continue
            submitted += 1
            ctx.stats.add('r')
            notified = await notify_inbox(ctx, client, f'⭐ [SUPPORTER] Custom request: <code>{escape(title)}</code>\nRequester: <code>{uid}</code>\nRequesters: {count}\nKey: <code>{escape(key)}</code>', [[Btn('✅ Done', f'ar:done:{key}'), Btn('💬 Reply', f'ar:rep:{key}'), Btn('🗑️ Delete', f'ar:rm:{key}')]])
            if not notified:
                notification_failed += 1
        note = f"Submitted {submitted} request(s); {duplicates} duplicate(s) skipped."
        if failed:
            note += f" {failed} could not be saved; resend the batch to retry (saved entries are deduplicated)."
        if notification_failed:
            note += f" {notification_failed} saved request(s) could not be sent to the admin inbox; they remain in the request list."
        await m.reply_text(note + " You can reply to the custom request prompt with another batch.")
        return True
    kind, cid = match.groups()
    claim = await ctx.store.get_claim(cid)
    if not claim or (kind == 'Donation admin reply' and not ctx.is_admin(uid)) or (kind != 'Donation admin reply' and int(claim.get('uid', 0)) != uid):
        await m.reply_text('That donation claim is not available for your account.')
        return True
    attachment = any(getattr(m, k, None) for k in ('photo', 'document', 'video', 'animation'))
    if len(text) > 1000:
        await m.reply_text("Please shorten the donation note to 1000 characters or send an attachment.")
        return True
    if not text and not attachment:
        await m.reply_text('Reply with a message or supported receipt attachment.')
        return True
    if kind == 'Donation admin reply':
        ok = await deliver_donation_followup(ctx, client, claim, m, from_admin=True)
        if ok and attachment:
            try:
                await client.copy_message(chat_id=claim['uid'], from_chat_id=m.chat.id, message_id=m.id)
            except Exception:
                log.exception("admin donation attachment failed")
                ok = False
        await m.reply_text('Message sent to the donor.' if ok else 'Delivery failed or was incomplete. Reply to the prompt to retry.')
        return True
    if kind == 'Donation follow-up':
        ok = await deliver_donation_followup(ctx, client, claim, m)
        await m.reply_text('Your follow-up was sent.' if ok else 'Delivery failed. Reply to the prompt to try again.')
        return True
    if claim.get('status') != 'pending' or claim.get('receipt_submitted'):
        await m.reply_text('This receipt is already submitted or the claim is closed. Use /donationclaim or Discuss this claim to contact the admin.')
        return True
    if not await ctx.store.submit_claim_receipt(cid, text):
        await m.reply_text('The receipt could not be attached. Reopen /donationclaim.')
        return True
    try:
        fresh = await ctx.store.get_claim(cid)
        ok = await deliver_donation_claim(ctx, client, fresh or claim, receipt_message=m if attachment else None, note=text)
    except Exception:
        log.exception("donation receipt delivery failed")
        ok = False
    if not ok:
        await ctx.store.reset_claim_receipt(cid)
    await m.reply_text('Receipt sent for review. You can continue searching normally.' if ok else 'Delivery failed. Reply to the receipt prompt to retry.', reply_markup=kb([[Btn('💬 Discuss this claim', f'dn:contact:{cid}')]]) if ok else None)
    return True


def _supporter_show(show, name):
    prefix = f'⭐ Supporter · {escape(" ".join(str(name or "Supporter").split())[:60])}\n\n'
    async def wrapped(text, rows=None):
        return await show(prefix + text, rows)
    return wrapped


def _preserve_group_badge(msg, text):
    if getattr(getattr(msg, 'chat', None), 'type', None) not in GROUPS:
        return text
    old = str(getattr(msg, 'text', None) or getattr(msg, 'caption', None) or '')
    if old.startswith('⭐ Supporter · ') and '\n' in old and not text.startswith('⭐ Supporter · '):
        return escape(old.split('\n', 1)[0][:80]) + '\n\n' + text
    return text


async def load_variants(ctx: Ctx, fid: str) -> list[FileRec]:
    cached = ctx.variant_cache.get(fid)
    if cached is not None:
        return cached
    files = await ctx.store.film_files(fid)
    files.sort(key=lambda r: ((r.ext or "").lower() not in SUB_EXTS, quality_key_of(r.res, r.src), r.size),
               reverse=True)
    ctx.variant_cache.set(fid, files)
    return list(files)


async def _open_film_by_id(ctx: Ctx, show, uid: int, fid: str) -> None:
    """Open search results directly for one film ID (used by digest deep links)."""
    if not ctx.ready:
        await show(ui.WARMING)
        return
    film = ctx.index.get(fid)
    if film:
        title, year = film.title, film.year
    else:
        row = await ctx.store.get_film(fid)
        if not row or not row.get("n"):
            await show("That film isn't available any more.")
            return
        title, year = row["title"], row.get("year")
    variants = await load_variants(ctx, fid)
    if not variants:
        await show("That film isn't available any more.")
        return
    label = f"{title} ({year})" if year else title
    sess = Session(uid, title, "match", [(fid, label, len(variants))], group=False)
    sess.film = (fid, title, year)
    sess.variants = variants
    t, rows = await _film_view(ctx, sess)
    await _track(ctx, await show(t, rows), sess)


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
            if not ok and await ctx.check_join_request(uid, cid):
                ok = True
            break
        except UserNotParticipant:
            if await ctx.check_join_request(uid, cid):
                ctx.mark_joined(uid, cid)
                return True
            return False
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
    if ctx.is_admin(uid) or await ctx.has_join_bypass(uid):
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
    if pend["type"] == "film":
        await _open_film_by_id(ctx, edit_of(msg), uid, pend["fid"])
        return
    rec = await ctx.store.get_file(pend["uid"])
    if not rec:
        await edit_of(msg)("That file isn't available any more.")
        return
    await edit_of(msg)("Thanks. Sending your file…")
    await deliver(ctx, client, msg.chat.id, rec)


async def on_join_request(ctx: Ctx, client, req) -> None:
    """Treat a pending request as satisfying force-join; auto-approve when configured."""
    if not any(c["chat"] == req.chat.id for c in await ctx.subs()):
        return
    await ctx.anote_join_request(req.from_user.id, req.chat.id)
    await _resume_after_join(ctx, client, req.from_user.id, req.chat.id)
    if not (await ctx.settings())["auto_approve"]:
        return
    try:
        await client.approve_chat_join_request(req.chat.id, req.from_user.id)
    except Exception as e:
        log.warning("couldn't approve join request: %s", e)
        if ctx.first_time(f"approve-{req.chat.id}"):
            await notify_admins(ctx, client, "I couldn't approve a join request. In that channel I need the "
                                             "admin right to invite users.")


async def _resume_after_join(ctx: Ctx, client, uid: int, chat: int) -> None:
    """Resume the file/search action in the user's DM once every required channel is satisfied."""
    pend = ctx.get_pending(uid)
    if not pend:
        return
    pend["missing"].discard(chat)
    if pend["missing"]:
        return
    ctx.pending.pop(uid, None)

    async def show(text, rows=None, **kw):
        return await client.send_message(uid, text, reply_markup=kb(rows) if rows else None,
                                         parse_mode=HTML, disable_web_page_preview=True)
    try:
        if pend["type"] == "search":
            await run_search(ctx, show, uid, pend["text"], False)
        elif pend["type"] == "film":
            await _open_film_by_id(ctx, show, uid, pend["fid"])
        elif pend["type"] == "file":
            rec = await ctx.store.get_file(pend["uid"])
            if rec:
                await show("Thanks for joining. Sending your file…")
                await deliver(ctx, client, uid, rec)
            else:
                await show("That file isn't available any more.")
    except Exception:
        log.exception("could not resume after join")


async def on_chat_member(ctx: Ctx, client, upd) -> None:
    """Resume pending actions as soon as Telegram reports a successful channel join."""
    try:
        new = upd.new_chat_member
        if not new or not getattr(new, "user", None):
            return
        if new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            return
        if new.status == ChatMemberStatus.RESTRICTED and not getattr(new, "is_member", False):
            return
        uid, chat = new.user.id, upd.chat.id
        if not any(c["chat"] == chat for c in await ctx.subs()):
            return
        ctx.mark_joined(uid, chat)
        await _resume_after_join(ctx, client, uid, chat)
    except Exception:
        log.exception("chat member update failed")


# ---------------------------------------------------------------- delivering files
def file_action_rows(uid):
    return [[Btn("Report a problem", f"rep:{uid}")], [Btn("Media Information", f"mi:{uid}")]]


async def deliver(ctx: Ctx, client, chat_id: int, rec: FileRec, quiet: bool = False) -> bool:
    film = ctx.index.get(rec.film)
    s = await ctx.settings()
    is_sup = await ctx.is_supporter(chat_id)
    clean_files = await ctx.has_clean_files(chat_id)
    ad = s.get("autodelete_min", 0)
    minutes = max(ad, 45) if (ad and is_sup) else ad
    footer = "" if clean_files else s.get("footer", "")
    cap = ui.caption(film.title if film else "File", film.year if film else None, rec, minutes, footer)
    if len(cap) > 1024:
        cap = cap[:1020] + "…"

    sent = None
    for attempt in (1, 2):
        try:
            src = await client.get_messages(rec.ch, rec.mid)
            if not src or getattr(src, "empty", False) or not (getattr(src, "document", None) or getattr(src, "video", None) or getattr(src, "audio", None)):
                await _drop(ctx, rec)
                await client.send_message(chat_id, "That file is gone from the channel. Try another version.")
                return False
            if not rec.size or rec.size <= 0:
                s_media = getattr(src, "document", None) or getattr(src, "video", None) or getattr(src, "audio", None)
                actual_sz = int(getattr(s_media, "file_size", 0) or 0)
                if actual_sz > 0:
                    rec.size = actual_sz
                    await ctx.store.update_file_size(rec.uid, actual_sz)
                    ctx.variant_cache.invalidate(rec.film)
            try:
                sent = await src.copy(chat_id, caption=cap, parse_mode=HTML, protect_content=bool(s.get("protect", False)),
                                      reply_markup=kb(file_action_rows(rec.uid)))
            except (RPCError, ValueError) as me:
                if _markup_error(me) or isinstance(me, ValueError):
                    plain_cap = _plain(cap)
                    if len(plain_cap) > 1024:
                        plain_cap = plain_cap[:1020] + "…"
                    sent = await src.copy(chat_id, caption=plain_cap, parse_mode=ParseMode.DISABLED,
                                          protect_content=bool(s.get("protect", False)),
                                          reply_markup=kb(file_action_rows(rec.uid)))
                else:
                    raise
            if sent:
                break
        except FloodWait as e:
            if attempt == 2:
                await client.send_message(chat_id, "Telegram is busy right now. Please try again in a moment.")
                return False
            await asyncio.sleep(min(e.value + 1, 10))
        except (ChannelInvalid, ChannelPrivate):
            if ctx.first_time(f"ch-{rec.ch}"):
                await notify_admins(ctx, client, f"⚠️ <b>Channel access error:</b> I cannot read file channel <code>{rec.ch}</code>. "
                                                 "Please verify the bot is an Administrator in that channel.")
            await client.send_message(chat_id, "I can't access that file storage channel right now. Please notify the admin.")
            return False
        except RPCError as e:
            log.warning("copy RPCError: %s", e)
            if not quiet:
                await client.send_message(chat_id, "Couldn't send that file version. Try another version.")
            return False
        except Exception as e:
            log.exception("unexpected deliver error: %s", e)
            if not quiet:
                await client.send_message(chat_id, "Something went wrong while sending this file. Please try another version.")
            return False

    if not sent:
        return False

    await ctx.store.bump_hit(rec.film)
    ctx.index.record_open(rec.film)
    ctx.stats.add("f")
    if minutes and getattr(sent, "id", None):
        await ctx.store.schedule_delete(chat_id, sent.id, time.time() + minutes * 60)
    return True



async def _drop(ctx: Ctx, rec: FileRec) -> None:
    async with ctx.catalogue_lock:
        removed = await ctx.store.remove_file(rec.uid)
        if removed:
            ctx.variant_cache.invalidate(removed.film)
            ctx.index.adjust_files(removed.film, -1)
            if await ctx.store.remove_film_if_empty(removed.film):
                await asyncio.to_thread(ctx.index.remove_film, removed.film)


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
async def _results_view(ctx: Ctx, s: Session):
    back = Btn("‹ Discover", s.back) if s.back else None
    st = await ctx.settings()
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
        await ctx.store.add_miss(res.query, owner)
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
        t, rows = await _results_view(ctx, sess)
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
    if not group and await require_join(ctx, client, uid, show, {"type": "search", "text": text, "group": group}):
        return
    await run_search(ctx, show, uid, text, group)


async def _configure_digest_channel(ctx: Ctx, client, m, raw_target) -> bool:
    """Resolve and verify a Telegram channel for daily updates, then persist it."""
    target = raw_target
    if isinstance(target, str):
        val = target.strip()
        m_link = re.search(r"t\.me/([A-Za-z0-9_]{4,})$", val)
        if m_link:
            val = "@" + m_link.group(1)
        elif re.fullmatch(r"-?\d+", val):
            val = int(val)
        elif not val.startswith("@") and re.fullmatch(r"[A-Za-z0-9_]{4,}", val):
            val = "@" + val
        target = val
    try:
        chat = await client.get_chat(target)
        test_msg = await client.send_message(chat.id, "✅ Daily FilmBot updates channel verified.", parse_mode=HTML)
    except Exception as e:
        await m.reply_text(
            f"I couldn't post to that channel ({escape(str(e))}). "
            "Make sure I'm added there as an admin with permission to post messages, then try again."
        )
        return False
    title = getattr(chat, "title", None) or (f"@{chat.username}" if getattr(chat, "username", None) else str(chat.id))
    await ctx.set_setting("digest_channel", chat.id)
    await ctx.set_setting("digest_channel_title", title)
    await ctx.set_setting("digest_enabled", True)
    s = await ctx.settings()
    if s.get("digest_last_cutoff") is None:
        await ctx.set_setting("digest_last_cutoff", latest_due_cutoff(lagos_now(), 10, 0))
    return True


async def on_receipt_media(ctx: Ctx, client, m) -> None:
    if not m.from_user or m.chat.type != ChatType.PRIVATE or m.from_user.id in ctx.banned:
        return
    uid = m.from_user.id
    if ctx.is_admin(uid) and uid in ctx.pending_digest_channel:
        fwd = getattr(m, "forward_from_chat", None)
        if fwd and getattr(fwd, "id", None):
            ctx.pending_digest_channel.discard(uid)
            if await _configure_digest_channel(ctx, client, m, fwd.id):
                t, rows = ui.digest_settings_screen(await ctx.settings())
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
            return
        await m.reply_text("Forward a post from the target channel, or send its channel ID (like <code>-100...</code>) or <code>@username</code>.")
        return
    if await _supporter_reply(ctx, client, m):
        return


async def on_text(ctx: Ctx, client, m) -> None:
    if not m.from_user or m.from_user.id in ctx.banned:
        return
    uid, text = m.from_user.id, (m.text or "").strip()
    if await _supporter_reply(ctx, client, m):
        return
    if m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid) and uid in ctx.pending_digest_channel:
        fwd = getattr(m, "forward_from_chat", None)
        if text.lower() in ("cancel", "stop"):
            ctx.pending_digest_channel.discard(uid)
            t, rows = ui.digest_settings_screen(await ctx.settings())
            await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
            return
        target = fwd.id if (fwd and getattr(fwd, "id", None)) else text
        if target:
            ctx.pending_digest_channel.discard(uid)
            if await _configure_digest_channel(ctx, client, m, target):
                t, rows = ui.digest_settings_screen(await ctx.settings())
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
            return
    if m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid) and uid in ctx.pending_digest_setting:
        dstate = ctx.pending_digest_setting[uid]
        if text and not text.startswith("/"):
            if time.monotonic() - float(dstate.get("at", 0)) > 900:
                ctx.pending_digest_setting.pop(uid, None)
                await m.reply_text("Digest setting prompt expired. Reopen it from /digest.")
                return
            if text.lower() in ("cancel", "stop") and dstate.get("action") != "year":
                ctx.pending_digest_setting.pop(uid, None)
                t, rows = ui.digest_settings_screen(await ctx.settings())
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
                return
            if text.lower() == "cancel":
                ctx.pending_digest_setting.pop(uid, None)
                t, rows = ui.digest_settings_screen(await ctx.settings())
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
                return
            note = await digest_apply_text_setting(ctx, dstate.get("action", ""), text)
            if note.startswith("✅"):
                ctx.pending_digest_setting.pop(uid, None)
                t, rows = ui.digest_settings_screen(await ctx.settings())
                await m.reply_text(note + "\n\n" + t, parse_mode=HTML, reply_markup=kb(rows))
            else:
                dstate["at"] = time.monotonic()
                await m.reply_text(note)
            return
    if m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid) and uid in ctx.pending_claim_days:
        cid = ctx.pending_claim_days.pop(uid)
        claim = await ctx.store.get_claim(cid)
        if not text.isdigit() or not 1 <= int(text) <= 3650:
            ctx.pending_claim_bypass.discard(uid)
            await m.reply_text("Enter a whole number from 1 to 3650 days. Press the custom-days button again if needed.")
            return
        if not claim or claim.get("status") != "pending":
            await m.reply_text("That claim is no longer pending.")
            return
        days = int(text)
        new_date = await ctx.store.donor_add(claim["uid"], days=days, name=claim.get("name", ""))
        bypass_granted = uid in ctx.pending_claim_bypass
        ctx.pending_claim_bypass.discard(uid)
        if bypass_granted:
            await ctx.store.donor_set_join_bypass(claim["uid"], new_date)
        await ctx.store.resolve_claim(cid, f"approved_{days}d")
        await m.reply_text(f"Approved claim {cid} for {days} days. Access expires {new_date}." + (" Force-join bypass granted." if bypass_granted else ""))
        await _send(client, claim["uid"], f"🎉 <b>Donation verified!</b> Thank you for supporting Éric Rohmer. Your supporter access is active until <b>{new_date}</b>.\n\nChoose whether to feature your name on the Supporters Wall. Active supporters also receive a badge on group search responses and can use /request for custom requests.",
                    _approval_rows(cid))
        return
    if m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid) and uid in ctx.pending_curated:
        cstate = ctx.pending_curated[uid]
        if text and not text.startswith("/"):
            if time.monotonic() - float(cstate.get("at", 0)) > 900:
                ctx.pending_curated.pop(uid, None)
                await m.reply_text("Curated list prompt expired. Reopen it from /settings or /curated.")
                return
            if text.lower() in ("cancel", "stop"):
                ctx.pending_curated.pop(uid, None)
                await m.reply_text("Cancelled.")
                return
            act = cstate.get("action")
            s = await ctx.settings()
            lists = get_curated_lists(s)
            if act == "create":
                ctx.pending_curated.pop(uid, None)
                name = text.strip()[:40] or "Curated"
                lid = _new_list_id(name, {x["id"] for x in lists})
                lists.append({"id": lid, "name": name, "fids": []})
                await save_curated_lists(ctx, lists)
                t, rows = await _curated_list_screen(ctx, lid, 0)
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
                return
            if act == "rename":
                ctx.pending_curated.pop(uid, None)
                lid = cstate.get("list_id", "")
                lst = next((x for x in lists if x["id"] == lid), None)
                if lst:
                    lst["name"] = text.strip()[:40] or lst["name"]
                    await save_curated_lists(ctx, lists)
                t, rows = await _curated_list_screen(ctx, lid, 0)
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
                return
            if act == "add_search":
                lid = cstate.get("list_id", "")
                cstate["query"] = text.strip()
                cstate["at"] = time.monotonic()
                t, rows = await _curated_search_screen(ctx, lid, cstate["query"])
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
                return
            if act == "replace":
                ctx.pending_curated.pop(uid, None)
                lid = cstate.get("list_id", "")
                lst = next((x for x in lists if x["id"] == lid), None)
                if not lst:
                    await m.reply_text("That curated list no longer exists.")
                    return
                titles = [x.strip() for x in text.split("|") if x.strip()]
                chosen, missed = [], []
                for title in titles[:50]:
                    res = await asyncio.to_thread(ctx.index.search, title)
                    if res.hits and (len(res.hits) == 1 or normalize(res.hits[0].film.title) == normalize(title)):
                        chosen.append(res.hits[0].film.id)
                    else:
                        missed.append(title)
                lst["fids"] = list(dict.fromkeys(chosen))
                await save_curated_lists(ctx, lists)
                t, rows = await _curated_list_screen(ctx, lid, 0)
                note = ("\n\n<i>Not found: " + ", ".join(escape(x) for x in missed) + "</i>") if missed else ""
                await m.reply_text(t + note, parse_mode=HTML, reply_markup=kb(rows))
                return
    if m.chat.type == ChatType.PRIVATE:
        if uid in ctx.pending_stars_amount and text and not text.startswith("/"):
            ctx.pending_stars_amount.discard(uid)
            if not text.isdigit() or not 1 <= int(text) <= 10000:
                await m.reply_text("Please enter a whole number from 1 to 10,000 Stars. Open Donate → Telegram Stars → Custom amount to try again.")
                return
            amount = int(text)
            try:
                await client.send_invoice(
                    chat_id=uid, title="Éric Rohmer Support",
                    description=f"Support Éric Rohmer with {amount} Telegram Stars.",
                    payload=f"stars_{uid}_{amount}", currency="XTR",
                    prices=[LabeledPrice(f"Éric Rohmer support ({amount} Stars)", amount)]
                )
                await m.reply_text(f"Here's your invoice for {amount} Stars.")
            except Exception as e:
                log.exception("custom Stars invoice failed: %s", e)
                await m.reply_text("I couldn't create that Stars invoice. Please try again.")
            return
    if m.reply_to_message and ctx.is_admin(uid):
        rep_msg = m.reply_to_message
        rep_text = rep_msg.text or rep_msg.caption or ""
        claim_match = re.search(r"Claim ID:</b>\s*<code>([^<]+)</code>", rep_text, re.IGNORECASE)
        if not claim_match:
            claim_match = re.search(r"Receipt for claim\s*<code>([^<]+)</code>", rep_text, re.IGNORECASE)
        if not claim_match:
            claim_match = re.search(r"Claim ID[^A-Za-z0-9_-]*([A-Za-z0-9_-]+)", rep_text, re.IGNORECASE)
        if claim_match:
            claim = await ctx.store.get_claim(claim_match.group(1))
            if claim and claim.get("status") == "pending":
                ok = await deliver_donation_followup(ctx, client, claim, m, from_admin=True)
                await m.reply_text("Message sent to the donor." if ok else "Delivery to the donor failed.")
                return
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
    show = reply_to(m)
    if group and await ctx.is_supporter(uid):
        show = _supporter_show(show, getattr(m.from_user, "first_name", ""))
    await handle_query(ctx, client, show, uid, text, group)


# ---------------------------------------------------------------- start / help / discover
async def send_home(ctx: Ctx, m) -> None:
    s = await ctx.settings()
    text, rows = ui.home_screen(s, getattr(m.from_user, "first_name", "") or "", len(ctx.index) if ctx.ready else 0)
    if await ctx.is_supporter(m.from_user.id):
        rows.append([Btn("⭐ Custom requests", "customreq:start")])
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
    if arg.startswith("film_"):
        fid = arg[5:]
        if await require_join(ctx, client, uid, reply_to(m), {"type": "film", "fid": fid}):
            return
        await _open_film_by_id(ctx, reply_to(m), uid, fid)
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
        contact_url = ui.contact_link(s, next(iter(sorted(ctx.cfg.admins)), None))
        t, rows = ui.donate_screen(s, methods, wall, contact_url)
        await show(t, rows)
    elif cmd == "survey":
        if not s.get("survey_active", True):
            t, rows = ui.survey_closed_screen()
            await show(t, rows)
            return
        u = await ctx.store.get_user(m.from_user.id)
        if u and u.get("sv"):
            t, rows = ui.survey_completed_screen()
            await show(t, rows)
            return
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
    elif which.startswith("cur:"):
        lid = which.split(":", 1)[1]
        lst = next((x for x in get_curated_lists(s) if x["id"] == lid), None)
        name = lst["name"] if lst else "Curated list"
        fids = [f for f in (lst["fids"] if lst else []) if ctx.index.get(f)]
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


# ---------------------------------------------------------------- curated Discover lists
def get_curated_lists(s: dict) -> list[dict]:
    """Return normalized curated Discover lists ([{id, name, fids}]) without disturbing Hand-picked."""
    raw = s.get("curated_lists")
    lists: list[dict] = []
    if not isinstance(raw, list):
        return lists
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        lid = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("id") or f"list{idx + 1}"))[:12] or f"list{idx + 1}"
        while lid in seen_ids:
            lid = f"{lid[:8]}_{len(seen_ids)}"
        seen_ids.add(lid)
        name = str(item.get("name") or "Curated").strip()[:40] or "Curated"
        fids = list(dict.fromkeys(str(x) for x in (item.get("fids") or []) if x))
        lists.append({"id": lid, "name": name, "fids": fids})
    return lists


async def save_curated_lists(ctx: Ctx, lists: list[dict]) -> None:
    clean = [{"id": str(x["id"]), "name": str(x["name"]).strip()[:40] or "Curated",
              "fids": list(dict.fromkeys(str(f) for f in x.get("fids", []) if f))}
             for x in lists]
    await ctx.set_setting("curated_lists", clean)


def _new_list_id(name: str, existing_ids: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())[:8] or "list"
    cand, i = slug, 2
    while cand in existing_ids:
        cand = f"{slug[:6]}{i}"
        i += 1
    return cand


async def _curated_menu_screen(ctx: Ctx) -> tuple[str, list]:
    lists = get_curated_lists(await ctx.settings())
    rows = [[Btn(f"{lst['name']} · {len(lst['fids'])} film(s)"[:58], f"cur:view:{lst['id']}:0")] for lst in lists]
    rows.append([Btn("➕ Create curated list", "cur:create")])
    rows.append([Btn("‹ Settings", "st:refresh"), Btn("Close", "cur:close")])
    intro = ("<b>Curated Discover Lists</b>\n"
             "Select a list to view, add/remove films, rename, replace, or clear it.\n"
             "Non-empty lists appear on the Discover page alongside Hand-picked.")
    if not lists:
        intro += "\n\nNo curated lists yet. Create one below."
    return intro, rows


async def _curated_list_screen(ctx: Ctx, list_id: str, page: int = 0) -> tuple[str, list]:
    lists = get_curated_lists(await ctx.settings())
    lst = next((x for x in lists if x["id"] == list_id), None)
    if not lst:
        return await _curated_menu_screen(ctx)
    fids = lst["fids"]
    per = 10
    pages = max(1, (len(fids) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    chunk = fids[page * per:(page + 1) * per]
    lines = [f"<b>{escape(lst['name'])}</b> · {len(fids)} film(s)"]
    rows = []
    for fid in chunk:
        f = ctx.index.get(fid)
        title = f.label if f else f"(missing) {fid}"
        lines.append(f"• {escape(title)}")
    if not fids:
        lines.append("\n<i>This list is empty. Tap Add films to populate it.</i>")
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(Btn("‹ Prev", f"cur:view:{list_id}:{page - 1}"))
        nav.append(Btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(Btn("Next ›", f"cur:view:{list_id}:{page + 1}"))
        rows.append(nav)
    rows.append([Btn("➕ Add films", f"cur:add:{list_id}"), Btn("➖ Remove films", f"cur:rm:{list_id}:0")])
    rows.append([Btn("✏️ Rename", f"cur:ren:{list_id}"), Btn("🔄 Replace", f"cur:rep:{list_id}")])
    rows.append([Btn("🧹 Clear", f"cur:clr:{list_id}"), Btn("🗑️ Delete", f"cur:del:{list_id}")])
    rows.append([Btn("‹ All lists", "cur:menu"), Btn("‹ Settings", "st:refresh")])
    return "\n".join(lines), rows


async def _curated_remove_screen(ctx: Ctx, list_id: str, page: int = 0) -> tuple[str, list]:
    lst = next((x for x in get_curated_lists(await ctx.settings()) if x["id"] == list_id), None)
    if not lst:
        return await _curated_menu_screen(ctx)
    fids = lst["fids"]
    per = 10
    pages = max(1, (len(fids) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    rows = []
    for fid in fids[page * per:(page + 1) * per]:
        f = ctx.index.get(fid)
        title = f.label if f else fid
        rows.append([Btn(f"✖ {title}"[:58], f"cur:df:{list_id}:{fid}:{page}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(Btn("‹ Prev", f"cur:rm:{list_id}:{page - 1}"))
        nav.append(Btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(Btn("Next ›", f"cur:rm:{list_id}:{page + 1}"))
        rows.append(nav)
    rows.append([Btn("‹ Back to list", f"cur:view:{list_id}:0")])
    return f"<b>Remove films from {escape(lst['name'])}</b>\nTap a film to remove it:", rows


async def _curated_search_screen(ctx: Ctx, list_id: str, query: str) -> tuple[str, list]:
    lst = next((x for x in get_curated_lists(await ctx.settings()) if x["id"] == list_id), None)
    if not lst:
        return await _curated_menu_screen(ctx)
    res = await asyncio.to_thread(ctx.index.search, query)
    current = set(lst["fids"])
    rows = []
    for h in res.hits[:12]:
        mark = "✅ " if h.film.id in current else "➕ "
        rows.append([Btn(f"{mark}{h.film.label}"[:58], f"cur:tog:{list_id}:{h.film.id}")])
    if not rows:
        rows.append([Btn("No matching films found", "noop")])
    rows.append([Btn("🔍 Search again", f"cur:add:{list_id}"), Btn("‹ Back to list", f"cur:view:{list_id}:0")])
    return f"<b>Add/remove films in {escape(lst['name'])}</b>\nQuery: <code>{escape(query)}</code>\nTap a film to toggle it:", rows


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
    if msg is None:
        await cb.answer("This message is no longer available.",show_alert=True)
        return
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
            contact_url = ui.contact_link(s, next(iter(sorted(ctx.cfg.admins)), None))
            t, rows = ui.donate_screen(s, methods, wall, contact_url)
        elif arg == "discover":
            t, rows = ui.discover_screen(s)
        elif arg == "survey":
            if not s.get("survey_active", True):
                await cb.answer("The community survey is currently closed.", show_alert=True)
                return
            u = await ctx.store.get_user(cb.from_user.id)
            if u and u.get("sv"):
                t, rows = ui.survey_completed_screen()
                img = p_imgs.get("survey") or p_imgs.get("welcome") or s.get("image")
                await edit_of(msg, new_photo=img)(t, rows)
                await cb.answer()
                return
            t, rows = ui.survey_screen(0)
        else:
            t, rows = ui.home_screen(s, cb.from_user.first_name or "", len(ctx.index))
            if msg.chat.type == ChatType.PRIVATE and await ctx.is_supporter(uid):
                rows.append([Btn("⭐ Custom requests", "customreq:start")])
            img = p_imgs.get("welcome") or s.get("image")
        await edit_of(msg, new_photo=img)(t, rows)
        await cb.answer()
        return
    if head == "mi":
        from film_upgrade import media_callback
        await media_callback(ctx, client, cb, arg)
        return
    if head == "miclose":
        # Only the requester can close the independent metadata message.
        if arg == str(uid):
            await msg.delete()
        await cb.answer()
        return
    if head == "rep":
        await msg.edit_reply_markup(kb(ui.report_rows(arg)))
        await cb.answer()
        return
    if head == "rr":
        await _report(ctx, client, cb, arg)
        return
    if head in ("st", "cur", "dg", "rmfile", "rdis", "ixstop", "ddy", "ddn", "sk", "skt", "psy", "psn", "gst", "clm", "ar", "rqpg"):
        if not ctx.is_admin(uid):
            await cb.answer("Admins only.", show_alert=True)
            return
        await _admin_cb(ctx, client, cb, head, arg)
        return
    if head == "dn":
        if msg.chat.type != ChatType.PRIVATE or msg.chat.id != uid:
            await cb.answer("Open the bot privately to use Donate.", show_alert=True)
            return
        s = await ctx.settings()
        methods = s.get("donate_methods", [])
        if arg == "myclaim":
            claim = await ctx.store.pending_claim_for_receipt(uid)
            if not claim:
                await cb.answer("No pending donation claim. Choose a payment method to start one.", show_alert=True)
                return
            await _claim_prompt(client, uid, str(claim.get("id", claim.get("_id"))), not claim.get("receipt_submitted"))
            await cb.answer("Reply to the new donation prompt.")
            return
        if arg.startswith("contact:"):
            cid = arg.split(":", 1)[1]
            claim = await ctx.store.get_claim(cid)
            if not claim or int(claim.get("uid", 0)) != uid:
                await cb.answer("I couldn't find that claim for your account.", show_alert=True)
                return
            if msg.chat.type != ChatType.PRIVATE or msg.chat.id != uid:
                await cb.answer("Open the bot privately to discuss your claim.", show_alert=True)
                return
            await _claim_prompt(client, uid, cid, receipt=False)
            await cb.answer("Reply to the new donation prompt.")
            return
        if arg == "stars":
            t, rows = ui.stars_screen(uid)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        elif arg == "stcustom":
            ctx.pending_stars_amount.add(uid)
            await _send(client, uid, "Enter the number of Telegram Stars you want to contribute (1–10,000). I'll send an invoice for that exact amount.")
            await cb.answer("Enter your custom Stars amount in the chat.")
            return
        elif arg.startswith("st:"):
            stars_amt = int(arg.split(":")[1])
            try:
                await client.send_invoice(
                    chat_id=msg.chat.id,
                    title="Éric Rohmer Support",
                    description=f"Support Éric Rohmer with {stars_amt} Telegram Stars.",
                    payload=f"stars_{uid}_{stars_amt}",
                    currency="XTR",
                    prices=[LabeledPrice(f"Éric Rohmer support ({stars_amt} Stars)", stars_amt)]
                )
                await cb.answer("Invoice sent above! Tap to pay.")
            except Exception as e:
                log.exception("send_invoice failed: %s", e)
                await cb.answer("Could not send Stars invoice. Try another method.", show_alert=True)
            return
        elif arg == "myid":
            await cb.answer(f"Your User ID is: {uid}\n(Send this to the admin to claim perks)", show_alert=True)
            return
        elif arg.startswith("claimidx:") or arg.startswith("claim:"):
            if arg.startswith("claimidx:"):
                raw_idx = arg.split(":", 1)[1]
                if not raw_idx.isdigit() or int(raw_idx) >= len(methods):
                    await cb.answer("That payment method is no longer available. Please reopen Donate.", show_alert=True)
                    return
                method_name = methods[int(raw_idx)].get("title", "Donation")
            else:
                method_name = arg.split(":", 1)[1]
            user_name = cb.from_user.first_name or f"User {uid}"
            ok, cid = await ctx.store.add_claim(uid, user_name, method_name)
            existing = await ctx.store.get_claim(cid)
            if not existing or int(existing.get("uid", 0)) != uid:
                await cb.answer("Claim not found. Please reopen Donate.", show_alert=True)
                return
            await _claim_prompt(client, uid, cid, receipt=existing.get("status") == "pending" and not existing.get("receipt_submitted"))
            await cb.answer("Reply to the new donation prompt.", show_alert=True)
            return
        elif arg.startswith("m:") and arg[2:].isdigit():
            idx = int(arg[2:])
            if idx < len(methods):
                m_info = methods[idx]
                t, rows = ui.donate_method_screen(m_info, idx, uid)
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
        elif arg == "wall" or arg.startswith("wallpage:"):
            wall = await ctx.store.get_supporters_wall()
            page = arg.split(":", 1)[-1]
            t, rows = ui.wall_screen(wall, int(page) if page.isdigit() else 0)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        wall = await ctx.store.get_supporters_wall()
        contact_url = ui.contact_link(s, next(iter(sorted(ctx.cfg.admins)), None))
        t, rows = ui.donate_screen(s, methods, wall, contact_url)
        await edit_of(msg)(t, rows)
        await cb.answer()
        return
    if head == "wall":
        if msg.chat.type != ChatType.PRIVATE or msg.chat.id != uid:
            await cb.answer("Manage your wall preference privately.", show_alert=True)
            return
        if arg not in ("opt:yes", "opt:no", "1", "0", "yes", "no"):
            await cb.answer("Unknown preference.", show_alert=True)
            return
        flag = arg in ("opt:yes", "1", "yes")
        if flag and not await ctx.is_supporter(uid):
            await cb.answer("Only active supporters can join the wall.", show_alert=True)
            return
        if flag:
            # Existing manually granted supporters may not have a display name.
            await ctx.store.set_wall_name(uid, (cb.from_user.first_name or "Supporter")[:60])
        await ctx.store.set_wall_opt_in(uid, flag)
        await cb.answer("Your wall preference has been saved.", show_alert=True)
        return
    if head == "customreq":
        if msg.chat.type != ChatType.PRIVATE or msg.chat.id != uid:
            await cb.answer("Open the bot privately and use /request.", show_alert=True)
            return
        await _custom_request_prompt(ctx, client, uid)
        await cb.answer()
        return
    if head == "sv":
        s = await ctx.settings()
        if not s.get("survey_active", True):
            await cb.answer("The community survey is currently closed.", show_alert=True)
            return
        u = await ctx.store.get_user(uid)
        if u and u.get("sv"):
            t, rows = ui.survey_completed_screen()
            await edit_of(msg)(t, rows)
            await cb.answer("You have already completed the survey!", show_alert=True)
            return
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
                u_fresh = await ctx.store.get_user(uid)
                if not (u_fresh and u_fresh.get("sv")):
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
        p_imgs = s.get("page_images") or {}
        img = p_imgs.get("discover") or p_imgs.get("welcome") or s.get("image")
        await edit_of(msg, new_photo=img)(t, rows)
        sess = ctx.sessions.get(msg.chat.id, msg.id)
        if not sess:
            sess = Session(uid, "", "discover", [], group=msg.chat.type in GROUPS)
            ctx.sessions.put(msg.chat.id, msg.id, sess)
        else:
            sess.kind, sess.hits, sess.header, sess.back, sess.film, sess.shuffle = "discover", [], None, None, None, None
        await cb.answer()
        return

    sess = ctx.sessions.get(msg.chat.id, msg.id)
    if head == "ml" and arg == "" and not (sess and sess.kind == "mylist"):      # My list, opened from the home page
        mine = Session(uid, "", "mylist", [], group=msg.chat.type in GROUPS)
        ctx.sessions.put(msg.chat.id, msg.id, mine)
        t, rows = await _mylist_view(ctx, uid, mine)
        p_imgs = (await ctx.settings()).get("page_images") or {}
        img = p_imgs.get("mylist") or p_imgs.get("welcome") or (await ctx.settings()).get("image")
        await edit_of(msg, new_photo=img)(t, rows)
        await cb.answer()
        return
    if not sess:
        await cb.answer(ui.EXPIRED, show_alert=True)
        return
    if sess.owner != uid and not sess.group:               # in private chat, only the owner can use this
        await cb.answer(ui.NOT_YOURS, show_alert=True)
        return
    if sess.group and sess.owner != uid and head in ("r", "v", "b"):
        await cb.answer("Only the person who searched can navigate these results. Tap any file button to receive it in your DM.", show_alert=True)
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
            t, rows = await _results_view(ctx, sess)
        else:
            t, rows = f"<b>{escape(name)}</b>\nNothing here yet.", [[Btn("‹ Discover", "dc")]]
        await show(t, rows)
    elif head == "r" and arg.isdigit():
        sess.page = int(arg)
        t, rows = await _results_view(ctx, sess)
        await show(t, rows)
    elif head == "b":
        sess.film, sess.mode, sess.shuffle = None, "", None
        if sess.kind == "surprise":                            # Back from Surprise me goes to Discover
            sess.kind, sess.hits, sess.header, sess.back = "discover", [], None, None
            t, rows = ui.discover_screen(await ctx.settings())
        elif sess.kind == "mylist":
            t, rows = await _mylist_view(ctx, uid, sess, sess.page)
        else:
            t, rows = await _results_view(ctx, sess)
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
            # Everyone can browse group results. Deliver to the tapper's DM if possible;
            # otherwise open the bot with the file deep link so /start can resume the flow.
            if await missing_channels(ctx, client, uid):
                await cb.answer(url=ctx.deep_link(rec.uid))
                return
            try:
                sent_ok = await deliver(ctx, client, uid, rec, quiet=True)
            except Exception:
                log.exception("group file delivery failed for user %s", uid)
                sent_ok = False
            if sent_ok:
                await cb.answer("Sent to your private messages 📬")
            else:
                await cb.answer(url=ctx.deep_link(rec.uid))
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
        rec = await ctx.store.get_file(arg)
        if rec:
            await _drop(ctx, rec)
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
    qkey = _film_request_key(sess.query) or sess.qkey
    count, new, added = await ctx.store.add_request(qkey, sess.query[:80], cb.from_user.id)
    if added:
        ctx.stats.add("r")
    is_sup = await ctx.is_supporter(cb.from_user.id)
    sup_tag = "⭐ [SUPPORTER] " if is_sup else ""
    extra = " Open me in private and press Start, or I can't message you." if group else ""
    await cb.answer("Requested. I'll message you when it's added." + extra, show_alert=True)
    if new or (added and (count in REQUEST_PINGS or is_sup)):
        rq_rows = [
            [Btn("✅ Done", f"ar:done:{qkey}"), Btn("💬 Reply", f"ar:rep:{qkey}"), Btn("🗑️ Delete", f"ar:rm:{qkey}")]
        ]
        await notify_inbox(ctx, client, f"{sup_tag}Request: <code>{escape(sess.query)}</code>  ({count} "
                                        f"{'person' if count == 1 else 'people'})\nKey: <code>{qkey}</code>", rq_rows)


async def _report(ctx: Ctx, client, cb, arg: str) -> None:
    code, _, uid_arg = arg.partition(":")
    if code == "x":                                             # cancelled
        await cb.message.edit_reply_markup(kb(file_action_rows(uid_arg)))
        await cb.answer()
        return
    if code not in ui.REASON_NAMES:
        await cb.answer()
        return
    rec = await ctx.store.get_file(uid_arg)
    await cb.message.edit_reply_markup(kb([[Btn("Reported, thanks", "noop")], [Btn("Media Information", f"mi:{uid_arg}")]]))
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


async def _curated_cb(ctx: Ctx, cb, show, arg: str) -> None:
    uid = cb.from_user.id
    parts = str(arg).split(":")
    action = parts[0]
    if action in ("menu", "cancel"):
        ctx.pending_curated.pop(uid, None)
        t, rows = await _curated_menu_screen(ctx)
        await show(t, rows)
    elif action == "close":
        ctx.pending_curated.pop(uid, None)
        try:
            await cb.message.delete()
        except RPCError:
            pass
    elif action == "create":
        ctx.pending_curated[uid] = {"action": "create", "at": time.monotonic()}
        await show("<b>Create curated list</b>\nSend the name for the new curated Discover list (up to 40 characters).",
                   [[Btn("Cancel", "cur:cancel")]])
    elif action == "view" and len(parts) >= 2:
        ctx.pending_curated.pop(uid, None)
        lid, pg = parts[1], int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        t, rows = await _curated_list_screen(ctx, lid, pg)
        await show(t, rows)
    elif action == "add" and len(parts) >= 2:
        lid = parts[1]
        ctx.pending_curated[uid] = {"action": "add_search", "list_id": lid, "at": time.monotonic()}
        await show("<b>Add films to curated list</b>\nSend a film title (or part of a title) to search and select films manually.",
                   [[Btn("Cancel", f"cur:view:{lid}:0")]])
    elif action == "tog" and len(parts) >= 3:
        lid, fid = parts[1], parts[2]
        lists = get_curated_lists(await ctx.settings())
        lst = next((x for x in lists if x["id"] == lid), None)
        if not lst:
            await cb.answer("List not found.", show_alert=True)
            return
        if fid in lst["fids"]:
            lst["fids"].remove(fid)
            note = "Removed from list."
        else:
            lst["fids"].append(fid)
            note = "Added to list."
        await save_curated_lists(ctx, lists)
        pend = ctx.pending_curated.get(uid)
        if pend and pend.get("action") == "add_search" and pend.get("list_id") == lid and pend.get("query"):
            t, rows = await _curated_search_screen(ctx, lid, pend["query"])
        else:
            t, rows = await _curated_list_screen(ctx, lid, 0)
        await show(t, rows)
        await cb.answer(note)
        return
    elif action == "rm" and len(parts) >= 2:
        lid, pg = parts[1], int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        t, rows = await _curated_remove_screen(ctx, lid, pg)
        await show(t, rows)
    elif action == "df" and len(parts) >= 3:
        lid, fid = parts[1], parts[2]
        pg = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        lists = get_curated_lists(await ctx.settings())
        lst = next((x for x in lists if x["id"] == lid), None)
        if lst and fid in lst["fids"]:
            lst["fids"].remove(fid)
            await save_curated_lists(ctx, lists)
        t, rows = await _curated_remove_screen(ctx, lid, pg)
        await show(t, rows)
        await cb.answer("Film removed.")
        return
    elif action == "ren" and len(parts) >= 2:
        lid = parts[1]
        ctx.pending_curated[uid] = {"action": "rename", "list_id": lid, "at": time.monotonic()}
        await show("<b>Rename curated list</b>\nSend the new name for this curated list.",
                   [[Btn("Cancel", f"cur:view:{lid}:0")]])
    elif action == "rep" and len(parts) >= 2:
        lid = parts[1]
        ctx.pending_curated[uid] = {"action": "replace", "list_id": lid, "at": time.monotonic()}
        await show("<b>Replace curated list films</b>\nSend film titles separated by <code>|</code> "
                   "(for example: <code>Dune | Aprile | Se7en</code>).",
                   [[Btn("Cancel", f"cur:view:{lid}:0")]])
    elif action == "clr" and len(parts) >= 2:
        lid = parts[1]
        await show("Clear all films from this curated list?",
                   [[Btn("Yes, clear list", f"cur:clrok:{lid}"), Btn("Cancel", f"cur:view:{lid}:0")]])
    elif action == "clrok" and len(parts) >= 2:
        lid = parts[1]
        lists = get_curated_lists(await ctx.settings())
        lst = next((x for x in lists if x["id"] == lid), None)
        if lst:
            lst["fids"] = []
            await save_curated_lists(ctx, lists)
        t, rows = await _curated_list_screen(ctx, lid, 0)
        await show(t, rows)
        await cb.answer("List cleared.")
        return
    elif action == "del" and len(parts) >= 2:
        lid = parts[1]
        await show("Delete this curated list permanently?",
                   [[Btn("Yes, delete list", f"cur:delok:{lid}"), Btn("Cancel", f"cur:view:{lid}:0")]])
    elif action == "delok" and len(parts) >= 2:
        lid = parts[1]
        lists = [x for x in get_curated_lists(await ctx.settings()) if x["id"] != lid]
        await save_curated_lists(ctx, lists)
        t, rows = await _curated_menu_screen(ctx)
        await show(t, rows)
        await cb.answer("List deleted.")
        return
    await cb.answer()


async def _digest_cb(ctx: Ctx, client, cb, show, arg: str) -> None:
    uid = cb.from_user.id
    parts = arg.split(":")
    act = parts[0]
    if act in _DIGEST_PANEL_ACTS:
        await digest_panel_cb(ctx, client, cb, show, parts)
        return
    s = await ctx.settings()
    if act in ("menu", "cancel"):
        ctx.pending_digest_channel.discard(uid)
        ctx.pending_digest_setting.pop(uid, None)
        t, rows = ui.digest_settings_screen(s)
        await show(t, rows)
        await cb.answer()
        return
    if act == "tog":
        new_val = not bool(s.get("digest_enabled", False))
        if new_val and s.get("digest_last_cutoff") is None:
            now = lagos_now()
            init_cutoff = latest_due_cutoff(now, 10, 0)
            await ctx.set_setting("digest_last_cutoff", init_cutoff)
        await ctx.set_setting("digest_enabled", new_val)
        t, rows = ui.digest_settings_screen(await ctx.settings())
        await show(t, rows)
        await cb.answer("Digest setting updated.")
        return
    if act == "set":
        ctx.pending_digest_setting.pop(uid, None)
        ctx.pending_digest_channel.add(uid)
        await show(
            "<b>Set Daily Update Channel</b>\n\n"
            "Forward any message from the destination channel, or send its numeric ID (e.g. <code>-1001234567890</code>) "
            "or <code>@username</code>.\nMake sure I am an admin with permission to post messages there.\n"
            "Send <b>cancel</b> to go back.",
            [[Btn("Cancel", "dg:cancel")]],
        )
        await cb.answer()
        return
    if act == "test":
        ch = s.get("digest_channel")
        if not ch:
            await cb.answer("No update channel configured yet.", show_alert=True)
            return
        try:
            await client.send_message(ch, "🧪 <b>Test post:</b> FilmBot daily updates channel is working.", parse_mode=HTML)
            await cb.answer("Test message sent!", show_alert=True)
        except Exception as e:
            await cb.answer(f"Failed to send test message: {e}", show_alert=True)
        return
    if act == "clr":
        await ctx.set_setting("digest_channel", None)
        await ctx.set_setting("digest_channel_title", None)
        await ctx.set_setting("digest_enabled", False)
        t, rows = ui.digest_settings_screen(await ctx.settings())
        await show(t, rows)
        await cb.answer("Update channel cleared.")
        return
    await cb.answer()


async def _admin_cb(ctx: Ctx, client, cb, head: str, arg: str) -> None:
    show = edit_of(cb.message)
    if head == "st":
        s = await ctx.settings()
        if arg == "parserrules":
            from film_upgrade import rules_screen
            await show(*rules_screen(s))
            await cb.answer()
            return
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
        elif arg == "curated":
            ctx.pending_curated.pop(cb.from_user.id, None)
            t, rows = await _curated_menu_screen(ctx)
            await show(t, rows)
            return
        elif arg == "digest":
            ctx.pending_digest_channel.discard(cb.from_user.id)
            ctx.pending_digest_setting.pop(cb.from_user.id, None)
            t, rows = ui.digest_settings_screen(s)
            await show(t, rows)
            return
        elif arg == "refresh":
            t, rows = ui.settings_screen(s)
            await show(t, rows)
            return
        t, rows = ui.settings_screen(await ctx.settings())
        await show(t, rows)
    elif head == "cur":
        await _curated_cb(ctx, cb, show, arg)
        return
    elif head == "dg":
        await _digest_cb(ctx, client, cb, show, arg)
        return
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
        if action == "reply":
            if not claim:
                await cb.answer("Claim not found.", show_alert=True)
                return
            await _send_reply_prompt(client, cb.from_user.id, f"<b>Donation admin reply</b> · <code>{escape(cid)}</code>\n\nReply to THIS message with your reply to the donor. Ignore to cancel.")
            await cb.answer("Reply to the new prompt in your private chat.")
            return
        if not claim or claim.get("status") != "pending":
            await cb.answer(f"Claim already {claim.get('status', 'resolved') if claim else 'not found'}.", show_alert=True)
            return
        if action in ("custom", "bypass"):
            ctx.pending_claim_days[cb.from_user.id] = cid
            ctx.pending_claim_bypass.discard(cb.from_user.id)
            if action == "bypass":
                ctx.pending_claim_bypass.add(cb.from_user.id)
            await _send(client, cb.from_user.id,
                        f"How many days of supporter access should I grant for claim <code>{escape(cid)}</code>? Enter a whole number from 1 to 3650."
                        + (" This approval will also grant force-join bypass for the same period." if action == "bypass" else ""))
            await cb.answer("Enter the number of days in your private chat with the bot.")
            return
        if action == "dec":
            await ctx.store.resolve_claim(cid, "declined")
            await show(f"❌ <b>Claim Declined</b> (by {escape(cb.from_user.first_name)})\nUser: <code>{claim['uid']}</code>")
            await cb.answer("Claim declined.")
            await _send(client, claim["uid"], "⚠️ Your donation claim could not be verified. If you think this is an error or need to clarify the transfer, you can message the admin below.",
                        [[Btn("💬 Discuss this claim", f"dn:contact:{cid}")]])
        elif action in ("30", "60", "90"):
            days = int(action)
            new_date = await ctx.store.donor_add(claim["uid"], days=days, name=claim["name"])
            await ctx.store.resolve_claim(cid, f"approved_{days}d")
            await show(f"✅ <b>Claim Approved ({days} days)</b> by {escape(cb.from_user.first_name)}\nUser: <code>{claim['uid']}</code>\nExpires: {new_date}")
            await cb.answer(f"Approved {days} days!")
            await _send(client, claim["uid"], f"🎉 <b>Donation verified!</b> Thank you for supporting Éric Rohmer. Your supporter access is active for <b>{days} days</b>, until <b>{new_date}</b>.\n\nChoose whether to feature your name on the Supporters Wall. Active supporters also receive a badge on group search responses and can use /request for custom requests.",
                        _approval_rows(cid))
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
            lines.append(f"• <code>{escape(r['text'])}</code> ({len(r['users'])} users) — <code>/delreq {escape(r['key'])}</code>")
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
        rec = await ctx.store.get_file(arg)
        if rec:
            await _drop(ctx, rec)
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
                ctx.variant_cache.invalidate(f)
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
    async with ctx.catalogue_lock:
        return await _index_messages_locked(ctx, msgs)


async def _index_messages_locked(ctx: Ctx, msgs) -> IndexSummary:
    s = IndexSummary()
    cfg = await ctx.store.settings()
    promos = cfg.get("custom_promos", [])
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
        p = parse_best(fname, m.caption, promos=promos, rules=cfg.get("parser_rules"))
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
        key = film_id(normalize(p.title), p.year)
        fid = ctx.title_routes.get(key, key)
        if fid in ctx.title_locks:
            title, year = ctx.title_locks[fid]
        else:
            title, year = p.title, p.year
        films[fid] = (title, year)
        recs.append(FileRec(media.file_unique_id, fid, m.chat.id, m.id, size, p.resolution,
                            p.source, p.languages, (fname or "")[:255], p.codec, p.group, p.tags, p.ext))
    new, known, look = await ctx.store.add_files(recs, films)
    for fid in ({r.film for r in new} | ({r.film for r in recs} if known else set())):
        ctx.variant_cache.invalidate(fid)
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
        await asyncio.to_thread(ctx.index.add_films, [(films[f][0], films[f][1], n, 0, (), f) for f, n in per.items()])
    return s


async def on_channel_file(ctx: Ctx, client, m) -> None:
    if m.chat.id in ctx.cfg.file_channels:
        if ctx.auto_queue is None:
            await index_messages(ctx, [m])
        else:
            from types import SimpleNamespace as NS
            media, _ = _media_of(m)
            if media is None:
                return
            # Queue only metadata; never retain whole Telegram Client/Message graphs.
            payload = NS(file_name=getattr(media,"file_name",None), file_size=media.file_size,
                         file_unique_id=media.file_unique_id, mime_type=getattr(media,"mime_type",None))
            msg = NS(chat=NS(id=m.chat.id),id=m.id,caption=m.caption,document=payload if m.document else None,
                     video=payload if m.video else None,audio=None)
            ctx.catchup_high[m.chat.id] = max(ctx.catchup_high.get(m.chat.id,0),m.id)
            try:
                ctx.auto_queue.put_nowait(msg)
            except asyncio.QueueFull:
                log.error("auto-index queue full; catch-up will retry channel=%s message=%s",m.chat.id,m.id)


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
    job = ctx.index_job = ctx.index_job or {"stop": False}
    stop_rows = [[Btn("Stop", "ixstop")]]
    try:
        await ctx.store.set_setting("index_job", {"targets": [[c, f, l] for c, f, l in targets]})
        for ch, first, last in targets:
            if job["stop"]:
                break
            await _edit(status, f"Looking for the newest post in <code>{ch}</code>…", stop_rows)
            explicit_end = last is not None
            last = last if explicit_end else await find_last_id(client, ch, first)
            tot, started, tick, pos, empty_run = IndexSummary(), time.monotonic(), time.monotonic(), first, 0
            while not job["stop"] and not (pos > last and (explicit_end or empty_run >= EMPTY_BLOCKS_TO_STOP)):
                ids = list(range(pos, min(pos + 200, last + 1) if explicit_end else pos + 200))
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
            state = f"Stopped. Run /index {ch} to continue from here." if job["stop"] else "Done."
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


def _series_only(rows: list[dict], promos=(), rules=None) -> list[dict]:
    return [r for r in rows if parse_filename(r["name"], promos=promos, rules=rules).kind == "series"]


async def _purge_scan(ctx: Ctx, status) -> None:
    """Dry run: read every saved file name, count the ones that are really series episodes. Deletes nothing."""
    ctx.maintenance = "purge"
    found, scanned, chunk, tick = [], 0, [], time.monotonic()
    try:
        cfg = await ctx.store.settings()
        async for r in ctx.store.iter_file_names():
            chunk.append(r)
            if len(chunk) >= 500:
                found += await asyncio.to_thread(_series_only, [r for r in chunk if r["film"] not in ctx.title_locks], cfg.get("custom_promos", []), cfg.get("parser_rules"))       # off the main loop; keeps the bot responsive
                scanned, chunk = scanned + len(chunk), []
                if len(found) >= PURGE_ROUND:
                    break
                if time.monotonic() - tick >= 5:
                    tick = time.monotonic()
                    await _edit(status, f"Scanning… {scanned:,} files checked, {len(found):,} look like series so far.", None)
        if chunk and len(found) < PURGE_ROUND:
            found += await asyncio.to_thread(_series_only, [r for r in chunk if r["film"] not in ctx.title_locks], cfg.get("custom_promos", []), cfg.get("parser_rules"))
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
            batch = [r for r in items[i:i + 500] if r["film"] not in ctx.title_locks]
            removed += await ctx.store.delete_files(batch, kind="series")
            for it in batch:
                ctx.variant_cache.invalidate(it["film"])
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


async def _underscore_audit(ctx: Ctx, client, m, args: list[str], say) -> None:
    """Export indexed filenames containing underscores so admins can identify promo prefixes."""
    try:
        limit = int(args[0]) if args else 10000
        offset = int(args[1]) if len(args) > 1 else 0
    except (TypeError, ValueError):
        await say("Usage: <code>/underscoreaudit [limit] [offset]</code> (limit maximum 50000).")
        return
    limit = max(1, min(limit, 50000))
    offset = max(0, offset)
    if ctx.maintenance or ctx.index_job:
        await say("Another maintenance or indexing job is running. Try again when it finishes.")
        return
    ctx.maintenance = "underscoreaudit"
    status = None
    try:
        status = await say("Scanning indexed filenames for underscores. This is read-only and does not change the catalogue…")
        import csv
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["file_unique_id", "current_film_id", "channel_id", "message_id", "filename"])
        total = listed = 0
        async for row in ctx.store.iter_file_names():
            name = row.get("name") or ""
            if "_" not in name:
                continue
            total += 1
            if total <= offset or listed >= limit:
                continue
            writer.writerow([row.get("uid", ""), row.get("film", ""), row.get("ch", ""),
                             row.get("mid", ""), name])
            listed += 1
        data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
        data.name = "filmbot-underscore-audit.csv"
        await client.send_document(m.chat.id, data,
                                   caption=(f"Found {total:,} indexed filenames containing underscores. "
                                            f"Exported {listed:,} from position {offset + 1:,}. "
                                            "This report is read-only; no files or titles were changed."))
        await _edit(status, f"Underscore audit complete: {total:,} matching filenames; {listed:,} exported.\n"
                            f"Exported positions {offset + 1:,}–{offset + listed:,}. "
                            "Review the filename prefixes, then register only confirmed promo phrases with /addpromo. "
                            "Use /reparse dry before applying a full reparse.", None)
    except Exception as e:
        log.exception("underscore audit failed")
        if status:
            await _edit(status, f"Underscore audit failed: {escape(str(e))}", None)
        else:
            await say(f"Underscore audit failed: {escape(str(e))}")
    finally:
        ctx.maintenance = None


async def _rebuild_search_index(ctx: Ctx) -> None:
    """Rebuild the in-memory search index from the authoritative film collection."""
    from index import FilmIndex
    fresh = FilmIndex()
    batch = []
    async for d in ctx.store.iter_films():
        if not d["n"]:
            continue
        batch.append((d["title"], d["year"], d["n"], d["hits"], tuple(d["aliases"]), d["fid"]))
        if len(batch) >= 3000:
            await asyncio.to_thread(fresh.add_films, batch)
            batch = []
    if batch:
        await asyncio.to_thread(fresh.add_films, batch)
    ctx.index = fresh


async def _reparse_cmd(ctx: Ctx, client, m, args: list[str], say) -> None:
    """Reprocess indexed filenames, or only the unreadable entries under /skipped."""
    mode = (args[0].lower() if args else "all")
    dry = "dry" in [a.lower() for a in args]
    skipped_only = mode == "skipped"
    if mode not in ("all", "skipped", "dry"):
        await say("Usage: <code>/reparse</code>, <code>/reparse dry</code>, <code>/reparse skipped</code>, or <code>/reparse skipped dry</code>.")
        return
    if ctx.maintenance or ctx.index_job:
        await say("Another maintenance or indexing job is running. Try again when it finishes.")
        return
    ctx.maintenance = "reparse"
    try:
        await ctx.catalogue_lock.acquire()
    except BaseException:
        ctx.maintenance = None
        raise
    scanned = changed = unchanged = unreadable = series = missing = 0
    examples = []
    status = None
    try:
        status = await say("Preparing reparse dry run…" if dry else "Preparing reparse…")
        cfg = await ctx.settings()
        promos = cfg.get("custom_promos", [])
        if skipped_only:
            rows = await ctx.store.skips_all("unreadable")
            total = len(rows)
            for i, row in enumerate(rows, 1):
                try:
                    msg = await client.get_messages(row["c"], row["m"])
                    if not msg or getattr(msg, "empty", False):
                        missing += 1
                        continue
                    media, ext = _media_of(msg)
                    if media is None:
                        missing += 1
                        continue
                    raw = getattr(media, "file_name", None) or row.get("n") or ""
                    parsed = parse_best(raw, getattr(msg, "caption", None), promos=promos, rules=cfg.get("parser_rules"))
                    scanned += 1
                    if is_junk(parsed.title) or not normalize(parsed.title):
                        unreadable += 1
                    elif parsed.kind == "series":
                        series += 1
                    else:
                        key = film_id(normalize(parsed.title), parsed.year)
                        fid = ctx.title_routes.get(key, key)
                        rec = FileRec(media.file_unique_id, fid, msg.chat.id, msg.id, media.file_size or row.get("z", 0),
                                      parsed.resolution, parsed.source, parsed.languages, raw[:255], parsed.codec,
                                      parsed.group, parsed.tags, parsed.ext or ext)
                        existing = await ctx.store.get_file(rec.uid)
                        if existing and existing.film in ctx.title_locks:
                            unchanged += 1
                            continue
                        if existing and existing.film == rec.film and existing.name == rec.name and existing.res == rec.res and existing.src == rec.src and existing.langs == rec.langs and existing.codec == rec.codec and existing.grp == rec.grp and existing.tags == rec.tags and existing.ext == rec.ext:
                            unchanged += 1
                        else:
                            changed += 1
                            if len(examples) < 8:
                                examples.append(f"• <code>{escape(raw[:75])}</code> → <b>{escape(parsed.title)}</b>{f' ({parsed.year})' if parsed.year else ''}")
                            if not dry:
                                title, year = ctx.title_locks.get(rec.film, (parsed.title, parsed.year))
                                await ctx.store.reparse_file(rec, title, year)
                                if await ctx.store.get_file(rec.uid):
                                    await ctx.store.clear_skips([rec.uid])
                except Exception:
                    log.exception("reparse skipped item failed: %s", row.get("uid"))
                    missing += 1
                if i % 50 == 0:
                    await _edit(status, f"Reparse skipped: {i:,}/{total:,} checked…", None)
        else:
            # Keyset-paginated batch scan over _id; bulk-write only changed file docs during scan,
            # move newly detected series files to skips, and reconcile films + delete empty ones once at end.
            index_series = bool(cfg.get("index_series"))
            total = (await ctx.store.counts())["files"]
            last_progress = 0.0
            film_meta: dict[str, tuple[str, int | None]] = {}
            async for batch in ctx.store.iter_file_batches(batch_size=500):
                pending = []
                series_uids = []
                series_notes = []
                for old in batch:
                    if old.film in ctx.title_locks:
                        scanned += 1
                        unchanged += 1
                        continue
                    parsed = parse_best(old.name or "", promos=promos, rules=cfg.get("parser_rules"))
                    scanned += 1
                    if is_junk(parsed.title) or not normalize(parsed.title):
                        unreadable += 1
                        continue
                    if parsed.kind == "series" and not index_series:
                        series += 1
                        series_uids.append(old.uid)
                        series_notes.append({"kind": "series", "uid": old.uid, "name": old.name or parsed.raw,
                                             "ch": old.ch, "mid": old.mid, "size": old.size})
                        continue
                    fid = ctx.title_routes.get(film_id(normalize(parsed.title), parsed.year), film_id(normalize(parsed.title), parsed.year))
                    rec = FileRec(old.uid, fid, old.ch, old.mid, old.size, parsed.resolution,
                                  parsed.source, parsed.languages, (old.name or "")[:255], parsed.codec,
                                  parsed.group, parsed.tags, parsed.ext or old.ext)
                    same = (old.film == rec.film and old.res == rec.res and old.src == rec.src and
                            old.langs == rec.langs and old.name == rec.name and old.codec == rec.codec and
                            old.grp == rec.grp and old.tags == rec.tags and old.ext == rec.ext)
                    if same:
                        unchanged += 1
                    else:
                        changed += 1
                        pending.append(rec)
                        if not dry:
                            film_meta[rec.film] = ctx.title_locks.get(rec.film, (parsed.title, parsed.year))
                        if len(examples) < 8:
                            examples.append(f"• <code>{escape((old.name or '')[:75])}</code> → <b>{escape(parsed.title)}</b>{f' ({parsed.year})' if parsed.year else ''}")
                if not dry:
                    if pending:
                        await ctx.store.bulk_reparse_files(pending)
                        await ctx.store.clear_skips([r.uid for r in pending])
                    if series_uids:
                        await ctx.store.note_skips(series_notes)
                        await ctx.store.bulk_remove_files(series_uids)
                now = time.monotonic()
                if now - last_progress >= 4 or scanned >= total:
                    await _edit(status, f"Reparse: {scanned:,}/{total:,} files checked · {changed:,} {'would change' if dry else 'changed'} · {series:,} series…", None)
                    last_progress = now
                await asyncio.sleep(0)
        rb = None
        if not dry:
            rb = await ctx.store.rebuild_films(film_meta if not skipped_only else None)
            ctx.variant_cache.clear()
            await _rebuild_search_index(ctx)
        label = "Dry run — no database changes made" if dry else "Reparse complete"
        scope = "unreadable skipped files" if skipped_only else "existing indexed files"
        lines = [f"<b>{label}</b>", f"Scope: {scope}", f"Scanned: {scanned:,}",
                 f"Would update / updated: {changed:,}", f"Already current: {unchanged:,}"]
        if skipped_only:
            lines += [f"Still unreadable: {unreadable:,}", f"Series-looking (left skipped): {series:,}",
                      f"Unavailable channel posts: {missing:,}"]
        else:
            series_note = "would move to /skipped" if dry else "moved to /skipped"
            lines += [f"Unreadable (left unchanged): {unreadable:,}", f"Series-looking ({series_note}): {series:,}"]
            if rb is not None:
                lines.append(f"Empty films removed: {rb.get('removed_empty', 0):,}")
        if examples:
            lines += ["", "Examples:"] + examples
        await _edit(status, "\n".join(lines), None)
    except Exception as e:
        log.exception("reparse failed")
        if status is not None:
            await _edit(status, f"Reparse failed: {escape(str(e))}", None)
        else:
            await say(f"Reparse failed: {escape(str(e))}")
    finally:
        ctx.catalogue_lock.release()
        ctx.maintenance = None


async def on_admin_cmd(ctx: Ctx, client, m) -> None:
    if not m.from_user or not ctx.is_admin(m.from_user.id):
        return
    cmd, args = m.command[0].lower(), m.command[1:]
    say = lambda t, rows=None: m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))   # noqa: E731

    from film_upgrade import admin_command
    if await admin_command(ctx, client, m, cmd, args, say):
        return

    if cmd == "stats":
        c = await ctx.store.counts()
        mem, subs = rss_mb(), await ctx.subs()
        day = lagos_day()
        saved, waiting = await ctx.store.stats_get(day), ctx.stats.peek(day)
        t = {k: saved.get(k, 0) + waiting.get(k, 0) for k in set(saved) | set(waiting)}
        lines = [f"Files: {c['files']:,}", f"Films: {c['films']:,}  (search index: {len(ctx.index):,})",
                 f"Users: {c['users']:,}", f"Supporters: {c.get('supporters', 0):,}", f"Open requests: {c['requests']}",
                 f"Surveys taken: {((await ctx.store.get_survey_stats()).get('_meta') or {}).get('count', 0):,}"]
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
    elif cmd == "reparse":
        await _reparse_cmd(ctx, client, m, args, say)
    elif cmd == "underscoreaudit":
        await _underscore_audit(ctx, client, m, args, say)
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
        if sub == "claims":
            claims = await ctx.store.list_pending_claims(20)
            lines = ["<b>Pending donation claims (up to 20)</b>"]
            for claim in claims:
                cid = escape(str(claim.get("id", "")))
                state = "receipt marked submitted" if claim.get("receipt_submitted") else "awaiting receipt"
                lines.append(f"<code>/donor claim {cid}</code> — {state}")
            if not claims:
                lines.append("No pending claims.")
            await say("\n".join(lines))
        elif sub == "claim" and len(args) == 2:
            claim = await ctx.store.get_claim(args[1])
            if not claim:
                await say("Claim not found.")
                return
            cid = claim["id"]
            status = claim.get("status", "unknown")
            state = "marked submitted" if claim.get("receipt_submitted") else "not submitted"
            lines = ["<b>Donation claim recovery</b>", f"Claim: <code>{escape(cid)}</code>",
                     f"Telegram ID: <code>{int(claim['uid'])}</code>",
                     f"Method: {escape(str(claim.get('method', ''))[:100])}",
                     f"Status: {escape(status)} · Receipt: {state}",
                     f"Saved note: {escape(str(claim.get('note', ''))[:500])}",
                     "Verify payment independently. A submitted flag does not prove that a receipt was delivered. Ask the donor to resend proof using /donationclaim if necessary."]
            await say("\n".join(lines), ui.claim_admin_rows(cid) if status == "pending" else [[Btn("Reply to donor", f"clm:reply:{cid}")]])
        elif sub == "add" and len(args) >= 2 and args[1].isdigit():
            duid = int(args[1])
            days = int(args[2]) if len(args) >= 3 and args[2].isdigit() else 30
            note = " ".join(args[3:]) if len(args) >= 4 else "manual"
            await ctx.store.donor_add(duid, days=days, name=note)
            await _send(client, duid, "⭐ Your supporter access is active. Choose whether to appear on the Supporters Wall, or use /request for custom requests.", _approval_rows())
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
            await say("Usage:\n<code>/donor add UID [DAYS] [NOTE]</code>\n<code>/donor remove UID</code>\n<code>/donor list</code>\n<code>/donor claims</code>\n<code>/donor claim CLAIM_ID</code>")
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
                for i, mth in enumerate(methods, 1):
                    lines.append(f"\n<b>{i}. {escape(mth.get('title',''))}</b> ({escape(mth.get('type',''))})\n{escape(mth.get('details',''))}")
                    lines.append(f"<code>/setdonate edit {i} | TYPE | TITLE | DETAILS</code>")
                    lines.append(f"<code>/setdonate remove {i}</code>")
                await say("\n".join(lines))
        elif sub == "remove":
            if len(args) < 2 or not args[1].isdigit():
                await say("Usage: <code>/setdonate remove NUMBER</code>. Get numbers with <code>/setdonate list</code>.")
            else:
                idx = int(args[1]) - 1
                if not 0 <= idx < len(methods):
                    await say("That payment method number doesn't exist. Check <code>/setdonate list</code>.")
                else:
                    removed = methods.pop(idx)
                    await ctx.set_setting("donate_methods", methods)
                    await say(f"Removed donation method <b>{escape(removed.get('title', ''))}</b>.")
        elif sub in ("add", "edit"):
            body = " ".join(args[1:])
            parts = [p.strip() for p in body.split("|")]
            edit_idx = None
            if sub == "edit":
                if len(args) < 3 or not args[1].isdigit():
                    await say("Usage: <code>/setdonate edit NUMBER | TYPE | TITLE | DETAILS</code>")
                    return
                edit_idx = int(args[1]) - 1
                body = " ".join(args[2:])
                parts = [p.strip() for p in body.split("|")]
                if not 0 <= edit_idx < len(methods):
                    await say("That payment method number doesn't exist. Check <code>/setdonate list</code>.")
                    return
            if len(parts) >= 3 and parts[0] and parts[1]:
                m_type, m_title = parts[0], parts[1]
                fields = parts[2:]
                # Accept the short bank format the owner already used, while retaining labelled/custom fields.
                if m_type.lower() == "bank" and len(fields) >= 3 and not any(":" in x for x in fields[:3]):
                    details = f"Bank: {fields[0]}\nAccount number: {fields[1]}\nAccount name: {' | '.join(fields[2:])}"
                else:
                    details = "\n".join(x for x in fields if x)
                old = methods[edit_idx] if edit_idx is not None else {}
                entry = {"type": m_type, "title": m_title, "details": details,
                         "qr": old.get("qr")}
                if edit_idx is None:
                    methods.append(entry)
                    action = "Added"
                else:
                    methods[edit_idx] = entry
                    action = "Updated"
                await ctx.set_setting("donate_methods", methods)
                await say(f"{action} donation method: <b>{escape(m_title)}</b>. View with /setdonate list.")
            else:
                await say("<b>Usage:</b> <code>/setdonate add TYPE | TITLE | DETAILS | MORE DETAILS</code>\n\n"
                          "Example bank: <code>/setdonate add bank | Bank Transfer | Opay | 8031656616 | Martin Akinola</code>\n"
                          "Example USDT: <code>/setdonate add crypto | USDT (BEP20) | Network: BSC (BEP20) | Address: 0xYourWalletAddress</code>\n"
                          "For labelled details, separate each field with <code>|</code>. All fields after TITLE are saved.")
        else:
            msg_lines = [
                "<b>Configure Payment Methods (/setdonate):</b>",
                "<code>/setdonate add TYPE | TITLE | DETAILS | MORE DETAILS</code>",
                "<code>/setdonate list</code> — view methods and their numbers",
                "<code>/setdonate remove NUMBER</code> — remove one method",
                "<code>/setdonate edit NUMBER | TYPE | TITLE | DETAILS</code> — replace one method",
                "<code>/setdonate reset</code> — clear all methods",
                "\nAll fields after TITLE are saved. Use <code>|</code> between fields."
            ]
            await say("\n".join(msg_lines))
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
    elif cmd == "curated":
        ctx.pending_curated.pop(m.from_user.id, None)
        t, rows = await _curated_menu_screen(ctx)
        await say(t, rows=rows)
    elif cmd == "setcontact":
        value = " ".join(args).strip()
        if not value:
            current = (await ctx.settings()).get("donate_contact")
            await say("Usage: <code>/setcontact @yourusername</code> (or a t.me link). <code>/setcontact clear</code> removes it, and the Donate page then links to the first admin's account.\n"
                      f"Current: {escape(str(current)) if current else 'not set'}")
        elif value.lower() == "clear":
            await ctx.set_setting("donate_contact", None)
            await say("Contact button reset. It now links to the first admin's account.")
        else:
            await ctx.set_setting("donate_contact", value)
            link = ui.contact_link({"donate_contact": value})
            await say(f"Donate page contact button now opens: {escape(str(link))}")
    elif cmd in ("addpromo", "delpromo", "promos"):
        promos = list((await ctx.settings()).get("custom_promos", []))
        value = " ".join(args).strip()
        if cmd == "promos" and not value:
            body = "\n".join(f"• <code>{escape(x)}</code>" for x in promos) if promos else "No custom promo words configured."
            await say("<b>Custom promo words</b>\n"
                      f"{body}\n\n"
                      "Add exact promo prefixes: <code>/addpromo PFM</code> (or <code>/addpromo word1 | word2</code>)\n"
                      "Remove: <code>/delpromo PFM</code>\n"
                      "Underscore filename review: <code>/underscoreaudit</code>\n"
                      "Only registered promo phrases are stripped from underscore-containing prefixes. Run <code>/reparse dry</code> before applying changes.")
        elif not value:
            await say(f"Usage: <code>/{cmd} &lt;promo word&gt;</code>")
        elif cmd == "addpromo":
            items = [x.strip() for x in value.split("|") if x.strip()]
            seen = {x.casefold() for x in promos}
            added = []
            for item in items:
                if item.casefold() not in seen:
                    promos.append(item)
                    seen.add(item.casefold())
                    added.append(item)
            await ctx.set_setting("custom_promos", promos)
            await say(("Added promo word(s): " + ", ".join(f"<b>{escape(x)}</b>" for x in added)
                       if added else "Already in the promo list.")
                      + "\nRun <code>/reparse dry</code> to preview the affected catalogue changes before applying them with <code>/reparse</code>.")
        elif cmd == "delpromo":
            targets = {x.strip().casefold() for x in value.split("|") if x.strip()}
            promos = [x for x in promos if x.casefold() not in targets]
            await ctx.set_setting("custom_promos", promos)
            await say(f"Removed promo word(s): <b>{escape(value)}</b>.\nRun <code>/reparse</code> to apply.")
        else:
            await say("Use <code>/addpromo &lt;word&gt;</code>, <code>/delpromo &lt;word&gt;</code>, or <code>/promos</code>.")
    elif cmd == "digest":
        ctx.pending_digest_channel.discard(m.from_user.id)
        ctx.pending_digest_setting.pop(m.from_user.id, None)
        t, rows = ui.digest_settings_screen(await ctx.settings())
        await say(t, rows=rows)
    elif cmd == "setupdatechannel":
        raw = " ".join(args).strip()
        if not raw:
            await say("Usage: <code>/setupdatechannel @channel</code> or <code>/setupdatechannel -100...</code> (or use /settings → Daily Updates).")
        elif raw.lower() in ("off", "clear", "none"):
            await ctx.set_setting("digest_channel", None)
            await ctx.set_setting("digest_channel_title", None)
            await ctx.set_setting("digest_enabled", False)
            await say("Daily update channel cleared.")
        else:
            if await _configure_digest_channel(ctx, client, m, raw):
                t, rows = ui.digest_settings_screen(await ctx.settings())
                await say(t, rows=rows)
    elif cmd == "recoversizes":
        if ctx.maintenance:
            await say("Another maintenance job is running. Try again when it finishes.")
            return
        ctx.maintenance = "recoversizes"
        status = await say("Scanning for files missing byte size metadata…")
        try:
            checked = recovered = missing = 0
            async for batch in ctx.store.iter_zero_size_files(batch_size=200):
                by_ch: dict[int, list[FileRec]] = {}
                for rec in batch:
                    by_ch.setdefault(rec.ch, []).append(rec)
                updates: dict[str, int] = {}
                touched_fids: set[str] = set()
                for ch, recs in by_ch.items():
                    try:
                        msgs = await _fetch(client, ch, [r.mid for r in recs])
                    except Exception:
                        msgs = []
                    by_mid = {getattr(x, "id", None): x for x in (msgs or []) if x and not getattr(x, "empty", False)}
                    for rec in recs:
                        checked += 1
                        msg = by_mid.get(rec.mid)
                        media = (getattr(msg, "document", None) or getattr(msg, "video", None)
                                 or getattr(msg, "audio", None)) if msg else None
                        sz = int(getattr(media, "file_size", 0) or 0) if media else 0
                        if sz > 0:
                            updates[rec.uid] = sz
                            touched_fids.add(rec.film)
                        else:
                            missing += 1
                if updates:
                    recovered += await ctx.store.bulk_update_sizes(updates)
                    for fid in touched_fids:
                        ctx.variant_cache.invalidate(fid)
            await _edit(status, f"<b>Size recovery complete</b>\nChecked: {checked:,}\nRecovered &amp; saved: {recovered:,}\nUnavailable: {missing:,}", None)
        except Exception as e:
            log.exception("recoversizes failed")
            await _edit(status, f"Size recovery failed: {escape(str(e))}", None)
        finally:
            ctx.maintenance = None
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
            lines.append(f"• <code>{escape(r['text'])}</code> ({len(r['users'])} users) — <code>/delreq {escape(r['key'])}</code>")
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
    if not ctx.ready:
        await say("Wait until the catalogue has finished loading.")
        return
    if ctx.index_job or ctx.maintenance:
        await say("An indexing run or maintenance job is already going. /indexstop to stop indexing.")
        return
    cps = await ctx.store.settings()
    nums = [a for a in args if re.fullmatch(r"-?\d+", a)]
    if len(nums) != len(args):
        await say("Usage: <code>/index</code> (everything, resuming)\n<code>/index CHANNEL</code>\n"
                  "<code>/index CHANNEL FROM</code> (start at a message number)")
        return
    if len(args) > 3 or (args and (int(args[0]) >= 0 or any(int(a) < 1 for a in args[1:]))):
        await say("Usage: /index CHANNEL [FROM [TO]]")
        return
    if not args:
        targets = [(c, int(cps.get(f"cp_{c}") or 1), None) for c in sorted(ctx.cfg.file_channels)]
    else:
        ch = int(args[0])
        if ch not in ctx.cfg.file_channels:
            try:
                chat = await client.get_chat(ch)
                member = await client.get_chat_member(ch, "me")
                if chat.type != ChatType.CHANNEL or member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER, ChatMemberStatus.MEMBER):
                    raise ValueError("not accessible")
                await client.get_messages(ch, [1])
            except Exception:
                await say("That channel isn't in FILE_CHANNELS and access to its history could not be verified.")
                return
        first = int(args[1]) if len(args) > 1 else int(cps.get(f"cp_{ch}") or 1)
        end = int(args[2]) if len(args) > 2 else None
        if end is not None and end < first:
            await say("End must be at or after the starting message ID.")
            return
        targets = [(ch, first, end)]
    if ctx.index_job or ctx.maintenance:
        await say("Another indexing or maintenance job started. Try again later.")
        return
    ctx.index_job = {"stop": False}
    try:
        status = await say("Starting…")
    except BaseException:
        ctx.index_job = None
        raise
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


# ---------------------------------------------------------------- daily updates digest (10:00 AM Africa/Lagos)
_TRAILING_YEAR_RE = re.compile(r"(?:^|\s)((?:19|20)\d{2})$")


def lagos_cutoff_ts(d: date, hour: int = 10, minute: int = 0) -> float:
    return datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=LAGOS).timestamp()


def latest_due_cutoff(now: datetime, hour: int = 10, minute: int = 0) -> float:
    local = now.astimezone(LAGOS) if now.tzinfo else now.replace(tzinfo=LAGOS)
    today_cut = datetime(local.year, local.month, local.day, hour, minute, 0, tzinfo=LAGOS)
    if local < today_cut:
        today_cut = today_cut - timedelta(days=1)
    return today_cut.timestamp()


def _query_title_and_year(raw_q: str) -> tuple[str, int | None]:
    norm = normalize(raw_q or "")
    m = _TRAILING_YEAR_RE.search(norm)
    if m:
        base = norm[:m.start()].strip()
        if base:
            return base, int(m.group(1))
    return norm, None


def _interested_user_count(film: dict, req_rows: list[dict], miss_rows: list[dict]) -> int:
    title = str(film.get("title") or "")
    year = film.get("year")
    aliases = film.get("aliases") or ()
    exact_keys = {normalize(title)} | {normalize(a) for a in aliases if a}
    core_keys = {core_key(k) for k in exact_keys if core_key(k)}
    exact_keys.discard("")
    users: set[int] = set()
    for row in (*req_rows, *miss_rows):
        row_users = row.get("users") or ()
        if not row_users:
            continue
        q_base, q_year = _query_title_and_year(row.get("text") or row.get("key") or "")
        if not q_base:
            continue
        if q_year is not None and year is not None and int(q_year) != int(year):
            continue
        q_core = core_key(q_base)
        if q_base in exact_keys or (q_core and q_core in core_keys):
            for u in row_users:
                if u:
                    users.add(int(u))
    return len(users)


def rank_digest_films(candidates: list[dict], req_rows: list[dict], miss_rows: list[dict], limit: int = 15) -> list[dict]:
    enriched = []
    for f in candidates:
        demand = _interested_user_count(f, req_rows, miss_rows)
        enriched.append({**f, "demand": demand})
    enriched.sort(key=lambda x: (-x["demand"], -float(x.get("ts") or 0), x["fid"]))
    return enriched[:limit]


def format_digest_messages(films: list[dict], bot_username: str, day_label: str) -> list[str]:
    if not films:
        return []
    header = f"🎬 <b>Daily Film Updates — {escape(day_label)}</b>\n\n"
    bot = (bot_username or "").lstrip("@")
    chunks: list[str] = []
    cur = header
    for idx, f in enumerate(films, 1):
        title = escape(str(f["title"]))
        year = f.get("year")
        label = f"{title} ({year})" if year else title
        url = f"https://t.me/{bot}?start=film_{f['fid']}" if bot else None
        line = f"{idx}. <a href=\"{url}\">{label}</a>\n" if url else f"{idx}. <b>{label}</b>\n"
        if len(cur) + len(line) > 3800 and cur != header:
            chunks.append(cur.rstrip())
            cur = line
        else:
            cur += line
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks


async def maybe_send_daily_digest(ctx: Ctx, client, now: datetime | None = None, force: bool = False) -> dict:
    s = await ctx.settings()
    if not force and not s.get("digest_auto"):
        return {"published": 0, "reason": "manual_mode"}     # digests are approved and sent manually from the control panel
    if not s.get("digest_channel") or (not force and not s.get("digest_enabled")):
        return {"published": 0, "reason": "disabled_or_unconfigured"}
    min_year = digest_min_year(s)
    if min_year is None:
        return {"published": 0, "reason": "year_rule_paused"}
    cur_now = now or lagos_now()
    due_cutoff = latest_due_cutoff(cur_now, 10, 0)
    last_cutoff = s.get("digest_last_cutoff")
    if last_cutoff is None:
        if not force:
            await ctx.set_setting("digest_last_cutoff", due_cutoff)
            return {"published": 0, "reason": "initialized"}
        last_cutoff = 0.0
    if not force and due_cutoff <= float(last_cutoff):
        return {"published": 0, "reason": "not_due"}
    candidates = await ctx.store.digest_candidates(float(last_cutoff), due_cutoff, min_year=min_year)
    if not candidates:
        await ctx.set_setting("digest_last_cutoff", due_cutoff)
        return {"published": 0, "reason": "no_candidates"}
    req_rows, miss_rows = await ctx.store.demand_records()
    ranked = rank_digest_films(candidates, req_rows, miss_rows, limit=digest_cap(s))
    bot_user = await _digest_bot_username(ctx, client)
    text = format_digest_post(ranked, bot_user, digest_footer(s), group_link=digest_group_link(s))
    ch = s["digest_channel"]
    while True:
        try:
            msg = await client.send_message(ch, text, parse_mode=HTML, disable_web_page_preview=True)
            break
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
    mid = int(getattr(msg, "id", 0) or getattr(msg, "message_id", 0))
    await ctx.set_setting("digest_post", {
        "channel": ch, "message_id": mid, "since": float(last_cutoff), "until": due_cutoff,
        "fids": [f["fid"] for f in ranked], "sent_at": time.time(),
    })
    await ctx.set_setting("digest_last_cutoff", due_cutoff)
    return {"published": len(ranked), "messages": 1, "films": ranked, "cutoff": due_cutoff}


# ---------------------------------------------------------------- digest control panel (manual, with preview)
DIGEST_CAP_DEFAULT = 15
DIGEST_CAP_MAX = 30
DIGEST_FOOTER_DEFAULT = "🎬 Daily Film Updates"
DIGEST_GROUP_FOOTER_DEFAULT = "🎬 Tap any title to copy, then search in our Film Group"
DIGEST_GROUP_LINK_DEFAULT = "https://t.me/+zS69fDk4SjczZDA0"
DIGEST_MIN_YEAR_DEFAULT = 2026
DIGEST_MIN_YEAR_MIN = 1900
DIGEST_MIN_YEAR_MAX = 2100
_DIGEST_PANEL_ACTS = frozenset({
    "prev", "edit", "pv", "sel", "top", "up", "down", "drop",
    "bench", "add", "refresh", "send", "skip", "skipok",
    "cap", "footer", "year", "setyr", "glink",
})


def _digest_drafts(ctx: Ctx) -> dict:
    if not hasattr(ctx, "digest_drafts"):
        ctx.digest_drafts = {}
    return ctx.digest_drafts


def digest_cap(s: dict) -> int:
    try:
        v = int(s.get("digest_cap") or DIGEST_CAP_DEFAULT)
    except (TypeError, ValueError):
        v = DIGEST_CAP_DEFAULT
    return max(1, min(DIGEST_CAP_MAX, v))


def digest_group_link(s: dict) -> str:
    val = s.get("digest_group_link")
    return DIGEST_GROUP_LINK_DEFAULT if val is None else str(val).strip()


def digest_footer(s: dict) -> str:
    f = s.get("digest_footer")
    if f is None:
        return DIGEST_GROUP_FOOTER_DEFAULT if digest_group_link(s) else DIGEST_FOOTER_DEFAULT
    return str(f)


def digest_min_year(s: dict) -> int | None:
    if "digest_min_year" not in s:
        return DIGEST_MIN_YEAR_DEFAULT
    val = s.get("digest_min_year")
    if val is None:
        return None
    try:
        y = int(val)
    except (TypeError, ValueError):
        return DIGEST_MIN_YEAR_DEFAULT
    return max(DIGEST_MIN_YEAR_MIN, min(DIGEST_MIN_YEAR_MAX, y))


def _lagos_str(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=LAGOS).strftime("%b %d, %H:%M")


async def _digest_bot_username(ctx: Ctx, client) -> str:
    return getattr(ctx, "bot_username", "") or (getattr(getattr(client, "me", None), "username", None) or "")


async def build_digest_draft(ctx: Ctx, since: float, until: float, mode: str = "new",
                             base_fids: list[str] | None = None) -> dict:
    s = await ctx.settings()
    cap = digest_cap(s)
    min_year = digest_min_year(s)
    base = [str(x) for x in (base_fids or ())]

    window_films = await ctx.store.digest_window_films(float(since), float(until), limit=500)
    by_fid: dict[str, dict] = {f["fid"]: dict(f) for f in window_films}
    if base:
        missing_base = [fid for fid in base if fid not in by_fid]
        if missing_base:
            for fid, f in (await ctx.store.get_films_by_ids(missing_base)).items():
                by_fid[fid] = dict(f)

    req_rows, miss_rows = await ctx.store.demand_records()
    for f in by_fid.values():
        f["demand"] = _interested_user_count(f, req_rows, miss_rows)

    eligible: set[str] = set()
    if min_year is not None:
        for f in window_films:
            if f.get("year") is not None and int(f["year"]) >= min_year:
                eligible.add(f["fid"])

    selected_pool = eligible | {fid for fid in base if fid in by_fid}
    entries: dict[str, dict] = {fid: by_fid[fid] for fid in selected_pool}
    ranked_ids = sorted(
        entries,
        key=lambda x: (-int(entries[x].get("demand") or 0), -float(entries[x].get("ts") or 0), x),
    )
    if mode == "edit":
        base_set = set(base)
        order = [x for x in ranked_ids if x not in base_set] + [x for x in base if x in entries]
    else:
        order = ranked_ids

    others: list[dict] = []
    other_candidates = [f for f in window_films if f["fid"] not in entries]
    other_candidates.sort(key=lambda x: (-int(by_fid[x["fid"]].get("demand") or 0), -float(x.get("ts") or 0), x["fid"]))
    for f in other_candidates[:300]:
        others.append(by_fid[f["fid"]])

    return {
        "mode": mode,
        "since": float(since),
        "until": float(until),
        "cap": cap,
        "min_year": min_year,
        "order": order[:cap],
        "dropped": order[cap:],
        "entries": entries,
        "others": others,
        "sel": 0,
    }


def format_digest_post(entries: list[dict], bot_username: str, footer: str, group_link: str = "") -> str:
    """Top film FIRST (visible in channel notification previews); footer at the bottom; no date anywhere.

    When group_link is configured, titles are wrapped in <code> for one-tap copy and the bottom footer
    text embeds the group link so users tap to copy, tap the footer to open the group, and paste to search.
    When group_link is empty, titles link directly to bot DM deep links (?start=film_<fid>).
    """
    bot = (bot_username or "").lstrip("@")
    glink = (group_link or "").strip()

    def render(max_title: int) -> str:
        blocks = []
        for idx, item in enumerate(entries, 1):
            year = item.get("year")
            title = escape(str(item["title"])[:max_title])
            label = f"{title} ({year})" if year else title
            if glink:
                head = f"{idx}. <code>{label}</code>"
            else:
                url = f"https://t.me/{bot}?start=film_{item['fid']}" if bot else None
                head = f"{idx}. <a href=\"{url}\"><b>{label}</b></a>" if url else f"{idx}. <b>{label}</b>"
            blocks.append(head)
        text = "\n\n".join(blocks)
        f = (footer or "").strip()
        if not f:
            return text
        if glink:
            return f"{text}\n\n<a href=\"{escape(glink, quote=True)}\"><b>{escape(f)}</b></a>"
        return f"{text}\n\n{escape(f)}"

    text = ""
    for mt in (180, 120, 80, 45):
        text = render(mt)
        if len(text) <= 4000:
            return text
    return text[:4000]


def digest_preview_screen(draft: dict, s: dict) -> tuple[str, list]:
    order, entries, sel = draft["order"], draft["entries"], draft.get("sel", 0)
    edit = draft["mode"] == "edit"
    min_yr = draft.get("min_year")
    yr_str = f"{min_yr}+" if min_yr is not None else "paused"
    lines = [f"<b>{'✏️ Edit posted digest' if edit else '📝 Digest preview'}</b> · {len(order)}/{draft['cap']} films", ""]
    lines.append(f"Window: {_lagos_str(draft['since'])} → now ({_lagos_str(draft['until'])} Lagos) · Year rule: <b>{yr_str}</b>")
    if edit:
        lines.append("New films are marked 🆕. Updating the post moves the cutoff to now: anything indexed after it goes in the next digest.")
    lines.append("")
    posted = set(draft.get("posted") or ())
    for i, fid in enumerate(order):
        e = entries[fid]
        yr = f" ({e['year']})" if e.get("year") else ""
        mark = "▶ " if i == sel else "   "
        new = " 🆕" if edit and fid not in posted else ""
        dem = int(e.get("demand") or 0)
        dem_tag = f" · 🔥{dem}" if dem > 0 else ""
        t_limit = 38 if len(order) > 10 else 65
        lines.append(f"{mark}<b>{i + 1}.</b> {escape(str(e['title']))[:t_limit]}{yr}{new}{dem_tag}")
    if not order:
        lines.append("<i>Nothing selected. Use ➕ to add films.</i>")
    footer = digest_footer(s).strip()
    glink = digest_group_link(s)
    if glink:
        f_line = (f"<a href=\"{escape(glink, quote=True)}\">{escape(footer)}</a> (one-tap copy titles)"
                  if footer else "<i>none</i> (one-tap copy titles)")
    else:
        f_line = f"{escape(footer) if footer else '<i>none</i>'} (bot DM deep links)"
    lines += ["", f"Footer (bottom of post): {f_line}"]
    rows: list = []
    nums = [Btn(("•" if i == sel else "") + str(i + 1), f"dg:sel:{i}") for i in range(len(order))]
    for i in range(0, len(nums), 5):
        rows.append(nums[i:i + 5])
    if order:
        rows.append([Btn("⏫ Top", "dg:top"), Btn("🔼 Up", "dg:up"), Btn("🔽 Down", "dg:down"), Btn("❌ Drop", "dg:drop")])
    bench_n = len(draft.get("dropped") or ()) + len(draft.get("others") or ())
    rows.append([Btn(f"➕ Add / swap in ({bench_n})", "dg:bench:0"), Btn("♻️ Refresh", "dg:refresh")])
    rows.append([Btn("✅ Update the post" if edit else "✅ Send to channel", "dg:send")])
    rows.append([Btn("⏭ Skip this window", "dg:skip"), Btn("‹ Digest", "dg:menu")])
    text = "\n".join(lines)
    while len(text) > 3900 and len(lines) > 6:     # never cut inside an HTML tag
        lines.pop(len(lines) // 2)
        text = "\n".join(lines)
    return text, rows


def digest_bench_screen(draft: dict, page: int) -> tuple[str, list]:
    items = []
    for fid in draft.get("dropped") or ():
        e = draft["entries"].get(fid)
        if e:
            yr = f" ({e['year']})" if e.get("year") else ""
            dem = int(e.get("demand") or 0)
            items.append((fid, f"{e['title']}{yr}" + (f" · 🔥{dem}" if dem else ""), "dropped"))
    for o in draft.get("others") or ():
        yr = f" ({o['year']})" if o.get("year") else ""
        dem = int(o.get("demand") or 0)
        items.append((o["fid"], f"{o['title']}{yr}" + (f" · 🔥{dem}" if dem else ""), "other"))
    per = 8
    pages = max(1, -(-len(items) // per))
    page = max(0, min(page, pages - 1))
    full = len(draft["order"]) >= draft["cap"]
    lines = [
        "<b>➕ Add a film to the digest</b>",
        "",
        ("The digest is full: the film you pick REPLACES the selected one (▶), which moves back here." if full
         else "The film you pick is added at the TOP. Move it with the arrows afterwards."),
        "Films flagged ↩ were dropped or benched from this digest; the others are newly indexed films outside the year rule.",
        "",
    ]
    rows = []
    for fid, label, kind in items[page * per:(page + 1) * per]:
        rows.append([Btn(("↩ " if kind == "dropped" else "") + label[:52], f"dg:add:{fid}")])
    if not items:
        lines.append("<i>No other films were indexed in this window.</i>")
    nav = []
    if page > 0:
        nav.append(Btn("‹ Prev", f"dg:bench:{page - 1}"))
    if page < pages - 1:
        nav.append(Btn("Next ›", f"dg:bench:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn("‹ Back to preview", "dg:pv")])
    lines.append(f"Page {page + 1}/{pages}")
    return "\n".join(lines), rows


async def digest_publish(ctx: Ctx, client, draft: dict) -> tuple[bool, str]:
    """Send new digest or edit last post. Moves digest_last_cutoff to draft['until'] either way."""
    s = await ctx.settings()
    ch = s.get("digest_channel")
    if not ch:
        return False, "Set an update channel first."
    order = [x for x in draft["order"] if x in draft["entries"]]
    if not order:
        return False, "Nothing selected: add at least one film."
    text = format_digest_post(
        [draft["entries"][x] for x in order],
        await _digest_bot_username(ctx, client),
        digest_footer(s),
        group_link=digest_group_link(s),
    )
    post = s.get("digest_post") or {}
    try:
        if draft["mode"] == "edit":
            if not post.get("message_id"):
                return False, "There is no earlier post to edit."
            try:
                await client.edit_message_text(
                    post.get("channel") or ch,
                    int(post["message_id"]),
                    text,
                    parse_mode=HTML,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                if "MESSAGE_NOT_MODIFIED" not in str(e).upper() and e.__class__.__name__ != "MessageNotModified":
                    raise
            new_post = {**post, "fids": order, "until": draft["until"], "edited_at": time.time()}
        else:
            while True:
                try:
                    msg = await client.send_message(ch, text, parse_mode=HTML, disable_web_page_preview=True)
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
            new_post = {
                "channel": ch,
                "message_id": int(getattr(msg, "id", 0) or getattr(msg, "message_id", 0)),
                "since": draft["since"],
                "until": draft["until"],
                "fids": order,
                "sent_at": time.time(),
            }
    except Exception as e:
        return False, f"Telegram refused it: {str(e)[:160]}"
    await ctx.set_setting("digest_post", new_post)
    await ctx.set_setting("digest_last_cutoff", draft["until"])
    return True, f"{len(order)} film{'s' if len(order) != 1 else ''}"


async def digest_apply_text_setting(ctx: Ctx, action: str, text: str) -> str:
    text = (text or "").strip()
    if action == "cap":
        if not text.isdigit() or not (1 <= int(text) <= DIGEST_CAP_MAX):
            return f"Please send a number from 1 to {DIGEST_CAP_MAX}."
        await ctx.set_setting("digest_cap", int(text))
        return f"✅ The digest now holds at most {int(text)} films."
    if action == "footer":
        if text.lower() in ("none", "off", "-", "clear"):
            await ctx.set_setting("digest_footer", "")
            return "✅ No footer will be added to the post."
        await ctx.set_setting("digest_footer", text[:120])
        return "✅ Footer saved. It is placed at the bottom of the post."
    if action == "year":
        if text.lower() in ("off", "stop", "pause", "none", "clear", "0"):
            await ctx.set_setting("digest_min_year", None)
            return "✅ Release year rule paused. No films will be auto-selected until you set a year again (you can still pick films manually from the bench)."
        clean = text.rstrip("+").strip()
        if not clean.isdigit() or not (DIGEST_MIN_YEAR_MIN <= int(clean) <= DIGEST_MIN_YEAR_MAX):
            return f"Please send a 4-digit year from {DIGEST_MIN_YEAR_MIN} to {DIGEST_MIN_YEAR_MAX} (e.g. 2026 or 2027), or send <code>off</code> to stop the rule."
        yr = int(clean)
        await ctx.set_setting("digest_min_year", yr)
        return f"✅ Release year rule set to <b>{yr}+</b> (films before {yr} are excluded from automatic selection)."
    if action == "glink":
        if text.lower() in ("none", "off", "-", "clear"):
            await ctx.set_setting("digest_group_link", "")
            return "✅ Group link cleared. Digest titles will link directly to the bot DM."
        val = text.strip()
        if val.startswith("@") and re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,}", val):
            val = "https://t.me/" + val[1:]
        elif val.startswith("t.me/"):
            val = "https://" + val
        if not (val.startswith("https://t.me/") or val.startswith("http://t.me/")):
            return "Please send a valid Telegram group link (e.g. <code>https://t.me/+...</code> or <code>@groupname</code>), or <code>off</code> to link titles to bot DM."
        await ctx.set_setting("digest_group_link", val)
        return "✅ Group link saved. Digest titles use one-tap copy and the bottom footer embeds this group link."
    return "Unknown setting."


async def digest_panel_cb(ctx: Ctx, client, cb, show, parts: list[str]) -> None:
    uid = cb.from_user.id
    act = parts[0]
    drafts = _digest_drafts(ctx)
    s = await ctx.settings()

    async def render_preview(note: str | None = None):
        d = drafts.get(uid)
        t, rows = digest_preview_screen(d, await ctx.settings())
        await show(t, rows)
        await cb.answer(note) if note else await cb.answer()

    if act == "setyr":
        val = parts[1] if len(parts) > 1 else ""
        note = await digest_apply_text_setting(ctx, "year", val)
        ctx.pending_digest_setting.pop(uid, None)
        t, rows = ui.digest_settings_screen(await ctx.settings())
        await show(note + "\n\n" + t, rows)
        await cb.answer("Year rule updated.")
        return

    if act in ("prev", "edit", "refresh"):
        now = time.time()
        old = drafts.get(uid)
        mode = old["mode"] if (act == "refresh" and old) else ("edit" if act == "edit" else "new")
        post = s.get("digest_post") or {}
        if mode == "edit":
            if not post.get("message_id"):
                await cb.answer("No digest has been sent yet, so there is nothing to edit.", show_alert=True)
                return
            since, base = float(post.get("since") or 0), list(post.get("fids") or ())
        else:
            last = s.get("digest_last_cutoff")
            since, base = (float(last) if last is not None else latest_due_cutoff(lagos_now(), 10, 0)), None
        await cb.answer("Building the preview…")
        d = await build_digest_draft(ctx, since, now, mode, base)
        d["posted"] = list(base or ())
        drafts[uid] = d
        if not d["order"]:
            min_yr = d.get("min_year")
            if min_yr is None:
                msg = "The release year rule is currently paused, so no films were auto-selected."
            elif mode == "new":
                msg = f"No {min_yr}+ films have been indexed since the last digest."
            else:
                msg = "Every film in the post was dropped."
            await show(
                f"<b>📝 Digest preview</b>\n\n{msg}\nWindow: {_lagos_str(since)} → now.\n\n"
                "You can still pick films manually from the bench, change the year rule, or skip this window.",
                [
                    [Btn(f"➕ Add a film ({len(d.get('others') or ())})", "dg:bench:0")],
                    [Btn("📅 Change / stop year rule", "dg:year")],
                    [Btn("⏭ Skip this window", "dg:skip"), Btn("‹ Digest", "dg:menu")],
                ],
            )
            return
        t, rows = digest_preview_screen(d, s)
        await show(t, rows)
        return

    d = drafts.get(uid)
    if act in ("cap", "footer", "year", "glink"):
        ctx.pending_digest_channel.discard(uid)
        ctx.pending_digest_setting[uid] = {"action": act, "at": time.monotonic()}
        if act == "cap":
            prompt = (
                f"<b>Digest size</b>\nSend the maximum number of films per digest (1–{DIGEST_CAP_MAX}). "
                f"Now: <b>{digest_cap(s)}</b>."
            )
            rows = [[Btn("Cancel", "dg:menu")]]
        elif act == "footer":
            glink = digest_group_link(s)
            extra = "\n<i>When a Group link is set, this footer text automatically links to your group.</i>" if glink else ""
            prompt = (
                "<b>Digest footer</b>\nSend the line to show at the BOTTOM of every digest post (no date is added).\n"
                f"Now: <b>{escape(digest_footer(s)) or 'none'}</b>{extra}\nSend <code>none</code> to remove it."
            )
            rows = [[Btn("Cancel", "dg:menu")]]
        elif act == "glink":
            cur_g = digest_group_link(s)
            prompt = (
                "<b>Digest target group link</b>\n"
                f"Now: <code>{escape(cur_g) if cur_g else 'none (bot DM deep links)'}</code>\n\n"
                "Send your Telegram group link (e.g. <code>https://t.me/+...</code> or <code>@groupname</code>) "
                "to use one-tap copy titles + an embedded group link in the footer.\n"
                "Send <code>off</code> to switch back to direct bot DM links."
            )
            rows = [[Btn("Cancel", "dg:menu")]]
        else:
            cur_yr = digest_min_year(s)
            cur_txt = f"<b>{cur_yr}+</b>" if cur_yr is not None else "<b>Paused (stopped)</b>"
            y0 = max(2026, lagos_now().year)
            prompt = (
                "<b>Release year rule</b>\n"
                f"Now: {cur_txt}\n\n"
                "Pick a quick option below, or send a 4-digit year (e.g. <code>2026</code> or <code>2027</code>) "
                "to include that year and newer.\n"
                "Send <code>off</code> or <code>stop</code> to pause the year rule."
            )
            rows = [
                [Btn(f"{y0}+", f"dg:setyr:{y0}"), Btn(f"{y0 + 1}+", f"dg:setyr:{y0 + 1}"), Btn(f"{y0 + 2}+", f"dg:setyr:{y0 + 2}")],
            ]
            if cur_yr is not None:
                rows.append([Btn("⏸ Stop / Pause year rule", "dg:setyr:off")])
            rows.append([Btn("Cancel", "dg:menu")])
        await show(prompt, rows)
        await cb.answer()
        return

    if act in ("skip", "skipok"):
        if not d:
            await cb.answer("This preview expired. Open it again.", show_alert=True)
            return
        if act == "skip":
            await show(
                "<b>Skip this window?</b>\n\nNothing is posted. The cutoff moves to now, so the films indexed so far will NOT appear in any digest.",
                [[Btn("Yes, skip it", "dg:skipok")], [Btn("‹ Back to preview", "dg:pv")]],
            )
            await cb.answer()
            return
        await ctx.set_setting("digest_last_cutoff", d["until"])
        drafts.pop(uid, None)
        t, rows = ui.digest_settings_screen(await ctx.settings())
        await show(t, rows)
        await cb.answer("Window skipped.")
        return

    if not d:
        await cb.answer("This preview expired. Open it again from the digest menu.", show_alert=True)
        return

    order = d["order"]
    if act == "pv":
        await render_preview()
        return
    if act == "sel":
        i = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        d["sel"] = max(0, min(i, len(order) - 1)) if order else 0
        await render_preview()
        return
    if act in ("top", "up", "down", "drop"):
        i = d.get("sel", 0)
        if not order or i >= len(order):
            await cb.answer()
            return
        if act == "top" and i > 0:
            order.insert(0, order.pop(i))
            d["sel"] = 0
        elif act == "up" and i > 0:
            order[i - 1], order[i] = order[i], order[i - 1]
            d["sel"] = i - 1
        elif act == "down" and i < len(order) - 1:
            order[i + 1], order[i] = order[i], order[i + 1]
            d["sel"] = i + 1
        elif act == "drop":
            d.setdefault("dropped", []).insert(0, order.pop(i))
            d["sel"] = max(0, min(i, len(order) - 1))
        await render_preview()
        return
    if act == "bench":
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        t, rows = digest_bench_screen(d, page)
        await show(t, rows)
        await cb.answer()
        return
    if act == "add":
        fid = parts[1] if len(parts) > 1 else ""
        if fid in order:
            await cb.answer("Already in the digest.")
            return
        e = d["entries"].get(fid)
        if e is None:
            e = next((o for o in d.get("others", []) if o["fid"] == fid), None)
            if e is None:
                await cb.answer("That film is no longer available in this window.", show_alert=True)
                return
            d["entries"][fid] = e
        if fid in d.get("dropped", []):
            d["dropped"].remove(fid)
        d["others"] = [o for o in d.get("others", []) if o["fid"] != fid]
        if len(order) >= d["cap"] and order:
            i = min(d.get("sel", 0), len(order) - 1)
            d.setdefault("dropped", []).insert(0, order[i])
            order[i] = fid
            d["sel"] = i
        else:
            order.insert(0, fid)
            d["sel"] = 0
        await render_preview("Added.")
        return
    if act == "send":
        ok, info = await digest_publish(ctx, client, d)
        if not ok:
            await cb.answer(info, show_alert=True)
            return
        drafts.pop(uid, None)
        edited = d["mode"] == "edit"
        await show(
            f"✅ <b>{'Post updated' if edited else 'Digest sent'}</b> · {info}.\n\n"
            f"Cutoff is now <b>{_lagos_str(d['until'])}</b> (Lagos). Anything indexed after this goes in the next digest.",
            [[Btn("✏️ Edit this post", "dg:edit")], [Btn("‹ Digest", "dg:menu")]],
        )
        await cb.answer("Done.")
        return
    await cb.answer()


async def stats_job(ctx: Ctx, client, tick: int = 60) -> None:
    """Counters live in memory and are written once a minute, as one small update per day."""
    while True:
        await asyncio.sleep(tick)
        try:
            await ctx.flush_stats()
            await maybe_send_summary(ctx, client)
            await maybe_send_daily_digest(ctx, client)
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
    stars = int(getattr(payment, "total_amount", 50) if payment else 50)
    name = getattr(m.from_user, "first_name", "") or str(uid)

    if stars < 100:
        days = 30
        tier_benefits = ("• Supporter badge, priority and custom multi-title requests\n"
                         "• Larger watchlist capacity\n"
                         "• Extended file availability where configured\n")
    elif stars <= 250:
        days = 60
        tier_benefits = ("• Supporter badge, priority and custom multi-title requests\n"
                         "• Larger watchlist capacity\n"
                         "• Extended file availability where configured\n")
    else:
        days = 90
        tier_benefits = ("• Supporter badge, priority and custom multi-title requests\n"
                         "• Larger watchlist capacity\n"
                         "• Extended file availability where configured\n"
                         "• File messages without the standard footer\n"
                         "• Force-join bypass while your qualifying supporter period is active\n")

    new_date = await ctx.store.donor_add(uid, days=days, name=name)
    if stars > 250:
        await ctx.store.donor_set_join_bypass(uid, new_date)
        await ctx.store.donor_set_clean_files(uid, new_date)

    text = (f"⭐ <b>Thank you for supporting Éric Rohmer!</b>\n\n"
            f"Your contribution helps cover hosting and database costs and gives me more room to expand and maintain the film library.\n\n"
            f"Your supporter access is active until <b>{new_date}</b>. Your current benefits include:\n{tier_benefits}\n"
            "Thank you for helping keep the service running. Would you like your name "
            f"(<b>{escape(name)}</b>) featured on the Supporters Wall?")
    rows = _approval_rows()
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
    add(MessageHandler(_bind(ctx, cmd_supporter), filters.command(["supporters", "request", "donationclaim"]) & filters.private))
    add(MessageHandler(_bind(ctx, cmd_start), filters.command("start") & filters.private))
    add(MessageHandler(_bind(ctx, on_admin_cmd), filters.command(ADMIN_CMDS) & filters.private & admins))
    add(MessageHandler(_bind(ctx, cmd_info), filters.command(["help", "about", "discover", "donate", "survey"])))
    add(MessageHandler(_bind(ctx, on_successful_payment), filters.successful_payment & filters.private))
    add(PreCheckoutQueryHandler(_bind(ctx, on_pre_checkout)))
    add(MessageHandler(_bind(ctx, on_receipt_media), filters.private & (filters.photo | filters.document | filters.video | filters.animation)))
    add(MessageHandler(_bind(ctx, on_text), filters.text & ~filters.regex(r"^/") & ~filters.bot
                       & (filters.private | filters.group)))
    add(MessageHandler(_bind(ctx, on_channel_file),
                       filters.channel & (filters.document | filters.video | filters.audio)))
    add(CallbackQueryHandler(_bind(ctx, on_cb)))
    add(ChatJoinRequestHandler(_bind(ctx, on_join_request)))
    add(ChatMemberUpdatedHandler(_bind(ctx, on_chat_member)))
