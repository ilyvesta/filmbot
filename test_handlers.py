import asyncio, sys, unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

ROOT = __file__.rsplit("/tests", 1)[0]
sys.path.insert(0, ROOT); sys.path.insert(0, ROOT + "/tests")
import stub_pyrogram; stub_pyrogram.install()

from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait, RPCError, UserNotParticipant

import handlers as H
from config import Config
from ctx import Ctx
from index import FilmIndex
from store import MemoryStore

ADMIN, USER, OTHER, CH = 1, 2, 3, -1001


class Msg:
    """Fake Telegram message; records what the bot says/edits."""
    _next = 100

    def __init__(self, chat_id=USER, chat_type=ChatType.PRIVATE, user=USER, text="", command=None,
                 doc_name=None, caption=None, uid=None, sink=None, mid=None):
        Msg._next += 1
        self.id = mid or Msg._next
        self.chat = NS(id=chat_id, type=chat_type)
        self.from_user = NS(id=user) if user else None
        self.text, self.command, self.caption, self.empty = text, command or [], caption, False
        self.document = NS(file_name=doc_name, file_size=700 * 2**20, file_unique_id=uid or doc_name) if doc_name else None
        self.video = self.audio = None
        self.sink = sink if sink is not None else []
        self.texts, self.markup = [], None

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kw):
        m = Msg(self.chat.id, self.chat.type, 0, sink=self.sink)
        m.texts, m.markup = [text], reply_markup
        self.sink.append(m)
        return m

    async def edit_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.texts.append(text); self.markup = reply_markup
        return self

    async def copy(self, chat_id, **kw):
        self.copied = (chat_id, kw)
        return Msg(chat_id, user=0)

    def buttons(self):
        return [b for row in (self.markup.inline_keyboard if self.markup else []) for b in row]

    @property
    def last(self): return self.texts[-1]


class Cb:
    def __init__(self, data, msg, user=USER):
        self.data, self.message, self.from_user = data, msg, NS(id=user)
        self.answer = AsyncMock()


def channel_post(name, mid, uid=None):
    return Msg(chat_id=CH, chat_type=ChatType.CHANNEL, user=0, doc_name=name, uid=uid or name, mid=mid)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = Config(1, "h", "1:" + "x" * 30, frozenset({ADMIN}), frozenset({CH}), "mongodb://x", "t", None, 1)
        self.ctx = Ctx(cfg, MemoryStore(), FilmIndex())
        self.ctx.ready, self.ctx.bot_username = True, "tessbot"
        self.client = NS(send_message=AsyncMock(), get_messages=AsyncMock(), get_chat_member=AsyncMock(),
                         delete_messages=AsyncMock(), get_chat=AsyncMock(), export_chat_invite_link=AsyncMock())
        self.sources = {}
        async def get_messages(ch, mid):
            return self.sources.get((ch, mid)) or NS(empty=True)
        self.client.get_messages = get_messages
        posts = [channel_post("What.2020.1080p.WEB-DL.Hindi.mkv", 1), channel_post("What.2020.720p.BluRay.mkv", 2),
                 channel_post("What.2008.480p.mkv", 3), channel_post("Avengers.Endgame.2019.1080p.mkv", 4),
                 channel_post("Breaking.Bad.S01E01.720p.mkv", 5), channel_post("video_9.mp4", 6)]
        for p in posts:
            self.sources[(CH, p.id)] = p
        self.summary = await H.index_messages(self.ctx, posts)

    async def say(self, text, user=USER, chat=USER, ctype=ChatType.PRIVATE):
        m = Msg(chat, ctype, user, text)
        await H.on_text(self.ctx, self.client, m)
        return m.sink[-1] if m.sink else None

    async def tap(self, bot_msg, data, user=USER):
        cb = Cb(data, bot_msg, user)
        await H.on_cb(self.ctx, self.client, cb)
        return cb


class IndexingTests(Base):
    async def test_summary_counts_skips(self):
        s = self.summary
        self.assertEqual((s.seen, s.added, s.series, s.unreadable), (6, 4, 1, 1))
        self.assertEqual(len(self.ctx.index), 3)           # What 2020, What 2008, Avengers Endgame

    async def test_reindexing_same_files_adds_nothing(self):
        again = await H.index_messages(self.ctx, [self.sources[(CH, 1)]])
        self.assertEqual((again.added, again.dupes), (0, 1))
        self.assertEqual(self.ctx.index.search("what 2020").hits[0].film.file_count, 2)


class SearchFlowTests(Base):
    async def test_ambiguous_title_shows_list_then_film_then_sends_file(self):
        m = await self.say("what")
        labels = [b.text.split("  ·")[0] for b in m.buttons()]
        self.assertEqual(labels[:2], ["What (2020)", "What (2008)"])
        film_btn = next(b for b in m.buttons() if b.text.startswith("What (2020)"))
        await self.tap(m, film_btn.callback_data)
        self.assertIn("2 versions", m.last)
        variants = [b for b in m.buttons() if b.callback_data.startswith("s:")]
        self.assertTrue(variants[0].text.startswith("1080p"))           # best quality first
        cb = await self.tap(m, variants[0].callback_data)
        chat_id, kw = self.sources[(CH, 1)].copied
        self.assertEqual(chat_id, USER)
        self.assertIn("What", kw["caption"])
        cb.answer.assert_awaited()
        self.assertEqual(self.ctx.index.search("what 2020").hits[0].film.hits, 1)

    async def test_exact_unique_goes_straight_to_film(self):
        m = await self.say("what 2008")
        self.assertIn("1 version", m.last)

    async def test_typo_is_fixed_and_says_so(self):
        m = await self.say("avngers endgame")
        self.assertIn("Showing results for", m.last)
        self.assertIn("Avengers", m.last)

    async def test_symbols_only_gets_a_reply_not_silence(self):
        m = await self.say("?!?!")
        self.assertIn("film name", m.texts[0])

    async def test_nothing_found_offers_request_and_notifies_admin(self):
        m = await self.say("zzqx wvvk")
        self.assertIn("Couldn't find", m.texts[0])
        cb = await self.tap(m, "rq")
        self.assertIn("Requested", cb.answer.await_args.args[0])
        self.client.send_message.assert_awaited()
        self.assertEqual(self.client.send_message.await_args.args[0], ADMIN)
        await self.tap(m, "rq", user=USER)                      # same person again: no second ping
        self.assertEqual(self.client.send_message.await_count, 1)
        self.assertEqual((await self.ctx.store.top_misses())[0][0], "zzqx wvvk")

    async def test_someone_elses_buttons_are_refused(self):
        m = await self.say("what")
        cb = await self.tap(m, "f:whatever", user=OTHER)
        self.assertEqual(cb.answer.await_args.args[0], H.ui.NOT_YOURS)

    async def test_expired_session(self):
        cb = await self.tap(Msg(), "r:1")
        self.assertEqual(cb.answer.await_args.args[0], H.ui.EXPIRED)

    async def test_group_variant_tap_opens_bot_with_deeplink(self):
        m = await self.say("what 2008", user=USER, chat=-500, ctype=ChatType.GROUP)
        cb = await self.tap(m, "s:0")
        self.assertIn("t.me/tessbot?start=f_", cb.answer.await_args.kwargs["url"])

    async def test_group_ignores_chatter_and_respects_toggle(self):
        self.assertIsNone(await self.say("hi", chat=-500, ctype=ChatType.GROUP))
        await self.ctx.set_setting("groups", False)
        self.assertIsNone(await self.say("what 2008", chat=-500, ctype=ChatType.GROUP))

    async def test_not_ready_says_waking_up(self):
        self.ctx.ready = False
        m = await self.say("what")
        self.assertEqual(m.texts[0], H.ui.WARMING)

    async def test_banned_user_is_ignored(self):
        self.ctx.banned.add(USER)
        self.assertIsNone(await self.say("what"))

    async def test_search_is_throttled(self):
        await self.say("what")
        self.assertIsNone(await self.say("what 2008"))

    async def test_paging_buttons_stay_within_telegram_limit(self):
        for i in range(30):
            self.ctx.index.add_film(f"Echo {i}", 2000 + i % 20, file_count=2)
        m = await self.say("echo")
        for b in m.buttons():
            self.assertLessEqual(len((b.callback_data or "").encode()), 64)
        await self.tap(m, "r:2")
        self.assertTrue(any(b.text.startswith("3/") for b in m.buttons()))
        self.assertTrue(any(b.callback_data == "r:1" for b in m.buttons()))


class DeliveryTests(Base):
    async def test_deleted_source_message_removes_file_and_apologises(self):
        rec = (await self.ctx.store.film_files(self.ctx.index.search("what 2008").hits[0].film.id))[0]
        del self.sources[(CH, rec.mid)]
        ok = await H.deliver(self.ctx, self.client, USER, rec)
        self.assertFalse(ok)
        self.assertIsNone(await self.ctx.store.get_file(rec.uid))
        self.assertIn("gone", self.client.send_message.await_args.args[1])

    async def test_autodelete_schedules_removal(self):
        await self.ctx.set_setting("autodelete_min", 5)
        tasks = []
        real = asyncio.create_task
        H.asyncio.create_task = lambda c: tasks.append(c) or real(asyncio.sleep(0))
        try:
            rec = (await self.ctx.store.film_files(self.ctx.index.search("what 2008").hits[0].film.id))[0]
            self.assertTrue(await H.deliver(self.ctx, self.client, USER, rec))
        finally:
            H.asyncio.create_task = real
        self.assertEqual(len(tasks), 1)
        for t in tasks: t.close()
        self.assertIn("5 min", self.sources[(CH, rec.mid)].copied[1]["caption"])

    async def test_deep_link_start_delivers(self):
        rec = (await self.ctx.store.film_files(self.ctx.index.search("what 2008").hits[0].film.id))[0]
        m = Msg(text="/start", command=["start", f"f_{rec.uid}"])
        await H.cmd_start(self.ctx, self.client, m)
        self.assertEqual(self.sources[(CH, rec.mid)].copied[0], USER)

    async def test_report_notifies_admin_with_remove_button(self):
        rec = (await self.ctx.store.film_files(self.ctx.index.search("what 2008").hits[0].film.id))[0]
        await self.tap(Msg(), f"rep:{rec.uid}")
        rows = self.client.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rows[0][0].callback_data, f"rmfile:{rec.uid}")


class ForceJoinTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.ctx.set_setting("force_sub", {"chat": -999, "link": "https://t.me/x", "title": "Our Channel"})

    async def test_non_member_is_blocked_with_join_button(self):
        self.client.get_chat_member = AsyncMock(side_effect=UserNotParticipant())
        m = await self.say("what")
        self.assertIn("Join", m.texts[0])
        self.assertEqual(m.buttons()[0].url, "https://t.me/x")

    async def test_member_passes(self):
        self.client.get_chat_member = AsyncMock(return_value=NS(status=ChatMemberStatus.MEMBER))
        m = await self.say("what")
        self.assertIn("film", m.texts[0])

    async def test_check_failure_lets_users_in_and_alerts_once(self):
        self.client.get_chat_member = AsyncMock(side_effect=RPCError("boom"))
        m = await self.say("what 2008")
        self.assertIn("1 version", m.last)
        self.ctx._last_search.clear()
        await self.say("what 2008")
        self.assertEqual(self.client.send_message.await_count, 1)

    async def test_admin_skips_check(self):
        self.client.get_chat_member = AsyncMock(side_effect=UserNotParticipant())
        m = await self.say("what 2008", user=ADMIN, chat=ADMIN)
        self.assertIn("1 version", m.last)


class AdminTests(Base):
    async def cmd(self, text):
        m = Msg(chat_id=ADMIN, user=ADMIN, text=text, command=text.lstrip("/").split())
        await H.on_admin_cmd(self.ctx, self.client, m)
        return m.sink[-1]

    async def test_forcesub_set_checks_bot_is_admin(self):
        self.client.get_chat = AsyncMock(return_value=NS(id=-999, title="Our Channel", username="ourchan", invite_link=None))
        self.client.get_chat_member = AsyncMock(return_value=NS(status=ChatMemberStatus.MEMBER))
        self.assertIn("admin", (await self.cmd("/forcesub -999")).texts[0])
        self.assertIsNone((await self.ctx.settings())["force_sub"])
        self.client.get_chat_member = AsyncMock(return_value=NS(status=ChatMemberStatus.ADMINISTRATOR))
        await self.cmd("/forcesub -999")
        self.ctx._settings = None
        fs = (await self.ctx.settings())["force_sub"]
        self.assertEqual((fs["chat"], fs["link"]), (-999, "https://t.me/ourchan"))
        await self.cmd("/forcesub off"); self.ctx._settings = None
        self.assertIsNone((await self.ctx.settings())["force_sub"])

    async def test_alias_teaches_a_native_title(self):
        self.assertEqual((await self.say("gisaengchung")).texts[0][:7], "Couldn'")
        await self.cmd("/alias avengers endgame = Gisaengchung")
        self.ctx._last_search.clear()
        self.assertIn("Avengers", (await self.say("gisaengchung")).last)

    async def test_ban_and_unban(self):
        await self.cmd(f"/ban {OTHER}")
        self.assertIn(OTHER, self.ctx.banned)
        await self.cmd(f"/unban {OTHER}")
        self.assertNotIn(OTHER, self.ctx.banned)

    async def test_stats_and_settings_cycle(self):
        self.assertIn("Films: 3", (await self.cmd("/stats")).texts[0])
        panel = await self.cmd("/settings")
        await self.tap(panel, "st:ad", user=ADMIN); await self.tap(panel, "st:pc", user=ADMIN)
        s = await self.ctx.store.settings()
        self.assertEqual((s["autodelete_min"], s["protect"]), (5, True))

    async def test_non_admin_cannot_use_admin_buttons(self):
        cb = await self.tap(Msg(), "rmy:abc", user=USER)
        self.assertEqual(cb.answer.await_args.args[0], "Admins only.")

    async def test_remove_film_asks_then_removes_everywhere(self):
        film = self.ctx.index.search("what 2020").hits[0].film
        m = Msg()
        await self.tap(m, f"rmf:{film.id}", user=ADMIN)
        self.assertIn("Remove", m.last)
        await self.tap(m, f"rmy:{film.id}", user=ADMIN)
        self.assertEqual(await self.ctx.store.film_files(film.id), [])
        left = self.ctx.index.search("what")
        self.assertEqual([h.film.year for h in left.hits], [2008])
        self.assertEqual(len(self.ctx.index), 2)

    async def test_backfill_walks_ids_and_reports(self):
        calls = []
        async def get_messages(ch, ids):
            calls.append(ids)
            return [self.sources.get((ch, i)) or NS(empty=True) for i in ids]
        self.client.get_messages = get_messages
        status = Msg(); status.sink = []
        await H.backfill(self.ctx, self.client, status, CH, 1, 450)
        self.assertEqual([len(c) for c in calls], [200, 200, 50])
        self.assertIn("Done", status.sink[-1].texts[0])

    async def test_backfill_survives_flood_wait(self):
        n = {"i": 0}
        async def get_messages(ch, ids):
            n["i"] += 1
            if n["i"] == 1: raise FloodWait(0)
            return []
        self.client.get_messages = get_messages
        status = Msg(); status.sink = []
        await H.backfill(self.ctx, self.client, status, CH, 1, 10)
        self.assertIn("Done", status.sink[-1].texts[0])


class RequestFulfilTests(Base):
    async def test_requesters_are_told_when_film_arrives(self):
        await self.ctx.store.add_request("parasite 2019", "Parasite 2019", USER)
        await self.ctx.store.add_request("avengers endgame", "Avengers Endgame", OTHER)
        task = asyncio.create_task(H.requests_job(self.ctx, self.client, every=0))
        await asyncio.sleep(0.05); task.cancel()
        told = {c.args[0] for c in self.client.send_message.await_args_list}
        self.assertEqual(told, {OTHER})                       # only the film we actually have
        self.assertEqual([r["text"] for r in await self.ctx.store.open_requests()], ["Parasite 2019"])


if __name__ == "__main__":
    unittest.main()
