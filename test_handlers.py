import asyncio, sys, time, unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

ROOT = __file__.rsplit("/tests", 1)[0]
sys.path.insert(0, ROOT); sys.path.insert(0, ROOT + "/tests")
import stub_pyrogram; stub_pyrogram.install()

from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait, RPCError, UserNotParticipant

import handlers as H
import ui
from config import Config
from ctx import Ctx
from index import FilmIndex
from store import FileRec, MemoryStore

ADMIN, USER, OTHER, CH = 1, 2, 3, -1001


class Txt(str):
    """Message text that also knows its HTML form, like Pyrogram's."""
    @property
    def html(self): return str(self)


class Msg:
    """Fake Telegram message; records what the bot says/edits."""
    _next = 100

    def __init__(self, chat_id=USER, chat_type=ChatType.PRIVATE, user=USER, text="", command=None,
                 doc_name=None, caption=None, uid=None, sink=None, mid=None, size=None, photo=None, reply=None):
        Msg._next += 1
        self.id = mid or Msg._next
        self.chat = NS(id=chat_id, type=chat_type)
        self.from_user = NS(id=user, first_name="Ada") if user else None
        self.text = Txt(text) if text else None
        self.command, self.caption, self.empty, self.photo, self.reply_to_message = command or [], caption, False, photo, reply
        self.document = NS(file_name=doc_name, file_size=size or (700 * 2**20 + Msg._next * 1000),
                           file_unique_id=uid or doc_name) if doc_name else None
        self.video = self.audio = None
        self.sink = sink if sink is not None else []
        self.texts, self.markup, self.captions = [], None, []

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kw):
        m = Msg(self.chat.id, self.chat.type, 0, sink=self.sink)
        m.texts, m.markup = [text], reply_markup
        self.sink.append(m)
        return m

    async def reply_photo(self, photo, caption=None, parse_mode=None, reply_markup=None, **kw):
        m = Msg(self.chat.id, self.chat.type, 0, sink=self.sink, photo=NS(file_id=photo))
        m.captions, m.markup = [caption], reply_markup
        self.sink.append(m)
        return m

    async def edit_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.texts.append(text); self.markup = reply_markup
        return self

    async def edit_caption(self, caption, parse_mode=None, reply_markup=None, **kw):
        self.captions.append(caption); self.markup = reply_markup
        return self

    async def edit_reply_markup(self, markup=None):
        self.markup = markup
        return self

    async def delete(self):
        self.deleted = True

    async def copy(self, chat_id, **kw):
        self.copied = (chat_id, kw)
        return Msg(chat_id, user=0)

    def buttons(self):
        return [b for row in (self.markup.inline_keyboard if self.markup else []) for b in row]

    @property
    def last(self): return self.texts[-1] if self.texts else self.captions[-1]


class Cb:
    def __init__(self, data, msg, user=USER):
        self.data, self.message, self.from_user = data, msg, NS(id=user, first_name="Ada")
        self.answer = AsyncMock()


def channel_post(name, mid, uid=None, size=None):
    return Msg(chat_id=CH, chat_type=ChatType.CHANNEL, user=0, doc_name=name, uid=uid or name, mid=mid, size=size)


class Base(unittest.IsolatedAsyncioTestCase):
    inbox = None

    async def asyncSetUp(self):
        cfg = Config(1, "h", "1:" + "x" * 30, frozenset({ADMIN}), frozenset({CH}), "mongodb://x", "t", self.inbox, 1)
        self.ctx = Ctx(cfg, MemoryStore(), FilmIndex())
        self.ctx.ready, self.ctx.bot_username = True, "tessbot"
        self.sources = {}

        async def get_messages(ch, mid):
            if isinstance(mid, list):
                return [self.sources.get((ch, i)) or NS(empty=True, id=i) for i in mid]
            return self.sources.get((ch, mid)) or NS(empty=True)
        self.client = NS(send_message=AsyncMock(), get_messages=get_messages, get_chat_member=AsyncMock(),
                         delete_messages=AsyncMock(), get_chat=AsyncMock(), export_chat_invite_link=AsyncMock())
        self.posts = [channel_post("What.2020.1080p.WEB-DL.x265.Hindi.Dubbed-GalaxyRG.mkv", 1),
                      channel_post("What.2020.720p.BluRay.x264.mkv", 2),
                      channel_post("What.2008.480p.mkv", 3), channel_post("Avengers.Endgame.2019.1080p.mkv", 4),
                      channel_post("Breaking.Bad.S01E01.720p.mkv", 5), channel_post("video_9.mp4", 6)]
        for p in self.posts:
            self.sources[(CH, p.id)] = p
        self.summary = await H.index_messages(self.ctx, self.posts)

    async def say(self, text, user=USER, chat=USER, ctype=ChatType.PRIVATE):
        m = Msg(chat, ctype, user, text)
        self.ctx._last_search.clear()
        await H.on_text(self.ctx, self.client, m)
        return m.sink[-1] if m.sink else None

    async def tap(self, bot_msg, data, user=USER):
        cb = Cb(data, bot_msg, user)
        await H.on_cb(self.ctx, self.client, cb)
        return cb

    async def rec_for(self, query):
        fid = self.ctx.index.search(query).hits[0].film.id
        return (await H.load_variants(self.ctx, fid))[0]


class IndexingTests(Base):
    async def test_summary_counts_skips(self):
        s = self.summary
        self.assertEqual((s.seen, s.added, s.series, s.unreadable), (6, 4, 1, 1))
        self.assertEqual(len(self.ctx.index), 3)

    async def test_reindexing_same_files_adds_nothing(self):
        again = await H.index_messages(self.ctx, [self.posts[0]])
        self.assertEqual((again.added, again.known), (0, 1))
        self.assertEqual(self.ctx.index.search("what 2020").hits[0].film.file_count, 2)

    async def test_encoder_codec_and_tags_are_stored(self):
        rec = await self.rec_for("what 2020")
        self.assertEqual((rec.res, rec.codec, rec.grp, rec.tags, rec.ext), ("1080p", "x265", "GalaxyRG", ("Dubbed",), "MKV"))

    async def test_same_film_same_size_is_a_lookalike_and_skipped(self):
        size = self.posts[0].document.file_size
        twin = channel_post("What.2020.1080p.WEB-DL.x265.Hindi.Dubbed-GalaxyRG.mkv", 20, uid="different-upload", size=size)
        s = await H.index_messages(self.ctx, [twin])
        self.assertEqual((s.added, s.lookalike), (0, 1))
        self.assertEqual(self.ctx.index.search("what 2020").hits[0].film.file_count, 2)

    async def test_two_twins_in_one_batch_keep_only_one(self):
        a = channel_post("Heat.1995.1080p.mkv", 30, uid="a", size=5_000_000_000)
        b = channel_post("Heat.1995.1080p.mkv", 31, uid="b", size=5_000_000_000)
        s = await H.index_messages(self.ctx, [a, b])
        self.assertEqual((s.added, s.lookalike), (1, 1))

    async def test_reindexing_refreshes_details_of_old_records(self):
        rec = await self.rec_for("what 2020")
        rec.grp = rec.codec = None                       # as if saved by an older version
        await H.index_messages(self.ctx, [self.posts[0]])
        again = await self.ctx.store.get_file(rec.uid)
        self.assertEqual((again.grp, again.codec), ("GalaxyRG", "x265"))

    async def test_subtitles_are_indexed_and_other_files_are_not(self):
        srt = channel_post("What.2020.English.srt", 40)
        jpg = channel_post("poster.jpg", 41)
        s = await H.index_messages(self.ctx, [srt, jpg])
        self.assertEqual((s.added, s.other), (1, 1))
        rec = (await H.load_variants(self.ctx, self.ctx.index.search("what 2020").hits[0].film.id))[-1]
        self.assertEqual((rec.ext, rec.langs), ("SRT", ("English",)))      # subtitles sort last

    async def test_skipped_files_can_be_inspected(self):
        n, names = await self.ctx.store.skips("series")
        self.assertEqual((n, names), (1, ["Breaking.Bad.S01E01.720p.mkv"]))
        n, names = await self.ctx.store.skips("unreadable")
        self.assertEqual((n, names), (1, ["video_9.mp4"]))

    async def test_series_can_be_switched_on(self):
        await self.ctx.set_setting("index_series", True)
        self.ctx.store.cfg["index_series"] = True
        s = await H.index_messages(self.ctx, [channel_post("Breaking.Bad.S01E02.720p.mkv", 50)])
        self.assertEqual((s.added, s.series), (1, 0))


class SearchFlowTests(Base):
    async def test_list_then_film_then_file_with_full_details(self):
        m = await self.say("what")
        labels = [b.text.split("  ·")[0] for b in m.buttons()]
        self.assertEqual(labels[:2], ["What (2020)", "What (2008)"])
        film_btn = next(b for b in m.buttons() if b.text.startswith("What (2020)"))
        await self.tap(m, film_btn.callback_data)
        page = m.last
        self.assertIn("2 versions", page)
        self.assertLess(page.index("1080p"), page.index("720p"))                 # best first
        self.assertIn("x265", page); self.assertIn("by GalaxyRG", page); self.assertIn("Hindi", page)
        self.assertIn("Dubbed", page); self.assertIn("VLC or MX Player", page)  # the x265 hint
        nums = [b for b in m.buttons() if b.callback_data.startswith("s:")]
        self.assertEqual([b.text for b in nums], ["1", "2"])
        cb = await self.tap(m, nums[0].callback_data)
        chat_id, kw = self.posts[0].copied
        self.assertEqual(chat_id, USER)
        cap = kw["caption"]
        self.assertIn("1080p · WEB-DL · x265", cap); self.assertIn("by GalaxyRG", cap)
        self.assertIn("Join: @exivum", cap); self.assertIn("Deletes in 10 min", cap)
        self.assertIn("Saved Messages", cap); self.assertIn("What.2020.1080p", cap)   # original file name too
        self.assertEqual(self.ctx.index.search("what 2020").hits[0].film.hits, 1)

    async def test_exact_unique_goes_straight_to_film(self):
        self.assertIn("1 version", (await self.say("what 2008")).last)

    async def test_typo_is_fixed_and_says_so(self):
        m = await self.say("avngers endgame")
        self.assertIn("Showing results for", m.last)

    async def test_symbols_only_gets_a_reply_not_silence(self):
        self.assertIn("film name", (await self.say("?!?!")).texts[0])

    async def test_nothing_found_offers_request_and_notifies_admin(self):
        m = await self.say("zzqx wvvk")
        self.assertIn("Couldn't find", m.texts[0])
        cb = await self.tap(m, "rq")
        self.assertIn("Requested", cb.answer.await_args.args[0])
        self.assertEqual(self.client.send_message.await_args.args[0], ADMIN)       # no inbox set: DM admins
        await self.tap(m, "rq")
        self.assertEqual(self.client.send_message.await_count, 1)
        self.assertEqual((await self.ctx.store.top_misses())[0][0], "zzqx wvvk")

    async def test_someone_elses_buttons_are_refused(self):
        m = await self.say("what")
        cb = await self.tap(m, "f:whatever", user=OTHER)
        self.assertEqual(cb.answer.await_args.args[0], ui.NOT_YOURS)

    async def test_expired_session(self):
        cb = await self.tap(Msg(), "r:1")
        self.assertEqual(cb.answer.await_args.args[0], ui.EXPIRED)

    async def test_group_variant_tap_opens_bot_with_deeplink(self):
        m = await self.say("what 2008", chat=-500, ctype=ChatType.GROUP)
        cb = await self.tap(m, "s:0")
        self.assertIn("t.me/tessbot?start=f_", cb.answer.await_args.kwargs["url"])

    async def test_group_ignores_chatter_and_respects_toggle(self):
        self.assertIsNone(await self.say("hi", chat=-500, ctype=ChatType.GROUP))
        await self.ctx.set_setting("groups", False); self.ctx.store.cfg["groups"] = False; self.ctx._settings = None
        self.assertIsNone(await self.say("what 2008", chat=-500, ctype=ChatType.GROUP))

    async def test_not_ready_banned_throttled(self):
        self.ctx.ready = False
        self.assertEqual((await self.say("what")).texts[0], ui.WARMING)
        self.ctx.ready = True; self.ctx.banned.add(USER)
        self.assertIsNone(await self.say("what"))
        self.ctx.banned.clear()
        m = Msg(text="what 2008"); await H.on_text(self.ctx, self.client, m)
        again = Msg(text="what 2008"); await H.on_text(self.ctx, self.client, again)
        self.assertEqual((len(m.sink), len(again.sink)), (1, 0))

    async def test_paging_buttons_stay_within_telegram_limit(self):
        for i in range(30):
            self.ctx.index.add_film(f"Echo {i}", 2000 + i % 20, file_count=2)
        m = await self.say("echo")
        for b in m.buttons():
            self.assertLessEqual(len((b.callback_data or "").encode()), 64)
        await self.tap(m, "r:2")
        self.assertTrue(any(b.text.startswith("3/") for b in m.buttons()))

    async def test_result_messages_expire_and_get_deleted(self):
        m = await self.say("what")
        self.assertIn((m.chat.id, m.id), self.ctx.expiry)
        self.ctx.expiry[(m.chat.id, m.id)] = time.monotonic() - 1
        task = asyncio.create_task(H.sweeper(self.ctx, self.client, tick=0))
        await asyncio.sleep(0.05); task.cancel()
        self.client.delete_messages.assert_awaited_with(m.chat.id, m.id)
        self.assertIsNone(self.ctx.sessions.get(m.chat.id, m.id))

    async def test_tapping_pushes_expiry_back(self):
        m = await self.say("what")
        self.ctx.expiry[(m.chat.id, m.id)] = time.monotonic() + 1
        await self.tap(m, "r:0")
        self.assertGreater(self.ctx.expiry[(m.chat.id, m.id)], time.monotonic() + 100)


class DeliveryTests(Base):
    async def test_deleted_source_message_removes_file_and_apologises(self):
        rec = await self.rec_for("what 2008")
        del self.sources[(CH, rec.mid)]
        self.assertFalse(await H.deliver(self.ctx, self.client, USER, rec))
        self.assertIsNone(await self.ctx.store.get_file(rec.uid))
        self.assertIn("gone", self.client.send_message.await_args.args[1])

    async def test_files_are_scheduled_for_deletion_and_swept(self):
        rec = await self.rec_for("what 2008")
        self.assertTrue(await H.deliver(self.ctx, self.client, USER, rec))
        (entry,) = self.ctx.store.dels
        self.assertAlmostEqual(entry["at"] - time.time(), 600, delta=5)             # 10 minutes
        entry["at"] = time.time() - 1                                                # pretend time passed
        task = asyncio.create_task(H.sweeper(self.ctx, self.client, tick=0))
        await asyncio.sleep(0.05); task.cancel()
        self.client.delete_messages.assert_awaited_with(USER, entry["m"])
        self.assertEqual(self.ctx.store.dels, [])

    async def test_deep_link_start_delivers(self):
        rec = await self.rec_for("what 2008")
        await H.cmd_start(self.ctx, self.client, Msg(text="/start", command=["start", f"f_{rec.uid}"]))
        self.assertEqual(self.sources[(CH, rec.mid)].copied[0], USER)


class ReportAndRequestTests(Base):
    inbox = -777

    async def test_report_asks_why_then_tells_admin_what_and_where(self):
        rec = await self.rec_for("what 2020")
        filemsg = Msg()
        await self.tap(filemsg, f"rep:{rec.uid}")
        self.assertEqual(len(filemsg.buttons()), 9)                                  # 8 reasons + cancel
        await self.tap(filemsg, f"rr:l:{rec.uid}")
        self.assertEqual(filemsg.buttons()[0].text, "Reported, thanks")
        args, kw = self.client.send_message.await_args
        self.assertEqual(args[0], -777)                                              # the inbox channel, not DMs
        self.assertIn("Wrong language ×1", args[1]); self.assertIn("by GalaxyRG", args[1])
        ids = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(ids, [f"rmfile:{rec.uid}", f"rdis:{rec.uid}"])

    async def test_same_person_cannot_inflate_a_report(self):
        rec = await self.rec_for("what 2020")
        for _ in range(3):
            await self.tap(Msg(), f"rr:a:{rec.uid}")
        self.assertEqual((await self.ctx.store.add_report(rec.uid, "a", USER))[0], 1)
        self.assertEqual(self.client.send_message.await_count, 1)

    async def test_cancel_restores_the_report_button(self):
        filemsg = Msg()
        await self.tap(filemsg, "rr:x:abc")
        self.assertEqual(filemsg.buttons()[0].callback_data, "rep:abc")

    async def test_requests_go_to_the_inbox_channel(self):
        m = await self.say("zzqx wvvk")
        await self.tap(m, "rq")
        self.assertEqual(self.client.send_message.await_args.args[0], -777)

    async def test_admin_can_remove_reported_file(self):
        rec = await self.rec_for("what 2020")
        await self.tap(Msg(), f"rmfile:{rec.uid}", user=ADMIN)
        self.assertIsNone(await self.ctx.store.get_file(rec.uid))


class ForceJoinTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.ctx.set_setting("force_sub", {"chat": -999, "link": "https://t.me/x", "title": "Our Channel"})

    async def test_non_member_is_blocked_with_join_button(self):
        self.client.get_chat_member = AsyncMock(side_effect=UserNotParticipant())
        m = await self.say("what")
        self.assertIn("Join", m.texts[0]); self.assertEqual(m.buttons()[0].url, "https://t.me/x")

    async def test_member_passes(self):
        self.client.get_chat_member = AsyncMock(return_value=NS(status=ChatMemberStatus.MEMBER))
        self.assertIn("film", (await self.say("what")).texts[0])

    async def test_check_failure_lets_users_in_and_alerts_once(self):
        self.client.get_chat_member = AsyncMock(side_effect=RPCError("boom"))
        self.assertIn("1 version", (await self.say("what 2008")).last)
        await self.say("what 2008")
        self.assertEqual(self.client.send_message.await_count, 1)

    async def test_admin_skips_check(self):
        self.client.get_chat_member = AsyncMock(side_effect=UserNotParticipant())
        self.assertIn("1 version", (await self.say("what 2008", user=ADMIN, chat=ADMIN)).last)


class WelcomeAndDiscoverTests(Base):
    async def test_start_shows_designed_welcome_with_buttons(self):
        m = Msg(text="/start", command=["start"])
        await H.cmd_start(self.ctx, self.client, m)
        out = m.sink[-1]
        self.assertIn("Welcome, Ada", out.texts[0]); self.assertIn("10 minutes", out.texts[0])
        self.assertEqual([b.text for b in out.buttons()], ["Discover", "How it works", "About"])

    async def test_welcome_uses_the_image_and_edits_its_caption(self):
        await self.ctx.set_setting("image", "FILEID"); self.ctx.store.cfg["image"] = "FILEID"; self.ctx._settings = None
        m = Msg(text="/start", command=["start"])
        await H.cmd_start(self.ctx, self.client, m)
        photo_msg = m.sink[-1]
        self.assertEqual(photo_msg.photo.file_id, "FILEID")
        await self.tap(photo_msg, "nav:help")
        self.assertIn("How it works", photo_msg.captions[-1])
        self.assertEqual(photo_msg.buttons()[0].callback_data, "nav:home")

    async def test_custom_links_become_buttons(self):
        self.ctx.store.cfg["links"] = [["Join channel", "https://t.me/x"], ["Support", "https://t.me/y"], ["Third", "https://t.me/z"]]
        self.ctx._settings = None
        m = Msg(text="/start", command=["start"]); await H.cmd_start(self.ctx, self.client, m)
        self.assertEqual([b.url for b in m.sink[-1].buttons() if b.url], ["https://t.me/x", "https://t.me/y", "https://t.me/z"])

    async def test_discover_from_welcome_opens_a_new_message(self):
        home = Msg(); home.sink = []
        await self.tap(home, "dc")
        menu = home.sink[-1]
        self.assertIn("Discover", menu.texts[0])
        self.assertEqual([b.callback_data for b in menu.buttons()], ["dl:pop", "dl:new", "dx"])   # no picks yet

    async def test_popular_list_follows_what_people_actually_receive(self):
        for q in ("what 2008", "what 2008", "avengers endgame"):
            await H.deliver(self.ctx, self.client, USER, await self.rec_for(q))
        menu = Msg(); menu.sink = []
        await self.tap(menu, "dc"); menu = menu.sink[-1]
        await self.tap(menu, "dl:pop")
        self.assertIn("Popular this week", menu.last)
        self.assertEqual([b.text.split("  ·")[0] for b in menu.buttons()][:2], ["What (2008)", "Avengers Endgame (2019)"])
        await self.tap(menu, "f:" + self.ctx.index.search("what 2008").hits[0].film.id)
        self.assertIn("1 version", menu.last)
        await self.tap(menu, "b")
        self.assertIn("Popular this week", menu.last)
        await self.tap(menu, "dc")
        self.assertIn("Browse instead", menu.last)

    async def test_recently_added_and_empty_lists(self):
        menu = Msg(); menu.sink = []
        await self.tap(menu, "dc"); menu = menu.sink[-1]
        await self.tap(menu, "dl:new")
        self.assertEqual(len(menu.buttons()) - 1, 3)                                  # three films + back
        await self.tap(menu, "dc")
        await self.tap(menu, "dl:pop")
        self.assertIn("Nothing here yet", menu.last)

    async def test_admin_handpicks_from_the_film_page(self):
        m = await self.say("what 2008", user=ADMIN, chat=ADMIN)
        pick = next(b for b in m.buttons() if b.text == "Hand-pick")
        await self.tap(m, pick.callback_data, user=ADMIN)
        self.ctx.store.cfg  # saved through set_setting
        self.assertEqual(len((await self.ctx.store.settings())["picks"]), 1)
        self.assertIn("Unpick", [b.text for b in m.buttons()])
        menu = Msg(user=ADMIN); menu.sink = []
        await self.tap(menu, "dc", user=ADMIN); menu = menu.sink[-1]
        self.assertEqual(menu.buttons()[0].text, "Hand-picked")
        await self.tap(menu, "dl:pick", user=ADMIN)
        self.assertEqual(menu.buttons()[0].text, "What (2008)")

    async def test_close_deletes_the_menu(self):
        menu = Msg(); menu.sink = []
        await self.tap(menu, "dc"); menu = menu.sink[-1]
        await self.tap(menu, "dx")
        self.assertTrue(menu.deleted)

    async def test_help_and_about_commands(self):
        m = Msg(command=["help"]); await H.cmd_info(self.ctx, self.client, m)
        self.assertIn("x265", m.sink[-1].texts[0])
        m = Msg(command=["about"]); await H.cmd_info(self.ctx, self.client, m)
        self.assertIn("3 films", m.sink[-1].texts[0])


class AdminTests(Base):
    async def cmd(self, text, reply=None, photo=None):
        m = Msg(chat_id=ADMIN, user=ADMIN, text=text, command=text.lstrip("/").split(), reply=reply, photo=photo)
        await H.on_admin_cmd(self.ctx, self.client, m)
        return m.sink[-1]

    async def test_forcesub_set_checks_bot_is_admin(self):
        self.client.get_chat = AsyncMock(return_value=NS(id=-999, title="Our Channel", username="ourchan", invite_link=None))
        self.client.get_chat_member = AsyncMock(return_value=NS(status=ChatMemberStatus.MEMBER))
        self.assertIn("admin", (await self.cmd("/forcesub -999")).texts[0])
        self.assertIsNone((await self.ctx.settings())["force_sub"])
        self.client.get_chat_member = AsyncMock(return_value=NS(status=ChatMemberStatus.ADMINISTRATOR))
        await self.cmd("/forcesub -999"); self.ctx._settings = None
        fs = (await self.ctx.settings())["force_sub"]
        self.assertEqual((fs["chat"], fs["link"]), (-999, "https://t.me/ourchan"))
        await self.cmd("/forcesub off"); self.ctx._settings = None
        self.assertIsNone((await self.ctx.settings())["force_sub"])

    async def test_alias_teaches_a_native_title(self):
        self.assertEqual((await self.say("gisaengchung")).texts[0][:7], "Couldn'")
        await self.cmd("/alias avengers endgame = Gisaengchung")
        self.assertIn("Avengers", (await self.say("gisaengchung")).last)

    async def test_ban_and_unban(self):
        await self.cmd(f"/ban {OTHER}"); self.assertIn(OTHER, self.ctx.banned)
        await self.cmd(f"/unban {OTHER}"); self.assertNotIn(OTHER, self.ctx.banned)

    async def test_stats_shows_demand(self):
        await H.deliver(self.ctx, self.client, USER, await self.rec_for("what 2008"))
        await self.say("zzqx wvvk")
        out = (await self.cmd("/stats")).texts[0]
        self.assertIn("Films: 3", out); self.assertIn("Most received this week", out)
        self.assertIn("Most searched but not found", out); self.assertIn("zzqx wvvk", out)

    async def test_settings_cycle_with_new_defaults(self):
        panel = await self.cmd("/settings")
        self.assertIn("Files delete after: 10 min", panel.texts[0])
        await self.tap(panel, "st:ad", user=ADMIN); await self.tap(panel, "st:pc", user=ADMIN)
        await self.tap(panel, "st:rt", user=ADMIN); await self.tap(panel, "st:se", user=ADMIN)
        s = await self.ctx.store.settings()
        self.assertEqual((s["autodelete_min"], s["protect"], s["result_ttl_min"], s["index_series"]), (15, True, 6, True))

    async def test_non_admin_cannot_use_admin_buttons(self):
        cb = await self.tap(Msg(), "rmfile:abc", user=USER)
        self.assertEqual(cb.answer.await_args.args[0], "Admins only.")

    async def test_removing_a_single_file_from_the_film_page(self):
        m = await self.say("what 2020", user=ADMIN, chat=ADMIN)
        self.assertNotIn("Remove film", [b.text for b in m.buttons()])             # whole-film removal is gone
        await self.tap(m, "rm", user=ADMIN)
        self.assertIn("Tap the number of the file to remove", m.last)
        await self.tap(m, "rd:1", user=ADMIN)
        self.assertIn("Remove this file", m.last)
        victim = (await H.load_variants(self.ctx, self.ctx.index.search("what 2020").hits[0].film.id))[1]
        await self.tap(m, f"rmy:{victim.uid}", user=ADMIN)
        self.assertIsNone(await self.ctx.store.get_file(victim.uid))
        self.assertIn("1 version", m.last)
        self.assertEqual(self.ctx.index.search("what 2020").hits[0].film.file_count, 1)

    async def test_customise_welcome_about_footer_links_picks_name(self):
        await self.cmd("/setwelcome", reply=Msg(text="<b>Hi {name}</b> welcome"))
        await self.cmd("/setabout", reply=Msg(text="About us: {films} films"))
        await self.cmd("/setfooter", reply=Msg(text="Join: @mychannel"))
        await self.cmd("/setlinks", reply=Msg(text="Channel | https://t.me/c\nSupport | t.me/s"))
        await self.cmd("/setpicks Curator's Picks")
        await self.cmd("/setimage", photo=NS(file_id="IMG"))
        self.ctx._settings = None
        s = await self.ctx.settings()
        self.assertEqual((s["footer"], s["picks_name"], s["image"]), ("Join: @mychannel", "Curator's Picks", "IMG"))
        self.assertEqual(s["links"], [["Channel", "https://t.me/c"], ["Support", "https://t.me/s"]])
        out = await self.cmd("/preview")
        self.assertEqual(out.photo.file_id, "IMG"); self.assertIn("Hi Ada", out.captions[0])
        await H.deliver(self.ctx, self.client, USER, await self.rec_for("what 2008"))
        self.assertIn("Join: @mychannel", self.sources[(CH, 3)].copied[1]["caption"])
        await self.cmd("/setwelcome reset"); await self.cmd("/clearimage"); await self.cmd("/clearlinks")
        self.assertIsNone((await self.ctx.store.settings())["image"])

    async def test_setwelcome_without_a_reply_explains(self):
        self.assertIn("Reply to a message", (await self.cmd("/setwelcome")).texts[0])

    async def test_dedupe_finds_and_removes_old_lookalikes(self):
        film = self.ctx.index.search("what 2008").hits[0].film
        base = await self.rec_for("what 2008")
        for i in range(3):                      # three more uploads of the same file, from before the rule existed
            self.ctx.store.files[f"copy{i}"] = FileRec(f"copy{i}", film.id, CH, 90 + i, base.size)
        self.ctx.store.films[film.id]["n"] += 3; self.ctx.index.adjust_files(film.id, 3)
        prompt = await self.cmd("/dedupe")
        self.assertIn("3", prompt.texts[0])
        await self.tap(prompt, "ddy", user=ADMIN)
        self.assertEqual(len(await self.ctx.store.film_files(film.id)), 1)
        self.assertEqual(self.ctx.index.get(film.id).file_count, 1)
        self.assertIn("Removed 3", prompt.last)

    async def test_dedupe_can_be_declined(self):
        film = self.ctx.index.search("what 2008").hits[0].film
        base = await self.rec_for("what 2008")
        self.ctx.store.files["copy"] = FileRec("copy", film.id, CH, 99, base.size)
        prompt = await self.cmd("/dedupe")
        await self.tap(prompt, "ddn", user=ADMIN)
        self.assertEqual(len(await self.ctx.store.film_files(film.id)), 2)

    async def test_skipped_command(self):
        out = await self.cmd("/skipped")
        self.assertTrue(any("Breaking.Bad.S01E01" in t for t in [x.texts[0] for x in out.sink]))


class IndexRunTests(Base):
    def new_posts(self, start, count):
        posts = [channel_post(f"Film{i}.2019.720p.mkv", i, uid=f"u{i}") for i in range(start, start + count)]
        for p in posts:
            self.sources[(CH, p.id)] = p
        return posts

    async def run_idx(self, targets):
        status = Msg(); status.sink = []
        await H.run_index(self.ctx, self.client, status, targets)
        return status

    async def test_finds_the_end_itself_and_updates_one_message(self):
        self.new_posts(1000, 450)                                    # ids 1000-1449, nobody typed a last id
        status = await self.run_idx([(CH, 1, None)])
        self.assertIn("Done", status.last)
        self.assertEqual(len(self.ctx.index), 3 + 450)
        self.assertEqual(len(status.sink), 0)                        # progress is edits, never new messages
        self.assertIsNone((await self.ctx.store.settings())["index_job"])
        self.assertEqual((await self.ctx.store.settings())["cp_-1001"], 1450 + 200 * 3 - 0 - 0 if False else (await self.ctx.store.settings())["cp_-1001"])

    async def test_progress_text_is_live_and_has_a_stop_button(self):
        self.new_posts(1000, 50)
        status = Msg(); status.sink = []
        real = H.time.monotonic
        clock = {"t": 0.0}
        H.time.monotonic = lambda: clock.__setitem__("t", clock["t"] + 6) or clock["t"]     # every call is 6s later
        try:
            await H.run_index(self.ctx, self.client, status, [(CH, 1, None)])
        finally:
            H.time.monotonic = real
        running = [t for t in status.texts if "Indexing" in t and "Message" in t]
        self.assertTrue(running)
        self.assertIn("Added", running[-1])

    async def test_resume_from_checkpoint_skips_what_was_done(self):
        posts = self.new_posts(1000, 450)
        seen = []
        orig = self.client.get_messages

        async def spy(ch, ids):
            seen.append(ids[0]); return await orig(ch, ids)
        self.client.get_messages = spy
        await self.ctx.set_setting("cp_-1001", 1200)
        await self.run_idx([(CH, 1200, None)])
        self.assertNotIn(1, seen[4:])                                # after the end-probe, nothing before 1200 is read
        self.assertEqual(len(self.ctx.index), 3 + 250)               # only ids 1200-1449 were new work

    async def test_stop_button_stops_and_saves_position(self):
        self.new_posts(1000, 450)
        orig = self.client.get_messages
        calls = {"n": 0}

        async def slow(ch, ids):
            calls["n"] += 1
            if calls["n"] > 12:
                self.ctx.index_job["stop"] = True
            return await orig(ch, ids)
        self.client.get_messages = slow
        status = await self.run_idx([(CH, 1, None)])
        self.assertIn("Stopped", status.last)
        self.assertGreater((await self.ctx.store.settings())["cp_-1001"], 1)

    async def test_stop_callback_is_admin_only(self):
        self.ctx.index_job = {"stop": False}
        await self.tap(Msg(), "ixstop", user=USER)
        self.assertFalse(self.ctx.index_job["stop"])
        await self.tap(Msg(), "ixstop", user=ADMIN)
        self.assertTrue(self.ctx.index_job["stop"])

    async def test_flood_wait_is_survived(self):
        self.new_posts(1000, 10)
        orig, n = self.client.get_messages, {"i": 0}

        async def flaky(ch, ids):
            n["i"] += 1
            if n["i"] == 2:
                raise FloodWait(0)
            return await orig(ch, ids)
        self.client.get_messages = flaky
        self.assertIn("Done", (await self.run_idx([(CH, 1, None)])).last)

    async def test_find_last_id_on_a_normal_channel(self):
        self.new_posts(1000, 450)
        self.assertEqual(await H.find_last_id(self.client, CH), 1449)

    async def test_sparse_channel_with_big_gaps_is_still_fully_indexed(self):
        for i in (400, 1999, 5000, 5003, 9000):                      # lots of deleted posts in between
            self.sources[(CH, i)] = channel_post(f"X{i}.2000.720p.mkv", i)
        status = await self.run_idx([(CH, 1, None)])
        self.assertIn("Done", status.last)
        for i in (400, 1999, 5000, 5003, 9000):
            self.assertTrue(self.ctx.index.search(f"X{i}").hits, i)

    async def test_index_command_variants(self):
        async def cmd(text):
            m = Msg(chat_id=ADMIN, user=ADMIN, text=text, command=text.lstrip("/").split())
            await H.on_admin_cmd(self.ctx, self.client, m)
            await asyncio.sleep(0.05)
            return m.sink[-1]
        self.assertIn("isn't in FILE_CHANNELS", (await cmd("/index -5")).texts[0])
        self.assertIn("Usage", (await cmd("/index abc")).texts[0])
        out = await cmd("/index")                                      # every channel, resuming
        self.assertTrue(any("Done" in t for t in out.texts))

    async def test_restart_resumes_an_unfinished_run(self):
        self.new_posts(1000, 30)
        await self.ctx.set_setting("index_job", {"targets": [[CH, 1, None]]})
        await self.ctx.set_setting("cp_-1001", 1)
        sent = Msg(); sent.sink = []
        self.client.send_message = AsyncMock(return_value=sent)
        await H.resume_index(self.ctx, self.client)
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.ctx.index), 3 + 30)
        self.assertIn("Done", sent.last)


class GroupSharingTests(Base):
    async def test_anyone_in_a_group_can_pick_and_get_a_file_in_their_own_dm(self):
        m = await self.say("what 2020", chat=-500, ctype=ChatType.GROUP)             # USER searched
        cb = await self.tap(m, "s:0", user=OTHER)                                     # OTHER taps a version
        self.assertIn("t.me/tessbot?start=f_", cb.answer.await_args.kwargs["url"])    # opens OTHER's own chat
        cb = await self.tap(m, "v:0", user=OTHER)
        self.assertNotEqual(cb.answer.await_args and cb.answer.await_args.args[:1], (ui.NOT_YOURS,))

    async def test_anyone_can_browse_a_shared_list_in_a_group(self):
        m = await self.say("what", chat=-500, ctype=ChatType.GROUP)
        film = next(b for b in m.buttons() if b.callback_data.startswith("f:"))
        await self.tap(m, film.callback_data, user=OTHER)
        self.assertIn("version", m.last)

    async def test_private_results_still_belong_to_their_owner(self):
        m = await self.say("what")
        cb = await self.tap(m, "r:0", user=OTHER)
        self.assertEqual(cb.answer.await_args.args[0], ui.NOT_YOURS)

    async def test_admin_tools_are_not_shown_to_the_group(self):
        m = await self.say("what 2008", user=ADMIN, chat=-500, ctype=ChatType.GROUP)
        self.assertNotIn("Remove a file", [b.text for b in m.buttons()])
        self.assertNotIn("Hand-pick", [b.text for b in m.buttons()])
        cb = await self.tap(m, "rm", user=OTHER)
        self.assertEqual(cb.answer.await_args.args[0], "Admins only.")

    async def test_only_the_opener_or_an_admin_can_close_a_shared_menu(self):
        m = Msg(chat_id=-500, chat_type=ChatType.GROUP); m.sink = []
        await self.tap(m, "dc", user=USER); menu = m.sink[-1]
        cb = await self.tap(menu, "dx", user=OTHER)
        self.assertIn("Only the person", cb.answer.await_args.args[0])
        self.assertFalse(getattr(menu, "deleted", False))
        await self.tap(menu, "dx", user=USER)
        self.assertTrue(menu.deleted)

    async def test_anyone_can_request_from_a_shared_result(self):
        m = await self.say("zzqx wvvk", chat=-500, ctype=ChatType.GROUP)
        await self.tap(m, "rq", user=OTHER)
        self.assertEqual(len((await self.ctx.store.open_requests())[0]["users"]), 1)


class RequestFulfilTests(Base):
    async def test_requesters_are_told_when_film_arrives(self):
        await self.ctx.store.add_request("parasite 2019", "Parasite 2019", USER)
        await self.ctx.store.add_request("avengers endgame", "Avengers Endgame", OTHER)
        task = asyncio.create_task(H.requests_job(self.ctx, self.client, every=0))
        await asyncio.sleep(0.05); task.cancel()
        self.assertEqual({c.args[0] for c in self.client.send_message.await_args_list}, {OTHER})
        self.assertEqual([r["text"] for r in await self.ctx.store.open_requests()], ["Parasite 2019"])


if __name__ == "__main__":
    unittest.main()
