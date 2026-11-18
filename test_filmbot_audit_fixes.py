import asyncio
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

import stub_pyrogram

stub_pyrogram.install()

from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import RPCError, UserNotParticipant

import handlers as H
import ui
from config import Config
from ctx import Ctx
from index import FilmIndex, film_id
from normalize import normalize
from parse import parse_filename
from store import FileRec, MemoryStore, MongoStore


ADMIN = 99999
OWNER = 1001
OTHER = 2002
CHANNEL = -100123456
GROUP = -100555666


def _cfg():
    return Config(1, "hash", "tok", frozenset({ADMIN}), frozenset({CHANNEL}), "mongodb://x", "db", None, 8080)


def _ctx(store, idx):
    c = Ctx(_cfg(), store, idx)
    c.ready = True
    c.bot_username = "filmbot"
    return c


def _user(uid, name="User"):
    return types.SimpleNamespace(id=uid, first_name=name, username=None)


class _Msg:
    _seq = 500

    def __init__(self, text="", uid=OWNER, chat=OWNER, ctype=ChatType.PRIVATE, command=None, photo=None):
        _Msg._seq += 1
        self.id = _Msg._seq
        self.chat = types.SimpleNamespace(id=chat, type=ctype)
        self.from_user = _user(uid)
        self.text = text
        self.caption = None
        self.command = command or (text[1:].split() if text.startswith("/") else [])
        self.photo = photo
        self.document = self.video = self.audio = self.voice = self.video_note = None
        self.sticker = self.animation = None
        self.forward_date = self.forward_from = self.forward_sender_name = None
        self.reply_to_message = None
        self.reply_markup = None
        self.sink = []
        self.deleted = False

    async def reply_text(self, text, **kw):
        m = _Msg(text=text, uid=self.from_user.id, chat=self.chat.id, ctype=self.chat.type)
        m.reply_markup = kw.get("reply_markup")
        self.sink.append(m)
        return m

    async def reply_photo(self, photo, caption="", **kw):
        m = _Msg(text=caption, uid=self.from_user.id, chat=self.chat.id, ctype=self.chat.type, photo=photo)
        m.caption = caption
        m.reply_markup = kw.get("reply_markup")
        self.sink.append(m)
        return m

    async def edit_text(self, text, **kw):
        self.text = text
        self.reply_markup = kw.get("reply_markup")
        return self

    async def edit_caption(self, caption, **kw):
        self.caption = caption
        self.reply_markup = kw.get("reply_markup")
        return self

    async def edit_media(self, media, **kw):
        self.caption = getattr(media, "caption", "")
        self.reply_markup = kw.get("reply_markup")
        return self

    async def delete(self):
        self.deleted = True

    def buttons(self):
        if not self.reply_markup:
            return []
        return [b for row in self.reply_markup.inline_keyboard for b in row]


class _SrcMsg:
    def __init__(self, ch, mid, uid, name, size=500_000_000, fail_uids=None):
        self.chat = types.SimpleNamespace(id=ch)
        self.id = mid
        self.empty = False
        self.document = types.SimpleNamespace(file_unique_id=uid, file_name=name, file_size=size)
        self.video = self.audio = None
        self.copied = []
        self.fail_uids = fail_uids or set()

    async def copy(self, chat_id, **kw):
        if chat_id in self.fail_uids:
            raise RPCError("PEER_ID_INVALID")
        self.copied.append(chat_id)
        return types.SimpleNamespace(id=888, chat=types.SimpleNamespace(id=chat_id))


class _FakeMongoCursor:
    def __init__(self, docs, projection=None):
        self._docs = docs
        self._proj = projection
        self._limit = None

    def sort(self, key, direction):
        reverse = direction == -1
        self._docs = sorted(self._docs, key=lambda d: d.get(key), reverse=reverse)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _project(self, doc):
        if not self._proj:
            return dict(doc)
        out = {"_id": doc["_id"]}
        for k, v in self._proj.items():
            if v and k in doc:
                out[k] = doc[k]
        return out

    async def to_list(self, length=None):
        lim = self._limit if self._limit is not None else length
        sliced = self._docs[:lim] if lim is not None else self._docs
        return [self._project(d) for d in sliced]

    def __aiter__(self):
        self._it = iter([self._project(d) for d in self._docs])
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeMongoCollection:
    def __init__(self, initial=None):
        self.docs: dict = {d["_id"]: dict(d) for d in (initial or [])}
        self.queries = []
        self.bulk_writes = 0

    def find(self, filt=None, projection=None, **kw):
        filt = filt or {}
        self.queries.append(dict(filt))
        rows = []
        for doc in self.docs.values():
            ok = True
            for k, v in filt.items():
                if isinstance(v, dict) and "$gt" in v:
                    if not (doc.get(k) > v["$gt"]):
                        ok = False
                        break
                elif isinstance(v, dict) and "$in" in v:
                    if doc.get(k) not in v["$in"]:
                        ok = False
                        break
                elif doc.get(k) != v:
                    ok = False
                    break
            if ok:
                rows.append(dict(doc))
        return _FakeMongoCursor(rows, projection)

    async def find_one(self, filt=None, projection=None):
        res = await self.find(filt, projection).limit(1).to_list(1)
        return res[0] if res else None

    async def count_documents(self, filt=None, limit=None):
        if not filt:
            return min(len(self.docs), limit) if limit else len(self.docs)
        res = await self.find(filt).to_list(limit)
        return len(res)

    async def update_one(self, filt, upd, upsert=False):
        key = filt["_id"]
        doc = self.docs.get(key)
        if doc is None:
            if not upsert:
                return
            doc = {"_id": key}
            for k, v in upd.get("$setOnInsert", {}).items():
                doc[k] = v
        for k, v in upd.get("$set", {}).items():
            doc[k] = v
        for k, v in upd.get("$inc", {}).items():
            doc[k] = doc.get(k, 0) + v
        self.docs[key] = doc

    async def bulk_write(self, ops, ordered=False):
        self.bulk_writes += 1
        for op in ops:
            filt = getattr(op, "_filter", getattr(op, "filter", None))
            upd = getattr(op, "_doc", getattr(op, "doc", None))
            upsert = getattr(op, "_upsert", getattr(op, "upsert", False))
            if filt is None:
                # Support dict-like PyMongo UpdateOne internals across versions
                r = repr(op)
                raise AssertionError(f"Unsupported op {r}")
            await self.update_one(filt, upd, upsert=upsert)

    async def delete_many(self, filt):
        in_ids = set(filt.get("_id", {}).get("$in", []))
        removed = 0
        for k in list(self.docs):
            if k in in_ids:
                del self.docs[k]
                removed += 1
        return types.SimpleNamespace(deleted_count=removed)

    async def aggregate(self, pipeline, allowDiskUse=False):
        counts = {}
        for d in self.docs.values():
            f = d.get("f")
            counts[f] = counts.get(f, 0) + 1
        rows = [{"_id": fid, "n": n} for fid, n in counts.items()]
        return _FakeMongoCursor(rows)


class ParsePromoAndSeriesTests(unittest.TestCase):
    def test_rarbg_and_spaced_promo_prefixes_extract_aprile_1998(self):
        variants = [
            "A R B G-Aprile (1998) @R_A_R_B_G-aprile.1998.internal.bdrip.x264-manic.mkv",
            "@R_A_R_B_G_Aprile_1998.mkv",
            "@R A R B G-Aprile (1998).mkv",
            "R A R B G-Aprile (1998).mkv",
            "A R B G-Aprile 1998 @R_A_R_B_G-aprile.1998.internal.bdrip.x264-manic.mkv",
            "A R B G Aprile (1998) @R_A_R_B_G-aprile.1998.internal.bdrip.x264-manic.mkv",
            "R A R B G Aprile (1998).mkv",
        ]
        for raw in variants:
            p = parse_filename(raw)
            self.assertEqual(p.kind, "movie", raw)
            self.assertEqual(p.title, "Aprile", raw)
            self.assertEqual(p.year, 1998, raw)

        p_pfm = parse_filename("PFM_Inception.2010.1080p.mkv", promos=["PFM"])
        self.assertEqual((p_pfm.kind, p_pfm.title, p_pfm.year), ("movie", "Inception", 2010))

    def test_series_guards_catch_episode_formats(self):
        episodes = [
            "@DramaOST.E01.The.Show.720p.mkv",
            "[TIF]_S01_E01_Show.mkv",
            "S4.16. Attack On Titan [720p Dual] @Animax_Industry.mkv",
            "07 - Demon Slayer [1080p] [Sub].mkv",
            "Ep-01 [480p] Show.mkv",
            "Yali Capkini 32 Bölüm.mp4",
            "Show.S03X02.720p.mkv",
            "Show.S01S01.720p.mkv",
            "steven.universe.ss03e01.1080p.mkv",
            "S2- 5 Too Hot To Handle.mkv",
            "Show S01 E01 720p.mkv",
            "Show Episode 01 1080p.mkv",
            "Show 00 [OVA].mkv",
            "Show Complete Season 1 1080p.mkv",
            "Show Seasons 1-4 720p.mkv",
        ]
        for raw in episodes:
            p = parse_filename(raw)
            self.assertEqual(p.kind, "series", f"Should classify as series: {raw} -> {p}")

    def test_genuine_films_never_classified_as_series(self):
        movies = [
            ("10x10 (2018) 1080p.mkv", "10x10", 2018),
            ("10x10.2018.1080p.mkv", "10x10", 2018),
            ("S1m0ne (2002) 720p.mkv", "S1m0ne", 2002),
            ("Se7en (1995) 1080p.mkv", "Se7en", 1995),
            ("2001 A Space Odyssey (1968).mkv", "2001 A Space Odyssey", 1968),
            ("Blade Runner 2049 (2017) 1080p.mkv", "Blade Runner 2049", 2017),
            ("The Four Seasons (1981) 720p.mkv", "The Four Seasons", 1981),
            ("Season of the Witch (2011) 720p.mkv", "Season of the Witch", 2011),
            ("Star Wars Episode III (2005) 1080p.mkv", "Star Wars Episode III", 2005),
            ("Dune Part Two (2024) 1080p.mkv", "Dune Part Two", 2024),
            ("Ona (2019) 1080p.mkv", "Ona", 2019),
            ("Episode (2020) 720p.mkv", "Episode", 2020),
        ]
        for raw, title, year in movies:
            p = parse_filename(raw)
            self.assertEqual(p.kind, "movie", raw)
            self.assertEqual(p.title, title, raw)
            self.assertEqual(p.year, year, raw)


class FastReparseAndMongoTests(unittest.IsolatedAsyncioTestCase):
    async def test_mongo_keyset_pagination_single_rebuild_and_zero_file_deletion(self):
        bad_fid = film_id(normalize("A R B G-Aprile"), 1998)
        good_fid = film_id(normalize("Aprile"), 1998)
        aot_fid = film_id(normalize("S4 16 Attack On Titan"), None)

        fake_db = types.SimpleNamespace(
            files=_FakeMongoCollection([
                {"_id": "u1", "f": bad_fid, "c": CHANNEL, "m": 1, "z": 100,
                 "n": "A R B G-Aprile (1998) @R_A_R_B_G-aprile.1998.internal.bdrip.x264-manic.mkv",
                 "r": None, "s": "BDRip", "l": [], "k": "x264", "g": "manic", "t": [], "x": "MKV"},
                {"_id": "u2", "f": good_fid, "c": CHANNEL, "m": 2, "z": 200,
                 "n": "Aprile.1998.1080p.BluRay.x264.mkv",
                 "r": "1080p", "s": "BluRay", "l": [], "k": "x264", "g": None, "t": [], "x": "MKV"},
                {"_id": "u3", "f": aot_fid, "c": CHANNEL, "m": 3, "z": 300,
                 "n": "S4.16. Attack On Titan [720p Dual] @Animax_Industry.mkv",
                 "r": "720p", "s": None, "l": [], "k": None, "g": None, "t": ["Dual Audio"], "x": "MKV"},
            ]),
            films=_FakeMongoCollection([
                {"_id": bad_fid, "t": "A R B G-Aprile", "y": 1998, "n": 1, "h": 5, "a": [], "ts": 10},
                {"_id": good_fid, "t": "Aprile", "y": 1998, "n": 1, "h": 7, "a": [], "ts": 11},
                {"_id": aot_fid, "t": "S4 16 Attack On Titan", "y": None, "n": 1, "h": 2, "a": [], "ts": 12},
            ]),
            skips=_FakeMongoCollection(),
            join_requests=_FakeMongoCollection(),
        )
        store = MongoStore.__new__(MongoStore)
        store.db = fake_db

        # Verify keyset pagination uses _id > last, projects z/x properly, and NEVER uses unindexed $nin
        batches = [b async for b in store.iter_file_batches(batch_size=2)]
        self.assertEqual([len(b) for b in batches], [2, 1])
        self.assertTrue(all("n" not in q for q in fake_db.files.queries))
        self.assertTrue(any("_id" in q and "$gt" in q["_id"] for q in fake_db.files.queries))
        self.assertEqual((batches[0][1].size, batches[0][1].ext), (200, "MKV"))

        # Test batch write omits films & never overwrites byte size z, then rebuild_films updates once & deletes 0-file films
        u1 = FileRec("u1", good_fid, CHANNEL, 1, 0, None, "BDRip", (),
                     "A R B G-Aprile (1998) @R_A_R_B_G-aprile.1998.internal.bdrip.x264-manic.mkv", "x264", "manic", (), "MKV")
        await store.bulk_reparse_files([u1])
        self.assertEqual(fake_db.files.docs["u1"]["z"], 100)
        self.assertEqual(fake_db.films.bulk_writes, 0)
        await store.bulk_remove_files(["u3"])
        rb = await store.rebuild_films({good_fid: ("Aprile", 1998)})

        self.assertEqual(rb, {"films": 1, "removed_empty": 2})
        self.assertIn(good_fid, fake_db.films.docs)
        self.assertEqual(fake_db.films.docs[good_fid]["n"], 2)
        self.assertEqual(fake_db.films.docs[good_fid]["h"], 7)  # preserves existing hits
        self.assertNotIn(bad_fid, fake_db.films.docs)
        self.assertNotIn(aot_fid, fake_db.films.docs)

    async def test_addpromo_delpromo_and_reparse_strips_custom_promo(self):
        store = MemoryStore()
        pfm_fid = film_id(normalize("PFM Inception"), 2010)
        inc_fid = film_id(normalize("Inception"), 2010)
        store.films = {pfm_fid: {"t": "PFM Inception", "y": 2010, "n": 1, "h": 2, "a": [], "ts": 1}}
        store.files = {
            "p1": FileRec("p1", pfm_fid, CHANNEL, 9, 555, "1080p", None, (),
                          "PFM_Inception.2010.1080p.mkv", None, None, (), "MKV"),
        }
        idx = FilmIndex()
        idx.add_films([("PFM Inception", 2010, 1, 2, ())])
        ctx = _ctx(store, idx)

        m_add = _Msg(text="/addpromo PFM", uid=ADMIN, chat=ADMIN, command=["addpromo", "PFM"])
        await H.on_admin_cmd(ctx, MagicMock(), m_add)
        self.assertEqual((await ctx.settings())["custom_promos"], ["PFM"])

        m_rep = _Msg(text="/reparse", uid=ADMIN, chat=ADMIN, command=["reparse"])
        await H.on_admin_cmd(ctx, MagicMock(), m_rep)
        self.assertEqual((await store.get_file("p1")).film, inc_fid)
        self.assertNotIn(pfm_fid, store.films)
        self.assertEqual(store.films[inc_fid]["t"], "Inception")

        m_del = _Msg(text="/delpromo PFM", uid=ADMIN, chat=ADMIN, command=["delpromo", "PFM"])
        await H.on_admin_cmd(ctx, MagicMock(), m_del)
        self.assertEqual((await ctx.settings())["custom_promos"], [])

    async def test_reparse_cmd_reassigns_files_moves_series_to_skips_and_deletes_empty_films(self):
        store = MemoryStore()
        bad_fid = film_id(normalize("A R B G-Aprile"), 1998)
        good_fid = film_id(normalize("Aprile"), 1998)
        aot_fid = film_id(normalize("S4 16 Attack On Titan"), None)

        store.films = {
            bad_fid: {"t": "A R B G-Aprile", "y": 1998, "n": 1, "h": 3, "a": [], "ts": 1},
            good_fid: {"t": "Aprile", "y": 1998, "n": 1, "h": 5, "a": [], "ts": 2},
            aot_fid: {"t": "S4 16 Attack On Titan", "y": None, "n": 1, "h": 1, "a": [], "ts": 3},
        }
        store.files = {
            "u1": FileRec("u1", bad_fid, CHANNEL, 1, 100, None, "BDRip", (),
                          "A R B G-Aprile (1998) @R_A_R_B_G-aprile.1998.internal.bdrip.x264-manic.mkv",
                          "x264", "manic", (), "MKV"),
            "u2": FileRec("u2", good_fid, CHANNEL, 2, 200, "1080p", "BluRay", (),
                          "Aprile.1998.1080p.BluRay.x264.mkv", "x264", None, (), "MKV"),
            "u3": FileRec("u3", aot_fid, CHANNEL, 3, 300, "720p", None, (),
                          "S4.16. Attack On Titan [720p Dual] @Animax_Industry.mkv", None, None, (), "MKV"),
        }
        idx = FilmIndex()
        idx.add_films([(d["t"], d["y"], d["n"], d["h"], ()) for d in store.films.values()])
        ctx = _ctx(store, idx)

        m = _Msg(text="/reparse", uid=ADMIN, chat=ADMIN, command=["reparse"])
        await H.on_admin_cmd(ctx, MagicMock(), m)

        self.assertIsNone(await store.get_file("u3"))
        self.assertEqual((await store.get_file("u1")).film, good_fid)
        self.assertEqual(len(store.films), 1)
        self.assertEqual(store.films[good_fid]["n"], 2)
        self.assertNotIn(bad_fid, store.films)
        self.assertNotIn(aot_fid, store.films)
        self.assertIn("u3", {r["uid"] for r in await store.skips_all("series")})
        self.assertEqual(ctx.index.search("aprile").hits[0].film.file_count, 2)
        self.assertIsNone(ctx.index.get(bad_fid))
        self.assertIsNone(ctx.index.get(aot_fid))


class GroupDonationCuratedTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_delivery_returning_vs_new_user_and_owner_only_nav(self):
        store = MemoryStore()
        idx = FilmIndex()
        ctx = _ctx(store, idx)
        await ctx.set_setting("force_subs", [{"chat": -100999, "link": "https://t.me/chan", "title": "Chan"}])

        fid1 = film_id("dune", 2021)
        fid2 = film_id("dune", 1984)
        await store.add_files([
            FileRec("f1", fid1, CHANNEL, 10, 1_000_000, "1080p", "BluRay", (), "Dune.2021.1080p.BluRay.mkv", "x265", None, (), "MKV"),
            FileRec("f2", fid2, CHANNEL, 11, 900_000, "720p", "WEBRip", (), "Dune.1984.720p.WEBRip.mkv", "x264", None, (), "MKV"),
        ], {fid1: ("Dune", 2021), fid2: ("Dune", 1984)})
        idx.add_films([("Dune", 2021, 1, 0, ()), ("Dune", 1984, 1, 0, ())])

        client = MagicMock()
        src1 = _SrcMsg(CHANNEL, 10, "f1", "Dune.2021.1080p.BluRay.mkv", fail_uids={3003})
        client.get_messages = AsyncMock(return_value=src1)
        client.send_message = AsyncMock()

        async def get_member(cid, uid):
            if uid == OTHER:
                return types.SimpleNamespace(status=ChatMemberStatus.LEFT)
            if uid == 4004:
                raise UserNotParticipant()
            return types.SimpleNamespace(status=ChatMemberStatus.MEMBER)

        client.get_chat_member = AsyncMock(side_effect=get_member)

        # Search in group by OWNER
        msg = _Msg(text="dune 2021", uid=OWNER, chat=GROUP, ctype=ChatType.GROUP)
        await H.on_text(ctx, client, msg)
        res_msg = msg.sink[-1]

        # 1) OTHER user with pending join request even when get_chat_member returns LEFT receives file directly!
        ctx.note_join_request(OTHER, -100999)
        cb_other = types.SimpleNamespace(from_user=_user(OTHER, "Other"), message=res_msg, data="s:0", answer=AsyncMock())
        await H.on_cb(ctx, client, cb_other)
        self.assertEqual(cb_other.answer.await_args.args[0], "Sent to your private messages 📬")
        self.assertEqual(src1.copied[-1], OTHER)

        # 2) Returning member 3003 who has NOT started bot in DM gets deep link
        cb_unstarted = types.SimpleNamespace(from_user=_user(3003), message=res_msg, data="s:0", answer=AsyncMock())
        await H.on_cb(ctx, client, cb_unstarted)
        self.assertIn("t.me/filmbot?start=f_f1", cb_unstarted.answer.await_args.kwargs.get("url", ""))

        # 3) Unjoined user 4004 gets deep link; after starting bot, force-join screen appears and delivers on join
        cb_unjoined = types.SimpleNamespace(from_user=_user(4004), message=res_msg, data="s:0", answer=AsyncMock())
        await H.on_cb(ctx, client, cb_unjoined)
        self.assertIn("t.me/filmbot?start=f_f1", cb_unjoined.answer.await_args.kwargs.get("url", ""))

        # 4) OTHER user cannot navigate Next/Prev/Back on OWNER's group results, while owner can close
        for nav_data in ("r:1", "v:1", "b"):
            cb_nav = types.SimpleNamespace(from_user=_user(OTHER), message=res_msg, data=nav_data, answer=AsyncMock())
            await H.on_cb(ctx, client, cb_nav)
            self.assertIn("navigate", cb_nav.answer.await_args.args[0].lower())
        cb_dx = types.SimpleNamespace(from_user=_user(OTHER), message=res_msg, data="dx", answer=AsyncMock())
        await H.on_cb(ctx, client, cb_dx)
        self.assertFalse(res_msg.deleted)

        # Owner can close
        cb_owner_dx = types.SimpleNamespace(from_user=_user(OWNER), message=res_msg, data="dx", answer=AsyncMock())
        await H.on_cb(ctx, client, cb_owner_dx)
        self.assertTrue(res_msg.deleted)

    async def test_donate_contact_fallback_and_setcontact(self):
        store = MemoryStore()
        idx = FilmIndex()
        ctx = _ctx(store, idx)

        # Default fallback links to first admin
        m_don = _Msg(text="/donate", uid=OWNER, chat=OWNER, command=["donate"])
        await H.cmd_info(ctx, MagicMock(), m_don)
        urls = [b.url for b in m_don.sink[-1].buttons() if b.url]
        self.assertIn(f"tg://user?id={ADMIN}", urls)

        # Admin sets custom contact handle via /setcontact
        m_set = _Msg(text="/setcontact @martin_help", uid=ADMIN, chat=ADMIN, command=["setcontact", "@martin_help"])
        await H.on_admin_cmd(ctx, MagicMock(), m_set)
        m_don2 = _Msg(text="/donate", uid=OWNER, chat=OWNER, command=["donate"])
        await H.cmd_info(ctx, MagicMock(), m_don2)
        urls2 = [b.url for b in m_don2.sink[-1].buttons() if b.url]
        self.assertIn("https://t.me/martin_help", urls2)

        # /setcontact clear resets back to admin account
        m_clr = _Msg(text="/setcontact clear", uid=ADMIN, chat=ADMIN, command=["setcontact", "clear"])
        await H.on_admin_cmd(ctx, MagicMock(), m_clr)
        self.assertEqual(ui.contact_link(await ctx.settings(), ADMIN), f"tg://user?id={ADMIN}")

    async def test_curated_lists_management_and_discover_preserves_handpicked(self):
        store = MemoryStore()
        idx = FilmIndex()
        f1 = idx.add_film("Aprile", 1998, file_count=1)
        f2 = idx.add_film("Dune", 2021, file_count=1)
        ctx = _ctx(store, idx)

        # Hand-picked preserved
        await ctx.set_setting("picks", [f1.id])
        await ctx.set_setting("picks_name", "Hand-picked")

        # Admin opens settings and clicks 📚 Curated Lists -> cur:create -> sends name
        m_st = _Msg(text="/settings", uid=ADMIN, chat=ADMIN, command=["settings"])
        await H.on_admin_cmd(ctx, MagicMock(), m_st)
        st_msg = m_st.sink[-1]
        self.assertTrue(any(b.callback_data == "st:curated" for b in st_msg.buttons()))

        await H.on_cb(ctx, MagicMock(), types.SimpleNamespace(from_user=_user(ADMIN), message=st_msg, data="st:curated", answer=AsyncMock()))
        await H.on_cb(ctx, MagicMock(), types.SimpleNamespace(from_user=_user(ADMIN), message=st_msg, data="cur:create", answer=AsyncMock()))
        await H.on_text(ctx, MagicMock(), _Msg(text="Weekend Picks", uid=ADMIN, chat=ADMIN))

        lists = H.get_curated_lists(await ctx.settings())
        self.assertEqual(len(lists), 1)
        lid = lists[0]["id"]

        # Add films interactively via search + toggle
        await H.on_cb(ctx, MagicMock(), types.SimpleNamespace(from_user=_user(ADMIN), message=st_msg, data=f"cur:add:{lid}", answer=AsyncMock()))
        m_q = _Msg(text="dune", uid=ADMIN, chat=ADMIN)
        await H.on_text(ctx, MagicMock(), m_q)
        await H.on_cb(ctx, MagicMock(), types.SimpleNamespace(from_user=_user(ADMIN), message=m_q, data=f"cur:tog:{lid}:{f2.id}", answer=AsyncMock()))

        # Rename
        await H.on_cb(ctx, MagicMock(), types.SimpleNamespace(from_user=_user(ADMIN), message=st_msg, data=f"cur:ren:{lid}", answer=AsyncMock()))
        await H.on_text(ctx, MagicMock(), _Msg(text="Sci-Fi Gems", uid=ADMIN, chat=ADMIN))
        lists = H.get_curated_lists(await ctx.settings())
        self.assertEqual(lists[0]["name"], "Sci-Fi Gems")
        self.assertEqual(lists[0]["fids"], [f2.id])

        # Discover page shows BOTH Hand-picked and Sci-Fi Gems
        m_dc = _Msg(text="/discover", uid=OWNER, chat=OWNER, command=["discover"])
        await H.cmd_info(ctx, MagicMock(), m_dc)
        dc_msg = m_dc.sink[-1]
        dc_datas = [b.callback_data for b in dc_msg.buttons()]
        self.assertIn("dl:pick", dc_datas)
        self.assertIn("sur", dc_datas)
        self.assertIn(f"dl:cur:{lid}", dc_datas)

        # User taps curated list on Discover and sees Dune (2021)
        await H.on_cb(ctx, MagicMock(), types.SimpleNamespace(from_user=_user(OWNER), message=dc_msg, data=f"dl:cur:{lid}", answer=AsyncMock()))
        self.assertIn("Sci-Fi Gems", dc_msg.text)
        self.assertTrue(any(b.callback_data == f"f:{f2.id}" for b in dc_msg.buttons()))
        # Original Hand-picked untouched
        self.assertEqual((await ctx.settings())["picks"], [f1.id])

        # Replace (pipe-separated), Remove, Clear, Delete
        await H.on_cb(ctx, MagicMock(), types.SimpleNamespace(from_user=_user(ADMIN), message=st_msg, data=f"cur:rep:{lid}", answer=AsyncMock()))
        await H.on_text(ctx, MagicMock(), _Msg(text="Aprile | Dune", uid=ADMIN, chat=ADMIN))
        self.assertEqual(H.get_curated_lists(await ctx.settings())[0]["fids"], [f1.id, f2.id])

        await H.on_cb(ctx, MagicMock(), types.SimpleNamespace(from_user=_user(ADMIN), message=st_msg, data=f"cur:df:{lid}:{f1.id}:0", answer=AsyncMock()))
        self.assertEqual(H.get_curated_lists(await ctx.settings())[0]["fids"], [f2.id])

        await H.on_cb(ctx, MagicMock(), types.SimpleNamespace(from_user=_user(ADMIN), message=st_msg, data=f"cur:clrok:{lid}", answer=AsyncMock()))
        self.assertEqual(H.get_curated_lists(await ctx.settings())[0]["fids"], [])

        await H.on_cb(ctx, MagicMock(), types.SimpleNamespace(from_user=_user(ADMIN), message=st_msg, data=f"cur:delok:{lid}", answer=AsyncMock()))
        self.assertEqual(H.get_curated_lists(await ctx.settings()), [])


if __name__ == "__main__":
    unittest.main()
