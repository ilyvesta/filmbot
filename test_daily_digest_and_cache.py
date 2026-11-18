import asyncio
import types
import unittest
from datetime import datetime
from unittest.mock import AsyncMock

import stub_pyrogram

stub_pyrogram.install()

from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import UserNotParticipant

import handlers as H
from config import Config
from ctx import Ctx, VariantCache
from index import FilmIndex, film_id
from normalize import normalize
from parse import button_label
from store import FileRec, MemoryStore, MongoStore
from test_filmbot_audit_fixes import _FakeMongoCollection, _FakeMongoCursor, _Msg, _SrcMsg, _user


ADMIN = 99999
U1 = 1001
U2 = 1002
U3 = 1003
STORAGE_CH = -100123456
DIGEST_CH = -100777888
FORCE_CH = -100999111


def _cfg():
    return Config(
        1,
        "hash",
        "tok",
        frozenset({ADMIN}),
        frozenset({STORAGE_CH}),
        "mongodb://x",
        "db",
        None,
        8080,
    )


def _ctx(store=None, idx=None):
    s = store or MemoryStore()
    i = idx or FilmIndex()
    c = Ctx(_cfg(), s, i)
    c.ready = True
    c.bot_username = "filmbot"
    return c


class _RichMongoCollection(_FakeMongoCollection):
    def find(self, filt=None, projection=None, **kw):
        filt = filt or {}
        self.queries.append(dict(filt))
        rows = []
        for doc in self.docs.values():
            ok = True
            for k, v in filt.items():
                val = doc.get(k)
                if isinstance(v, dict):
                    if "$gt" in v and not (val is not None and val > v["$gt"]):
                        ok = False
                        break
                    if "$gte" in v and not (val is not None and val >= v["$gte"]):
                        ok = False
                        break
                    if "$lte" in v and not (val is not None and val <= v["$lte"]):
                        ok = False
                        break
                    if "$in" in v and val not in v["$in"]:
                        ok = False
                        break
                    if "$nin" in v and val in v["$nin"]:
                        ok = False
                        break
                elif val != v:
                    ok = False
                    break
            if ok:
                rows.append(dict(doc))
        return _FakeMongoCursor(rows, projection)

    async def update_one(self, filt, upd, upsert=False):
        key = filt["_id"]
        doc = self.docs.get(key)
        inserted = False
        if doc is None:
            if not upsert:
                return False
            inserted = True
            doc = {"_id": key}
            for k, v in upd.get("$setOnInsert", {}).items():
                doc[k] = list(v) if isinstance(v, list) else v
        for k, v in upd.get("$set", {}).items():
            doc[k] = v
        for k, v in upd.get("$inc", {}).items():
            doc[k] = doc.get(k, 0) + v
        for k, v in upd.get("$addToSet", {}).items():
            arr = list(doc.get(k) or [])
            if v not in arr:
                arr.append(v)
            doc[k] = arr
        self.docs[key] = doc
        return inserted

    async def bulk_write(self, ops, ordered=False):
        self.bulk_writes += 1
        upserted = {}
        for idx, op in enumerate(ops):
            filt = getattr(op, "_filter", getattr(op, "filter", None))
            upd = getattr(op, "_doc", getattr(op, "doc", None))
            upsert = getattr(op, "_upsert", getattr(op, "upsert", False))
            if await self.update_one(filt, upd, upsert=upsert):
                upserted[idx] = filt["_id"]
        return types.SimpleNamespace(upserted_ids=upserted)

    async def insert_many(self, docs, ordered=False):
        for d in docs:
            self.docs[d["_id"]] = dict(d)


class _DigestClient:
    def __init__(self, messages=None, non_members=None):
        self.me = types.SimpleNamespace(id=777, username="filmbot")
        self.sent = []
        self.messages = dict(messages or {})
        self.non_members = set(non_members or ())
        self.chats = {
            DIGEST_CH: types.SimpleNamespace(id=DIGEST_CH, title="Daily Film Updates", username="dailyfilms"),
            FORCE_CH: types.SimpleNamespace(id=FORCE_CH, title="Join Channel", username="joinchan"),
        }

    async def get_chat(self, target):
        if isinstance(target, str) and target.lstrip("@") == "dailyfilms":
            return self.chats[DIGEST_CH]
        if target in self.chats:
            return self.chats[target]
        return types.SimpleNamespace(id=int(target), title=f"Chat {target}", username=None)

    async def get_chat_member(self, chat_id, uid):
        if uid == "me":
            return types.SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
        if (chat_id, uid) in self.non_members:
            raise UserNotParticipant("not member")
        return types.SimpleNamespace(status=ChatMemberStatus.MEMBER)

    async def send_message(self, chat_id, text, **kw):
        msg = _Msg(text=text, uid=ADMIN, chat=chat_id, ctype=ChatType.CHANNEL)
        msg.reply_markup = kw.get("reply_markup")
        self.sent.append((chat_id, text, kw))
        return msg

    async def get_messages(self, chat_id, message_ids):
        if isinstance(message_ids, list):
            return [self.messages.get((chat_id, mid), types.SimpleNamespace(id=mid, empty=True)) for mid in message_ids]
        return self.messages.get((chat_id, message_ids), types.SimpleNamespace(id=message_ids, empty=True))


class _Callback:
    def __init__(self, data, uid=U1, chat=U1, msg=None):
        self.data = data
        self.from_user = _user(uid)
        self.message = msg or _Msg(text="menu", uid=uid, chat=chat)
        self.answered = []

    async def answer(self, text="", show_alert=False):
        self.answered.append((text, show_alert))


async def _add_film(store, idx, title, year, ts, fid_suffix="", size=750_000_000, mid=100, aliases=()):
    fid = film_id(normalize(title), year)
    rec = FileRec(
        uid=f"u_{fid}_{mid}{fid_suffix}",
        film=fid,
        ch=STORAGE_CH,
        mid=mid,
        size=size,
        res="1080p",
        src="WEB-DL",
        langs=("English",),
        name=f"{title}.{year}.1080p.WEB-DL.x265.mkv",
        codec="x265",
        ext="mkv",
    )
    await store.add_files([rec], {fid: (title, year)})
    if isinstance(store, MemoryStore):
        store.films[fid]["ts"] = ts
        if aliases:
            store.films[fid]["a"] = list(aliases)
    else:
        await store.db.films.update_one({"_id": fid}, {"$set": {"ts": ts}})
        if aliases:
            await store.db.films.update_one({"_id": fid}, {"$set": {"a": list(aliases)}})
    idx.add_film(title, year, file_count=1, aliases=tuple(aliases))
    return fid, rec


class TestDailyDigest(unittest.TestCase):
    def test_eligibility_cutoff_ranking_cap_and_formatting(self):
        async def run():
            ctx = _ctx()
            c1 = datetime(2026, 5, 10, 10, 0, tzinfo=H.LAGOS).timestamp()
            c2 = datetime(2026, 5, 11, 10, 0, tzinfo=H.LAGOS).timestamp()
            await ctx.set_setting("digest_enabled", True)
            await ctx.set_setting("digest_channel", DIGEST_CH)
            await ctx.set_setting("digest_channel_title", "Daily Film Updates")
            await ctx.set_setting("digest_last_cutoff", c1)

            # Ineligible <= 2025 indexed inside window
            await _add_film(ctx.store, ctx.index, "Old Classic", 2025, c1 + 600, mid=1)
            await _add_film(ctx.store, ctx.index, "Prior Batch", 2026, c1 - 10, mid=2)
            await _add_film(ctx.store, ctx.index, "Next Day", 2026, c2 + 1, mid=3)

            # Qualified films with distinct demand scenarios
            f_hi, _ = await _add_film(ctx.store, ctx.index, "Alpha Horizon", 2026, c1 + 100, mid=10)
            await ctx.store.add_request("alpha horizon 2026", "Alpha Horizon 2026", U1)
            await ctx.store.add_miss("alpha horizon 2026", U1)
            await ctx.store.add_miss("alpha horizon", U2)

            f_tie_old, _ = await _add_film(ctx.store, ctx.index, "Beta Dawn", 2026, c1 + 200, mid=11)
            f_tie_new, _ = await _add_film(ctx.store, ctx.index, "Gamma Dusk", 2027, c1 + 500, mid=12)
            await ctx.store.add_request("beta dawn", "Beta Dawn", U3)
            await ctx.store.add_miss("gamma dusk 2027", U3)

            zero_fids = []
            for k in range(15):
                fid, _ = await _add_film(ctx.store, ctx.index, f"Zero Film {k:02d}", 2026, c1 + 1000 + k, mid=20 + k)
                zero_fids.append((k, fid))

            client = _DigestClient()
            now_after_10 = datetime(2026, 5, 11, 10, 5, tzinfo=H.LAGOS)
            res = await H.maybe_send_daily_digest(ctx, client, now=now_after_10)
            self.assertEqual(res["published"], 15)
            self.assertEqual(len(client.sent), 1)
            chat_id, text, _ = client.sent[0]
            self.assertEqual(chat_id, DIGEST_CH)
            lines = [ln for ln in text.splitlines() if ln and ln[0].isdigit()]
            self.assertEqual(len(lines), 15)
            self.assertIn(f"https://t.me/filmbot?start=film_{f_hi}", lines[0])
            self.assertIn(f"https://t.me/filmbot?start=film_{f_tie_new}", lines[1])
            self.assertIn(f"https://t.me/filmbot?start=film_{f_tie_old}", lines[2])
            self.assertIn("Zero Film 14", lines[3])
            self.assertNotIn("Old Classic", text)
            self.assertNotIn("Prior Batch", text)
            self.assertNotIn("Next Day", text)
            self.assertNotIn("Zero Film 00", text)

            # Second run same day does not resend
            res2 = await H.maybe_send_daily_digest(ctx, client, now=datetime(2026, 5, 11, 12, 0, tzinfo=H.LAGOS))
            self.assertEqual(res2["reason"], "not_due")
            self.assertEqual(len(client.sent), 1)

            # Next day with only "Next Day" (2026), then empty day sends nothing
            res3 = await H.maybe_send_daily_digest(ctx, client, now=datetime(2026, 5, 12, 10, 1, tzinfo=H.LAGOS))
            self.assertEqual(res3["published"], 1)
            self.assertIn("Next Day (2026)", client.sent[1][1])

            res4 = await H.maybe_send_daily_digest(ctx, client, now=datetime(2026, 5, 13, 10, 1, tzinfo=H.LAGOS))
            self.assertEqual(res4["reason"], "no_candidates")
            self.assertEqual(len(client.sent), 2)

        asyncio.run(run())

    def test_digest_splits_long_messages(self):
        films = [
            {"fid": f"abcdef01{i:02d}", "title": "A" * 350, "year": 2026, "ts": 100 + i}
            for i in range(15)
        ]
        msgs = H.format_digest_messages(films, "filmbot", "2026-05-11")
        self.assertGreater(len(msgs), 1)
        for m in msgs:
            self.assertLessEqual(len(m), 3850)

    def test_deep_link_opens_film_and_preserves_force_join(self):
        async def run():
            ctx = _ctx()
            fid, _ = await _add_film(ctx.store, ctx.index, "Future Arrival", 2026, 1000, size=900_000_000, mid=42)
            await ctx.set_setting("force_subs", [{"chat": FORCE_CH, "link": "https://t.me/joinchan", "title": "Join Channel"}])

            client = _DigestClient(non_members={(FORCE_CH, U1)})
            m = _Msg(f"/start film_{fid}", uid=U1, chat=U1, command=["start", f"film_{fid}"])
            await H.cmd_start(ctx, client, m)

            self.assertEqual(len(m.sink), 1)
            self.assertIn("join", m.sink[0].text.lower())
            self.assertEqual(ctx.pending[U1]["type"], "film")
            self.assertEqual(ctx.pending[U1]["fid"], fid)

            client.non_members.clear()
            cb = _Callback("fsok", uid=U1, chat=U1, msg=m.sink[0])
            await H.on_cb(ctx, client, cb)

            self.assertIn("Future Arrival", cb.message.text)
            btns = [b.text for b in cb.message.buttons()]
            self.assertTrue(any("1080p" in b and "MB" in b for b in btns), btns)

        asyncio.run(run())

    def test_channel_config_via_command_and_forward(self):
        async def run():
            ctx = _ctx()
            client = _DigestClient()

            m = _Msg("/setupdatechannel @dailyfilms", uid=ADMIN, chat=ADMIN, command=["setupdatechannel", "@dailyfilms"])
            await H.on_admin_cmd(ctx, client, m)
            s = await ctx.settings()
            self.assertEqual(s["digest_channel"], DIGEST_CH)
            self.assertTrue(s["digest_enabled"])

            m_bad = _Msg("/digest", uid=U1, chat=U1, command=["digest"])
            await H.on_admin_cmd(ctx, client, m_bad)
            self.assertEqual(len(m_bad.sink), 0)

            m_menu = _Msg("/digest", uid=ADMIN, chat=ADMIN, command=["digest"])
            await H.on_admin_cmd(ctx, client, m_menu)
            cb_test = _Callback("dg:test", uid=ADMIN, chat=ADMIN, msg=m_menu.sink[0])
            await H.on_cb(ctx, client, cb_test)
            self.assertTrue(any(cid == DIGEST_CH for cid, _, _ in client.sent))

            cb_set = _Callback("dg:set", uid=ADMIN, chat=ADMIN, msg=m_menu.sink[0])
            await H.on_cb(ctx, client, cb_set)
            self.assertIn(ADMIN, ctx.pending_digest_channel)
            fwd = _Msg("", uid=ADMIN, chat=ADMIN)
            fwd.forward_from_chat = types.SimpleNamespace(id=DIGEST_CH, title="Daily Film Updates")
            await H.on_text(ctx, client, fwd)
            self.assertNotIn(ADMIN, ctx.pending_digest_channel)

        asyncio.run(run())


class TestSizeRecoveryAndVariantCache(unittest.TestCase):
    def test_button_label_preserves_size_and_passive_recovery_updates_zero_size(self):
        with_size = button_label("1080p", "WEB-DL", "x265", 950_000_000, ("English",), (), "mkv", None)
        without_size = button_label("1080p", "WEB-DL", "x265", 0, ("English",), (), "mkv", None)
        self.assertIn("MB", with_size)
        self.assertNotIn("MB", without_size)

        async def run():
            ctx = _ctx()
            fid, rec = await _add_film(ctx.store, ctx.index, "Zero Size Movie", 2024, 1000, size=0, mid=55)
            vars_before = await H.load_variants(ctx, fid)
            self.assertEqual(vars_before[0].size, 0)
            self.assertIsNotNone(ctx.variant_cache.get(fid))

            src = _SrcMsg(STORAGE_CH, 55, rec.uid, rec.name, size=820_000_000)
            client = _DigestClient(messages={(STORAGE_CH, 55): src})
            ok = await H.deliver(ctx, client, U1, rec)
            self.assertTrue(ok)
            self.assertIn(U1, src.copied)

            stored = await ctx.store.film_files(fid)
            self.assertEqual(stored[0].size, 820_000_000)
            vars_after = await H.load_variants(ctx, fid)
            self.assertEqual(vars_after[0].size, 820_000_000)

        asyncio.run(run())

    def test_recover_sizes_command_fixes_only_zero_size_records(self):
        async def run():
            ctx = _ctx()
            f_zero, r_zero = await _add_film(ctx.store, ctx.index, "Missing Size", 2025, 1000, size=0, mid=61)
            f_ok, r_ok = await _add_film(ctx.store, ctx.index, "Has Size", 2025, 1000, size=444_000_000, mid=62)
            await H.load_variants(ctx, f_zero)
            await H.load_variants(ctx, f_ok)

            src61 = _SrcMsg(STORAGE_CH, 61, r_zero.uid, r_zero.name, size=777_000_000)
            client = _DigestClient(messages={(STORAGE_CH, 61): src61})
            client.get_messages = AsyncMock(side_effect=client.get_messages)

            m = _Msg("/recoversizes", uid=ADMIN, chat=ADMIN, command=["recoversizes"])
            await H.on_admin_cmd(ctx, client, m)

            self.assertEqual((await ctx.store.film_files(f_zero))[0].size, 777_000_000)
            self.assertEqual((await ctx.store.film_files(f_ok))[0].size, 444_000_000)
            self.assertIsNone(ctx.variant_cache.get(f_zero))
            self.assertIsNotNone(ctx.variant_cache.get(f_ok))

        asyncio.run(run())

    def test_variant_cache_ttl_lru_and_post_write_invalidation(self):
        cache = VariantCache(ttl=10.0, maxsize=2)
        cache.set("f1", ["v1"], now=100.0)
        cache.set("f2", ["v2"], now=101.0)
        self.assertEqual(cache.get("f1", now=102.0), ["v1"])
        cache.set("f3", ["v3"], now=103.0)
        self.assertIsNone(cache.get("f2", now=103.0))
        self.assertEqual(cache.get("f1", now=103.0), ["v1"])
        self.assertIsNone(cache.get("f1", now=113.0))

        async def run():
            ctx = _ctx()
            fid, _ = await _add_film(ctx.store, ctx.index, "Cached Odyssey", 2026, 1000, size=500_000_000, mid=70)
            calls = 0
            orig_variants = ctx.store.film_files

            async def counted_variants(f):
                nonlocal calls
                calls += 1
                return await orig_variants(f)

            ctx.store.film_files = counted_variants
            first = await H.load_variants(ctx, fid)
            second = await H.load_variants(ctx, fid)
            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)
            self.assertEqual(calls, 1)

            new_msg = _SrcMsg(STORAGE_CH, 71, "uid_odyssey_2", "Cached.Odyssey.2026.720p.WEB-DL.x264.mkv", size=350_000_000)
            new_msg.caption = None
            await H.index_messages(ctx, [new_msg])
            self.assertIsNone(ctx.variant_cache.get(fid))
            third = await H.load_variants(ctx, fid)
            self.assertEqual(len(third), 2)
            self.assertEqual(calls, 2)

            await H._drop(ctx, third[1])
            self.assertIsNone(ctx.variant_cache.get(fid))

        asyncio.run(run())

    def test_mongo_store_miss_dedup_and_digest_query(self):
        async def run():
            store = MongoStore.__new__(MongoStore)
            store.db = types.SimpleNamespace(
                command=AsyncMock(return_value={"ok": 1}),
                files=_RichMongoCollection(),
                films=_RichMongoCollection(),
                cfg=_RichMongoCollection(),
                requests=_RichMongoCollection(),
                misses=_RichMongoCollection(),
                daily=_RichMongoCollection(),
                skips=_RichMongoCollection(),
                deletions=_RichMongoCollection(),
                claims=_RichMongoCollection(),
                bot_users=_RichMongoCollection(),
                survey_responses=_RichMongoCollection(),
            )

            await store.add_miss("dune 2026", 111)
            await store.add_miss("dune 2026", 111)
            await store.add_miss("dune 2026", 222)
            _, miss_rows = await store.demand_records()
            self.assertEqual(len(miss_rows), 1)
            self.assertEqual(sorted(miss_rows[0]["users"]), [111, 222])

            idx = FilmIndex()
            fid_new, _ = await _add_film(store, idx, "Dune", 2026, 2000, mid=81)
            fid_old, _ = await _add_film(store, idx, "Dune", 2021, 2000, mid=82)
            cands = await store.digest_candidates(1000, 2500, min_year=2026)
            self.assertEqual([c["fid"] for c in cands], [fid_new])
            self.assertNotEqual(fid_new, fid_old)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
