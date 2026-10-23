"""Persistent data. MongoStore talks to Atlas; MemoryStore is the same API in
plain dicts, used by the tests (and handy for a dry run)."""
from __future__ import annotations

import time
from datetime import date, timedelta
from dataclasses import dataclass
from typing import AsyncIterator

from parse import SUB_EXTS

DEFAULTS = {
    "force_sub": None, "force_subs": [], "auto_approve": True, "autodelete_min": 10, "result_ttl_min": 4, "protect": False, "groups": True,
    "group_mention_only": False, "group_max_len": 60, "group_silent_miss": True,
    "welcome": None, "about": None, "image": None, "page_images": {}, "links": [], "footer": "Join: @exivum",
    "picks": [], "picks_name": "Hand-picked", "index_series": False,
    "series_bot": "Exivum_Series_Bot", "series_group": None, "summary_time": "08:00", "summary_last": None,
    "donate_methods": [], "stars_enabled": True, "survey_active": True,
}


DEDUPE_BATCH = 50000      # /dedupe works in rounds this size so a huge clean-up can't fill the 512 MB of RAM
SKIP_CAP = 20000          # most records kept per skipped-file list (oldest are dropped)
SKIP_KINDS = ("unreadable", "series", "lookalike")


@dataclass
class FileRec:
    uid: str                      # Telegram file_unique_id: same file => same id, forever
    film: str                     # film id (see index.film_id)
    ch: int                       # channel the file lives in
    mid: int                      # message id in that channel (delivery = copy)
    size: int = 0
    res: str | None = None
    src: str | None = None
    langs: tuple[str, ...] = ()
    name: str = ""
    codec: str | None = None
    grp: str | None = None        # encoder / uploader (best guess from the filename)
    tags: tuple[str, ...] = ()
    ext: str | None = None


def _meta(r: FileRec) -> dict:
    return {"z": r.size, "r": r.res, "s": r.src, "l": list(r.langs), "n": r.name,
            "k": r.codec, "g": r.grp, "t": list(r.tags), "x": r.ext}


def _doc(r: FileRec) -> dict:
    return {"f": r.film, "c": r.ch, "m": r.mid, **_meta(r)}


def _rec(d: dict) -> FileRec:
    return FileRec(d["_id"], d["f"], d["c"], d["m"], d.get("z", 0), d.get("r"), d.get("s"),
                   tuple(d.get("l", ())), d.get("n", ""), d.get("k"), d.get("g"),
                   tuple(d.get("t", ())), d.get("x"))


def _today() -> str:
    return date.today().isoformat()


def dupe_key(film: str, size: int, ext: str | None, langs) -> tuple:
    """Same film + exact same byte size = the same file. Subtitles also need the same language, so two
    different-language subtitle files that happen to be the same size are never mistaken for twins."""
    return (film, size, tuple(langs) if (ext or "").lower() in SUB_EXTS else None)


class MemoryStore:
    def __init__(self) -> None:
        self.files: dict[str, FileRec] = {}
        self.films: dict[str, dict] = {}
        self.users: dict[int, dict] = {}
        self.reqs: dict[str, dict] = {}
        self.miss: dict[str, dict] = {}
        self.cfg: dict = {}
        self.daily: dict[tuple[str, str], int] = {}
        self.dels: list[dict] = []
        self.reports: dict[str, dict] = {}
        self.skipped: dict[str, dict] = {}
        self.daily_stats: dict[str, dict] = {}
        self.survey_totals: dict[str, dict] = {}
        self._clock = 0

    async def setup(self) -> None: ...
    async def ping(self) -> bool: return True

    async def iter_films(self) -> AsyncIterator[dict]:
        for fid, d in list(self.films.items()):
            yield {"fid": fid, "title": d["t"], "year": d["y"], "n": d["n"], "hits": d["h"],
                   "aliases": list(d["a"])}

    async def add_files(self, recs: list[FileRec], films: dict[str, tuple]) -> tuple[list[FileRec], int, list]:
        """(newly added, how many we already had, [(look-alike record, uid of the copy we kept)])"""
        new, known, look, seen = [], 0, [], {}
        have: dict[tuple, str] = {}
        for r in self.files.values():
            if r.size:
                have.setdefault(dupe_key(r.film, r.size, r.ext, r.langs), r.uid)
        for r in recs:
            if r.uid in self.files:
                known += 1
                old = self.files[r.uid]
                for k in ("size", "res", "src", "langs", "name", "codec", "grp", "tags", "ext"):
                    setattr(old, k, getattr(r, k))
                continue
            key = dupe_key(r.film, r.size, r.ext, r.langs)
            if r.size and (key in have or key in seen):
                look.append((r, have.get(key) or seen[key]))
                continue
            if r.size:
                seen[key] = r.uid
            self.files[r.uid] = r
            new.append(r)
        for r in new:
            self._clock += 1
            d = self.films.setdefault(r.film, {"t": films[r.film][0], "y": films[r.film][1],
                                               "n": 0, "h": 0, "a": [], "ts": self._clock})
            d["n"] += 1
        return new, known, look

    async def film_files(self, fid: str) -> list[FileRec]:
        return [r for r in self.files.values() if r.film == fid]

    async def get_file(self, uid: str) -> FileRec | None:
        return self.files.get(uid)

    async def remove_file(self, uid: str) -> FileRec | None:
        r = self.files.pop(uid, None)
        if r and r.film in self.films:
            self.films[r.film]["n"] -= 1
        return r

    async def lookalikes(self, limit: int = DEDUPE_BATCH) -> list[dict]:
        """Extra copies of the same film + exact size (the first one found is the copy to keep)."""
        first: dict[tuple, str] = {}
        out = []
        for r in sorted(self.files.values(), key=lambda r: (r.film, r.size, r.uid)):
            if not r.size:
                continue
            key = dupe_key(r.film, r.size, r.ext, r.langs)
            if key in first:
                if len(out) >= limit:
                    break
                out.append({"uid": r.uid, "film": r.film, "size": r.size, "name": r.name, "ch": r.ch,
                            "mid": r.mid, "keep": first[key]})
            else:
                first[key] = r.uid
        return out

    async def delete_files(self, items: list[dict], kind: str = "lookalike") -> int:
        await self.note_skips([{"kind": kind, "uid": i["uid"], "name": i["name"], "ch": i["ch"], "mid": i["mid"],
                                "size": i["size"], "keep": i.get("keep")} for i in items])
        n = 0
        for i in items:
            if await self.remove_file(i["uid"]):
                n += 1
        return n

    async def iter_file_names(self) -> AsyncIterator[dict]:
        for r in list(self.files.values()):
            if r.name:
                yield {"uid": r.uid, "film": r.film, "name": r.name, "ch": r.ch, "mid": r.mid, "size": r.size}

    async def remove_film_if_empty(self, fid: str) -> bool:
        if any(r.film == fid for r in self.files.values()):
            return False
        return self.films.pop(fid, None) is not None

    async def files_by_uid(self, uids: list[str]) -> dict[str, FileRec]:
        return {u: self.files[u] for u in uids if u in self.files}

    async def bump_hit(self, fid: str) -> None:
        if fid in self.films:
            self.films[fid]["h"] += 1
        k = (_today(), fid)
        self.daily[k] = self.daily.get(k, 0) + 1

    async def top_films(self, days: int = 7, limit: int = 30) -> list[tuple[str, int]]:
        since = (date.today() - timedelta(days=days)).isoformat()
        agg: dict[str, int] = {}
        for (d, f), n in self.daily.items():
            if d >= since:
                agg[f] = agg.get(f, 0) + n
        return sorted(agg.items(), key=lambda x: -x[1])[:limit]

    async def recent_films(self, limit: int = 30) -> list[str]:
        rows = sorted(((d["ts"], f) for f, d in self.films.items()), reverse=True)
        return [f for _, f in rows[:limit]]

    async def prune_daily(self, keep_days: int = 30) -> None:
        cut = (date.today() - timedelta(days=keep_days)).isoformat()
        self.daily = {k: v for k, v in self.daily.items() if k[0] >= cut}

    async def add_alias(self, fid: str, alias: str) -> None:
        if fid in self.films and alias not in self.films[fid]["a"]:
            self.films[fid]["a"].append(alias)

    async def settings(self) -> dict:
        return {**DEFAULTS, **self.cfg}

    async def set_setting(self, key: str, value) -> None:
        self.cfg[key] = value

    async def touch_user(self, uid: int, day: str) -> tuple[bool, bool]:
        """(first time we've ever seen them, first time today). One cheap write per person per day."""
        new = uid not in self.users
        u = self.users.setdefault(uid, {})
        first_today = new or u.get("ls") != day
        u["ls"] = day
        return new, first_today

    async def get_user(self, uid: int) -> dict:
        return dict(self.users.get(uid, {}))

    async def set_watchlist(self, uid: int, ids: list[str]) -> None:
        self.users.setdefault(uid, {})["wl"] = list(ids)

    async def add_search_counts(self, deltas: dict[int, int]) -> None:
        for uid, n in deltas.items():
            u = self.users.setdefault(uid, {})
            u["sc"] = u.get("sc", 0) + n

    async def stats_add(self, day: str, deltas: dict) -> None:
        d = self.daily_stats.setdefault(day, {})
        for k, v in deltas.items():
            d[k] = d.get(k, 0) + v

    async def stats_get(self, day: str) -> dict:
        return dict(self.daily_stats.get(day, {}))

    async def user_requests(self, uid: int) -> list[dict]:
        return [{"key": k, "text": d["t"]} for k, d in self.reqs.items() if d["open"] and uid in d["u"]]

    async def cancel_request(self, key: str, uid: int) -> None:
        d = self.reqs.get(key)
        if d and uid in d["u"]:
            d["u"].remove(uid)
            if not d["u"]:
                d["open"] = False

    async def banned_ids(self) -> set[int]:
        return {u for u, d in self.users.items() if d.get("b")}

    async def set_banned(self, uid: int, flag: bool) -> None:
        self.users.setdefault(uid, {})["b"] = flag

    async def add_request(self, qkey: str, text: str, uid: int) -> tuple[int, bool, bool]:
        """(people asking, is this a brand-new request, did this person just add themselves)"""
        d = self.reqs.get(qkey)
        if d is None or not d["open"]:
            self.reqs[qkey] = {"t": text, "u": [uid], "open": True}
            return 1, True, True
        added = uid not in d["u"]
        if added:
            d["u"].append(uid)
        return len(d["u"]), False, added

    async def open_requests(self, limit: int = 50) -> list[dict]:
        rows = [{"key": k, "text": d["t"], "users": list(d["u"])} for k, d in self.reqs.items() if d["open"]]
        return sorted(rows, key=lambda r: -len(r["users"]))[:limit]

    async def close_request(self, qkey: str) -> None:
        if qkey in self.reqs:
            self.reqs[qkey]["open"] = False

    async def get_request(self, qkey: str) -> dict | None:
        d = self.reqs.get(qkey)
        return {"key": qkey, "text": d["t"], "users": list(d["u"]), "open": d["open"]} if d else None

    async def delete_request(self, qkey: str) -> bool:
        return bool(self.reqs.pop(qkey, None))

    async def add_miss(self, qkey: str) -> None:
        self.miss.setdefault(qkey, {"n": 0})["n"] += 1

    async def top_misses(self, limit: int = 15) -> list[tuple[str, int]]:
        return sorted(((k, d["n"]) for k, d in self.miss.items()), key=lambda x: -x[1])[:limit]

    async def schedule_delete(self, chat: int, mid: int, at: float) -> None:
        self.dels.append({"c": chat, "m": mid, "at": at})

    async def due_deletes(self, now: float, limit: int = 100) -> list[tuple[object, int, int]]:
        return [(d, d["c"], d["m"]) for d in self.dels if d["at"] <= now][:limit]

    async def clear_deletes(self, keys: list) -> None:
        self.dels = [d for d in self.dels if not any(d is k for k in keys)]

    async def add_report(self, uid: str, code: str, user: int) -> tuple[int, dict, bool]:
        """(distinct reporters, reason counts, was this reporter new?)"""
        d = self.reports.setdefault(uid, {"u": [], "c": {}})
        new = user not in d["u"]
        if new:
            d["u"].append(user)
            d["c"][code] = d["c"].get(code, 0) + 1
        return len(d["u"]), dict(d["c"]), new

    async def clear_reports(self, uid: str) -> None:
        self.reports.pop(uid, None)

    async def note_skips(self, items: list[dict]) -> None:
        for i in items:
            self._clock += 1
            self.skipped[i["uid"]] = {"k": i["kind"], "n": (i.get("name") or "")[:100], "c": i["ch"], "m": i["mid"],
                                      "z": i.get("size", 0), "keep": i.get("keep"), "t": self._clock}

    async def clear_skips(self, uids: list[str]) -> None:
        for u in uids:
            self.skipped.pop(u, None)

    async def skip_counts(self) -> dict[str, int]:
        return {k: sum(1 for d in self.skipped.values() if d["k"] == k) for k in SKIP_KINDS}

    def _skip_rows(self, kind: str) -> list[dict]:
        rows = sorted(((d["t"], u, d) for u, d in self.skipped.items() if d["k"] == kind), reverse=True)
        return [{"uid": u, "n": d["n"], "c": d["c"], "m": d["m"], "z": d["z"], "keep": d["keep"]} for _, u, d in rows]

    async def skips_page(self, kind: str, page: int, size: int = 10) -> tuple[int, list[dict]]:
        rows = self._skip_rows(kind)
        return len(rows), rows[page * size:(page + 1) * size]

    async def skips_all(self, kind: str) -> list[dict]:
        return self._skip_rows(kind)[:SKIP_CAP]

    async def prune_skips(self, cap: int = SKIP_CAP) -> None:
        for kind in SKIP_KINDS:
            rows = self._skip_rows(kind)
            for r in rows[cap:]:
                self.skipped.pop(r["uid"], None)

    async def donor_add(self, uid: int, days: int = 90, name: str = "") -> str:
        u = self.users.setdefault(uid, {})
        curr = u.get("su")
        try:
            base = date.fromisoformat(curr) if curr and curr >= _today() else date.today()
        except ValueError:
            base = date.today()
        new_date = (base + timedelta(days=days)).isoformat()
        u["su"] = new_date
        if name:
            u["name"] = name
        return new_date

    async def donor_remove(self, uid: int) -> bool:
        u = self.users.get(uid)
        if u and u.get("su"):
            u["su"] = None
            return True
        return False

    async def donor_list(self) -> list[dict]:
        today = _today()
        out = []
        for uid, u in self.users.items():
            if u.get("su") and u["su"] >= today:
                out.append({"uid": uid, "su": u["su"], "name": u.get("name", ""), "wall": bool(u.get("wall"))})
        return sorted(out, key=lambda x: x["su"])

    async def set_wall_opt_in(self, uid: int, flag: bool) -> None:
        self.users.setdefault(uid, {})["wall"] = flag

    async def get_supporters_wall(self) -> list[str]:
        today = _today()
        out = []
        for uid, u in self.users.items():
            if u.get("su") and u["su"] >= today and u.get("wall"):
                out.append(u.get("name") or str(uid))
        return out

    async def record_survey(self, uid: int, answers: dict) -> None:
        u = self.users.setdefault(uid, {})
        u["sv"] = True
        for q, ans in answers.items():
            d = self.survey_totals.setdefault(q, {})
            d[ans] = d.get(ans, 0) + 1
        d_tot = self.survey_totals.setdefault("_meta", {})
        d_tot["count"] = d_tot.get("count", 0) + 1
        g, a, c = answers.get("gender"), answers.get("age"), answers.get("country")
        if c and g and a:
            cohorts = self.survey_totals.setdefault("_cohorts", {})
            cohort_key = f"{c} • {g} • {a}"
            cohorts[cohort_key] = cohorts.get(cohort_key, 0) + 1

    async def get_survey_stats(self) -> dict:
        return self.survey_totals

    async def add_claim(self, uid: int, name: str, method: str, note: str = "") -> tuple[bool, str]:
        for cid, c in getattr(self, "claims", {}).items():
            if c["uid"] == uid and c["status"] == "pending":
                return False, cid
        if not hasattr(self, "claims"):
            self.claims = {}
        cid = f"cl_{len(self.claims) + 1}"
        self.claims[cid] = {"id": cid, "uid": uid, "name": name, "method": method, "note": note,
                            "status": "pending", "date": _today()}
        return True, cid

    async def get_claim(self, cid: str) -> dict | None:
        return getattr(self, "claims", {}).get(cid)

    async def resolve_claim(self, cid: str, status: str) -> bool:
        c = await self.get_claim(cid)
        if c:
            c["status"] = status
            return True
        return False

    async def counts(self) -> dict:
        today = _today()
        return {"files": len(self.files), "films": len(self.films), "users": len(self.users),
                "requests": sum(1 for d in self.reqs.values() if d["open"]),
                "supporters": sum(1 for u in self.users.values() if u.get("su") and u["su"] >= today),
                "db_mb": None}


class MongoStore:
    """Atlas-backed store. Needs pymongo >= 4.9 (async client)."""

    def __init__(self, uri: str, db_name: str) -> None:
        from pymongo import AsyncMongoClient
        self.db = AsyncMongoClient(uri, serverSelectionTimeoutMS=20000)[db_name]

    @property
    def cfg(self) -> dict:
        return {}

    async def setup(self) -> None:
        await self.db.files.create_index([("f", 1), ("z", 1)])      # film lookup + same-size check
        try:
            await self.db.files.drop_index("f_1")                    # the compound index covers it
        except Exception:
            pass
        await self.db.files.create_index([("c", 1), ("m", 1)])
        await self.db.requests.create_index("open")
        await self.db.requests.create_index("u")
        await self.db.daily.create_index("d")
        await self.db.films.create_index("ts")
        await self.db.deletions.create_index("at")
        await self.db.skips.create_index([("k", 1), ("t", -1)])

    async def ping(self) -> bool:
        try:
            await self.db.command("ping")
            return True
        except Exception:
            return False

    async def iter_films(self) -> AsyncIterator[dict]:
        async for d in self.db.films.find({}, batch_size=5000):
            yield {"fid": d["_id"], "title": d["t"], "year": d.get("y"), "n": d.get("n", 0),
                   "hits": d.get("h", 0), "aliases": d.get("a", [])}

    async def add_files(self, recs: list[FileRec], films: dict[str, tuple]) -> tuple[list[FileRec], int, list]:
        """(newly added, how many we already had, [(look-alike record, uid of the copy we kept)])"""
        from pymongo import UpdateOne
        if not recs:
            return [], 0, []
        known = {d["_id"] async for d in self.db.files.find({"_id": {"$in": [r.uid for r in recs]}}, {"_id": 1})}
        fresh = [r for r in recs if r.uid not in known]
        existing: dict[tuple, str] = {}
        sized = [r for r in fresh if r.size]
        if sized:
            async for d in self.db.files.find({"f": {"$in": list({r.film for r in sized})},
                                               "z": {"$in": list({r.size for r in sized})}},
                                              {"f": 1, "z": 1, "x": 1, "l": 1}):
                existing.setdefault(dupe_key(d["f"], d["z"], d.get("x"), d.get("l", ())), d["_id"])
        new, look, seen = [], [], {}
        for r in fresh:
            key = dupe_key(r.film, r.size, r.ext, r.langs)
            if r.size and (key in existing or key in seen):
                look.append((r, existing.get(key) or seen[key]))    # same film, same byte size: a re-upload
                continue
            if r.size:
                seen[key] = r.uid
            new.append(r)
        ops = [UpdateOne({"_id": r.uid}, {"$setOnInsert": _doc(r)}, upsert=True) for r in new]
        ops += [UpdateOne({"_id": r.uid}, {"$set": _meta(r)}) for r in recs if r.uid in known]   # refresh details
        added: list[FileRec] = []
        if ops:
            res = await self.db.files.bulk_write(ops, ordered=False)
            added = [new[i] for i in sorted(res.upserted_ids)]
        per_film: dict[str, int] = {}
        for r in added:
            per_film[r.film] = per_film.get(r.film, 0) + 1
        if per_film:
            now = int(time.time())
            await self.db.films.bulk_write([
                UpdateOne({"_id": f}, {"$setOnInsert": {"t": films[f][0], "y": films[f][1], "h": 0, "a": [], "ts": now},
                                       "$inc": {"n": n}}, upsert=True)
                for f, n in per_film.items()], ordered=False)
        return added, len(known), look

    async def film_files(self, fid: str) -> list[FileRec]:
        return [_rec(d) async for d in self.db.files.find({"f": fid})]

    async def get_file(self, uid: str) -> FileRec | None:
        d = await self.db.files.find_one({"_id": uid})
        return _rec(d) if d else None

    async def remove_file(self, uid: str) -> FileRec | None:
        d = await self.db.files.find_one_and_delete({"_id": uid})
        if not d:
            return None
        await self.db.films.update_one({"_id": d["f"]}, {"$inc": {"n": -1}})
        return _rec(d)

    async def lookalikes(self, limit: int = DEDUPE_BATCH) -> list[dict]:
        """Extra copies of the same film + exact size, up to `limit` per round. Streams the collection
        in index order and only holds one (film, size) group at a time, so memory stays small."""
        out: list[dict] = []
        group: list[dict] = []
        gkey = None

        def flush() -> None:
            first: dict[tuple, str] = {}
            for d in group:
                k = dupe_key(d["f"], d["z"], d.get("x"), d.get("l", ()))
                if k in first:
                    out.append({"uid": d["_id"], "film": d["f"], "size": d["z"], "name": d.get("n", ""),
                                "ch": d.get("c", 0), "mid": d.get("m", 0), "keep": first[k]})
                else:
                    first[k] = d["_id"]
        cur = self.db.files.find({"z": {"$gt": 0}}, {"f": 1, "z": 1, "n": 1, "c": 1, "m": 1, "x": 1, "l": 1}) \
            .sort([("f", 1), ("z", 1)])
        async for d in cur:
            key = (d["f"], d["z"])
            if key != gkey:
                flush()
                if len(out) >= limit:
                    return out[:limit]
                group, gkey = [], key
            group.append(d)
        flush()
        return out[:limit]

    async def delete_files(self, items: list[dict], kind: str = "lookalike") -> int:
        from pymongo import UpdateOne
        await self.note_skips([{"kind": kind, "uid": i["uid"], "name": i["name"], "ch": i["ch"], "mid": i["mid"],
                                "size": i["size"], "keep": i.get("keep")} for i in items])
        total, per_film = 0, {}
        for i in range(0, len(items), 1000):
            chunk = items[i:i + 1000]
            res = await self.db.files.delete_many({"_id": {"$in": [x["uid"] for x in chunk]}})
            total += res.deleted_count
            for x in chunk:
                per_film[x["film"]] = per_film.get(x["film"], 0) + 1
        if per_film:
            await self.db.films.bulk_write([UpdateOne({"_id": f}, {"$inc": {"n": -n}}) for f, n in per_film.items()],
                                           ordered=False)
        return total

    async def iter_file_names(self) -> AsyncIterator[dict]:
        async for d in self.db.files.find({"n": {"$nin": ["", None]}}, {"n": 1, "f": 1, "c": 1, "m": 1, "z": 1},
                                          batch_size=2000):
            yield {"uid": d["_id"], "film": d["f"], "name": d["n"], "ch": d.get("c", 0), "mid": d.get("m", 0),
                   "size": d.get("z", 0)}

    async def remove_film_if_empty(self, fid: str) -> bool:
        if await self.db.files.find_one({"f": fid}, {"_id": 1}):
            return False
        res = await self.db.films.delete_one({"_id": fid})
        return res.deleted_count > 0

    async def files_by_uid(self, uids: list[str]) -> dict[str, FileRec]:
        out: dict[str, FileRec] = {}
        for i in range(0, len(uids), 500):
            async for d in self.db.files.find({"_id": {"$in": uids[i:i + 500]}}):
                out[d["_id"]] = _rec(d)
        return out

    async def bump_hit(self, fid: str) -> None:
        day = _today()
        await self.db.films.update_one({"_id": fid}, {"$inc": {"h": 1}})
        await self.db.daily.update_one({"_id": f"{day}:{fid}"}, {"$inc": {"n": 1}, "$setOnInsert": {"d": day, "f": fid}},
                                       upsert=True)

    async def top_films(self, days: int = 7, limit: int = 30) -> list[tuple[str, int]]:
        since = (date.today() - timedelta(days=days)).isoformat()
        cur = await self.db.daily.aggregate([{"$match": {"d": {"$gte": since}}},
                                             {"$group": {"_id": "$f", "n": {"$sum": "$n"}}},
                                             {"$sort": {"n": -1}}, {"$limit": limit}])
        return [(d["_id"], d["n"]) async for d in cur]

    async def recent_films(self, limit: int = 30) -> list[str]:
        return [d["_id"] async for d in self.db.films.find({"ts": {"$exists": True}}, {"_id": 1})
                .sort("ts", -1).limit(limit)]

    async def prune_daily(self, keep_days: int = 30) -> None:
        await self.db.daily.delete_many({"d": {"$lt": (date.today() - timedelta(days=keep_days)).isoformat()}})

    async def add_alias(self, fid: str, alias: str) -> None:
        await self.db.films.update_one({"_id": fid}, {"$addToSet": {"a": alias}})

    async def settings(self) -> dict:
        return {**DEFAULTS, **(await self.db.settings.find_one({"_id": "main"}) or {})}

    async def set_setting(self, key: str, value) -> None:
        await self.db.settings.update_one({"_id": "main"}, {"$set": {key: value}}, upsert=True)

    async def touch_user(self, uid: int, day: str) -> tuple[bool, bool]:
        """(first time we've ever seen them, first time today). One cheap write per person per day."""
        res = await self.db.users.update_one({"_id": uid}, {"$set": {"ls": day}, "$setOnInsert": {"t": int(time.time())}},
                                             upsert=True)
        new = res.upserted_id is not None
        return new, (new or res.modified_count == 1)

    async def get_user(self, uid: int) -> dict:
        return await self.db.users.find_one({"_id": uid}) or {}

    async def set_watchlist(self, uid: int, ids: list[str]) -> None:
        await self.db.users.update_one({"_id": uid}, {"$set": {"wl": ids}}, upsert=True)

    async def add_search_counts(self, deltas: dict[int, int]) -> None:
        from pymongo import UpdateOne
        if deltas:
            await self.db.users.bulk_write([UpdateOne({"_id": u}, {"$inc": {"sc": n}}, upsert=True)
                                            for u, n in deltas.items()], ordered=False)

    async def stats_add(self, day: str, deltas: dict) -> None:
        if deltas:
            await self.db.stats.update_one({"_id": day}, {"$inc": deltas}, upsert=True)

    async def stats_get(self, day: str) -> dict:
        d = await self.db.stats.find_one({"_id": day}) or {}
        return {k: v for k, v in d.items() if k != "_id"}

    async def user_requests(self, uid: int) -> list[dict]:
        return [{"key": d["_id"], "text": d["t"]} async for d in self.db.requests.find({"u": uid, "open": True})]

    async def cancel_request(self, key: str, uid: int) -> None:
        await self.db.requests.update_one({"_id": key}, {"$pull": {"u": uid}})
        d = await self.db.requests.find_one({"_id": key})
        if d is not None and not d.get("u"):
            await self.db.requests.update_one({"_id": key}, {"$set": {"open": False}})

    async def banned_ids(self) -> set[int]:
        return {d["_id"] async for d in self.db.users.find({"b": True}, {"_id": 1})}

    async def set_banned(self, uid: int, flag: bool) -> None:
        await self.db.users.update_one({"_id": uid}, {"$set": {"b": flag}}, upsert=True)

    async def add_request(self, qkey: str, text: str, uid: int) -> tuple[int, bool, bool]:
        """(people asking, is this a brand-new request, did this person just add themselves)"""
        d = await self.db.requests.find_one({"_id": qkey})
        if d is None or not d.get("open"):
            await self.db.requests.replace_one({"_id": qkey}, {"t": text, "u": [uid], "open": True,
                                                               "ts": int(time.time())}, upsert=True)
            return 1, True, True
        if uid in d["u"]:
            return len(d["u"]), False, False
        await self.db.requests.update_one({"_id": qkey}, {"$addToSet": {"u": uid}})
        d = await self.db.requests.find_one({"_id": qkey})
        return len(d["u"]), False, True

    async def open_requests(self, limit: int = 50) -> list[dict]:
        rows = [{"key": d["_id"], "text": d["t"], "users": d["u"]}
                async for d in self.db.requests.find({"open": True}).limit(500)]
        return sorted(rows, key=lambda r: -len(r["users"]))[:limit]

    async def close_request(self, qkey: str) -> None:
        await self.db.requests.update_one({"_id": qkey}, {"$set": {"open": False}})

    async def get_request(self, qkey: str) -> dict | None:
        d = await self.db.requests.find_one({"_id": qkey})
        return {"key": d["_id"], "text": d["t"], "users": d.get("u", []), "open": d.get("open", False)} if d else None

    async def delete_request(self, qkey: str) -> bool:
        res = await self.db.requests.delete_one({"_id": qkey})
        return res.deleted_count > 0

    async def add_miss(self, qkey: str) -> None:
        await self.db.misses.update_one({"_id": qkey[:80]}, {"$inc": {"n": 1}}, upsert=True)

    async def top_misses(self, limit: int = 15) -> list[tuple[str, int]]:
        return [(d["_id"], d["n"]) async for d in self.db.misses.find().sort("n", -1).limit(limit)]

    async def schedule_delete(self, chat: int, mid: int, at: float) -> None:
        await self.db.deletions.insert_one({"c": chat, "m": mid, "at": at})

    async def due_deletes(self, now: float, limit: int = 100) -> list[tuple[object, int, int]]:
        return [(d["_id"], d["c"], d["m"]) async for d in self.db.deletions.find({"at": {"$lte": now}}).limit(limit)]

    async def clear_deletes(self, keys: list) -> None:
        if keys:
            await self.db.deletions.delete_many({"_id": {"$in": keys}})

    async def add_report(self, uid: str, code: str, user: int) -> tuple[int, dict, bool]:
        d = await self.db.reports.find_one({"_id": uid}) or {"u": [], "c": {}}
        new = user not in d["u"]
        if new:
            await self.db.reports.update_one({"_id": uid}, {"$addToSet": {"u": user}, "$inc": {f"c.{code}": 1}},
                                             upsert=True)
            d = await self.db.reports.find_one({"_id": uid})
        return len(d["u"]), d.get("c", {}), new

    async def clear_reports(self, uid: str) -> None:
        await self.db.reports.delete_one({"_id": uid})

    async def note_skips(self, items: list[dict]) -> None:
        from pymongo import UpdateOne
        if not items:
            return
        now = int(time.time())
        await self.db.skips.bulk_write([
            UpdateOne({"_id": i["uid"]}, {"$set": {"k": i["kind"], "n": (i.get("name") or "")[:100], "c": i["ch"],
                                                   "m": i["mid"], "z": i.get("size", 0), "keep": i.get("keep"),
                                                   "t": now}}, upsert=True) for i in items], ordered=False)

    async def clear_skips(self, uids: list[str]) -> None:
        if uids:
            await self.db.skips.delete_many({"_id": {"$in": uids}})

    async def skip_counts(self) -> dict[str, int]:
        return {k: await self.db.skips.count_documents({"k": k}) for k in SKIP_KINDS}

    @staticmethod
    def _skip_row(d: dict) -> dict:
        return {"uid": d["_id"], "n": d.get("n", ""), "c": d.get("c", 0), "m": d.get("m", 0),
                "z": d.get("z", 0), "keep": d.get("keep")}

    async def skips_page(self, kind: str, page: int, size: int = 10) -> tuple[int, list[dict]]:
        total = await self.db.skips.count_documents({"k": kind})
        cur = self.db.skips.find({"k": kind}).sort([("t", -1), ("_id", 1)]).skip(page * size).limit(size)
        return total, [self._skip_row(d) async for d in cur]

    async def skips_all(self, kind: str) -> list[dict]:
        cur = self.db.skips.find({"k": kind}).sort([("t", -1), ("_id", 1)]).limit(SKIP_CAP)
        return [self._skip_row(d) async for d in cur]

    async def prune_skips(self, cap: int = SKIP_CAP) -> None:
        for kind in SKIP_KINDS:
            extra = await self.db.skips.count_documents({"k": kind}) - cap
            while extra > 0:
                ids = [d["_id"] async for d in self.db.skips.find({"k": kind}, {"_id": 1}).sort("t", 1).limit(min(extra, 5000))]
                if not ids:
                    break
                await self.db.skips.delete_many({"_id": {"$in": ids}})
                extra -= len(ids)

    async def donor_add(self, uid: int, days: int = 90, name: str = "") -> str:
        u = await self.db.users.find_one({"_id": uid}) or {}
        curr = u.get("su")
        try:
            base = date.fromisoformat(curr) if curr and curr >= _today() else date.today()
        except ValueError:
            base = date.today()
        new_date = (base + timedelta(days=days)).isoformat()
        upd = {"su": new_date}
        if name:
            upd["name"] = name
        await self.db.users.update_one({"_id": uid}, {"$set": upd}, upsert=True)
        return new_date

    async def donor_remove(self, uid: int) -> bool:
        res = await self.db.users.update_one({"_id": uid}, {"$unset": {"su": ""}})
        return res.modified_count > 0

    async def donor_list(self) -> list[dict]:
        today = _today()
        cur = self.db.users.find({"su": {"$gte": today}}, {"_id": 1, "su": 1, "name": 1, "wall": 1}).sort("su", 1)
        return [{"uid": d["_id"], "su": d["su"], "name": d.get("name", ""), "wall": bool(d.get("wall"))} async for d in cur]

    async def set_wall_opt_in(self, uid: int, flag: bool) -> None:
        await self.db.users.update_one({"_id": uid}, {"$set": {"wall": flag}}, upsert=True)

    async def get_supporters_wall(self) -> list[str]:
        today = _today()
        cur = self.db.users.find({"su": {"$gte": today}, "wall": True}, {"_id": 1, "name": 1}).limit(100)
        return [(d.get("name") or str(d["_id"])) async for d in cur]

    async def record_survey(self, uid: int, answers: dict) -> None:
        await self.db.users.update_one({"_id": uid}, {"$set": {"sv": True}}, upsert=True)
        inc = {f"totals.{q}.{ans}": 1 for q, ans in answers.items()}
        inc["totals._meta.count"] = 1
        g, a, c = answers.get("gender"), answers.get("age"), answers.get("country")
        if c and g and a:
            cohort_key = f"{c} • {g} • {a}".replace(".", "_")
            inc[f"totals._cohorts.{cohort_key}"] = 1
        await self.db.stats.update_one({"_id": "survey"}, {"$inc": inc}, upsert=True)
        anon_doc = {k: v for k, v in answers.items()}
        anon_doc["date"] = _today()
        await self.db.surveys.insert_one(anon_doc)

    async def get_survey_stats(self) -> dict:
        d = await self.db.stats.find_one({"_id": "survey"}) or {}
        return d.get("totals", {})

    async def add_claim(self, uid: int, name: str, method: str, note: str = "") -> tuple[bool, str]:
        existing = await self.db.claims.find_one({"uid": uid, "status": "pending"})
        if existing:
            return False, str(existing["_id"])
        import uuid
        cid = f"cl_{uuid.uuid4().hex[:8]}"
        await self.db.claims.insert_one({
            "_id": cid,
            "uid": uid,
            "name": name,
            "method": method,
            "note": note,
            "status": "pending",
            "date": _today()
        })
        return True, cid

    async def get_claim(self, cid: str) -> dict | None:
        d = await self.db.claims.find_one({"_id": cid})
        if d:
            d["id"] = d["_id"]
        return d

    async def resolve_claim(self, cid: str, status: str) -> bool:
        res = await self.db.claims.update_one({"_id": cid}, {"$set": {"status": status}})
        return res.modified_count > 0

    async def counts(self) -> dict:
        stats = await self.db.command("dbStats")
        today = _today()
        return {"files": await self.db.files.estimated_document_count(),
                "films": await self.db.films.estimated_document_count(),
                "users": await self.db.users.estimated_document_count(),
                "requests": await self.db.requests.count_documents({"open": True}),
                "supporters": await self.db.users.count_documents({"su": {"$gte": today}}),
                "db_mb": round((stats.get("dataSize", 0) + stats.get("indexSize", 0)) / 1e6)}
