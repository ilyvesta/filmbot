"""Persistent data. MongoStore talks to Atlas; MemoryStore is the same API in
plain dicts, used by the tests (and handy for a dry run)."""
from __future__ import annotations

import time
from datetime import date, timedelta
from dataclasses import dataclass
from typing import AsyncIterator

DEFAULTS = {
    "force_sub": None, "autodelete_min": 10, "result_ttl_min": 4, "protect": False, "groups": True,
    "welcome": None, "about": None, "image": None, "links": [], "footer": "Join: @exivum",
    "picks": [], "picks_name": "Hand-picked", "index_series": False,
}


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
        self._clock = 0

    async def setup(self) -> None: ...
    async def ping(self) -> bool: return True

    async def iter_films(self) -> AsyncIterator[dict]:
        for fid, d in list(self.films.items()):
            yield {"fid": fid, "title": d["t"], "year": d["y"], "n": d["n"], "hits": d["h"],
                   "aliases": list(d["a"])}

    async def add_files(self, recs: list[FileRec], films: dict[str, tuple]) -> tuple[list[FileRec], int, int]:
        new, known, look, seen = [], 0, 0, set()
        have = {(r.film, r.size) for r in self.files.values() if r.size}
        for r in recs:
            if r.uid in self.files:
                known += 1
                old = self.files[r.uid]
                for k in ("size", "res", "src", "langs", "name", "codec", "grp", "tags", "ext"):
                    setattr(old, k, getattr(r, k))
                continue
            key = (r.film, r.size)
            if r.size and (key in have or key in seen):
                look += 1
                continue
            seen.add(key)
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

    async def lookalikes(self) -> list[tuple[str, str]]:
        """(file uid to remove, film id) for every extra copy of the same film+size."""
        seen, out = set(), []
        for r in sorted(self.files.values(), key=lambda r: (r.film, r.size, r.uid)):
            if not r.size:
                continue
            if (r.film, r.size) in seen:
                out.append((r.uid, r.film))
            seen.add((r.film, r.size))
        return out

    async def delete_files(self, items: list[tuple[str, str]]) -> int:
        n = 0
        for uid, _ in items:
            if await self.remove_file(uid):
                n += 1
        return n

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

    async def touch_user(self, uid: int) -> bool:
        new = uid not in self.users
        self.users.setdefault(uid, {})
        return new

    async def banned_ids(self) -> set[int]:
        return {u for u, d in self.users.items() if d.get("b")}

    async def set_banned(self, uid: int, flag: bool) -> None:
        self.users.setdefault(uid, {})["b"] = flag

    async def add_request(self, qkey: str, text: str, uid: int) -> tuple[int, bool]:
        d = self.reqs.get(qkey)
        if d is None or not d["open"]:
            self.reqs[qkey] = {"t": text, "u": [uid], "open": True}
            return 1, True
        if uid not in d["u"]:
            d["u"].append(uid)
        return len(d["u"]), False

    async def open_requests(self, limit: int = 50) -> list[dict]:
        rows = [{"key": k, "text": d["t"], "users": list(d["u"])} for k, d in self.reqs.items() if d["open"]]
        return sorted(rows, key=lambda r: -len(r["users"]))[:limit]

    async def close_request(self, qkey: str) -> None:
        if qkey in self.reqs:
            self.reqs[qkey]["open"] = False

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

    async def note_skip(self, kind: str, uid: str, name: str, ch: int, mid: int) -> None:
        self.skipped.setdefault(uid, {"k": kind, "n": name[:100], "c": ch, "m": mid})

    async def skips(self, kind: str, limit: int = 25) -> tuple[int, list[str]]:
        rows = [d["n"] for d in self.skipped.values() if d["k"] == kind]
        return len(rows), rows[-limit:][::-1]

    async def counts(self) -> dict:
        return {"files": len(self.files), "films": len(self.films), "users": len(self.users),
                "requests": sum(1 for d in self.reqs.values() if d["open"]), "db_mb": None}


class MongoStore:
    """Atlas-backed store. Needs pymongo >= 4.9 (async client)."""

    def __init__(self, uri: str, db_name: str) -> None:
        from pymongo import AsyncMongoClient
        self.db = AsyncMongoClient(uri, serverSelectionTimeoutMS=20000)[db_name]

    async def setup(self) -> None:
        await self.db.files.create_index([("f", 1), ("z", 1)])      # film lookup + same-size check
        try:
            await self.db.files.drop_index("f_1")                    # the compound index covers it
        except Exception:
            pass
        await self.db.files.create_index([("c", 1), ("m", 1)])
        await self.db.requests.create_index("open")
        await self.db.daily.create_index("d")
        await self.db.films.create_index("ts")
        await self.db.deletions.create_index("at")

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

    async def add_files(self, recs: list[FileRec], films: dict[str, tuple]) -> tuple[list[FileRec], int, int]:
        from pymongo import UpdateOne
        if not recs:
            return [], 0, 0
        known = {d["_id"] async for d in self.db.files.find({"_id": {"$in": [r.uid for r in recs]}}, {"_id": 1})}
        fresh = [r for r in recs if r.uid not in known]
        existing: set[tuple[str, int]] = set()
        sized = [r for r in fresh if r.size]
        if sized:
            async for d in self.db.files.find({"f": {"$in": list({r.film for r in sized})},
                                               "z": {"$in": list({r.size for r in sized})}}, {"f": 1, "z": 1}):
                existing.add((d["f"], d["z"]))
        new, look, seen = [], 0, set()
        for r in fresh:
            key = (r.film, r.size)
            if r.size and (key in existing or key in seen):
                look += 1                      # same film, same byte size: a re-upload of the same file
                continue
            seen.add(key)
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

    async def lookalikes(self) -> list[tuple[str, str]]:
        """(file uid to remove, film id) for every extra copy of the same film+size. Streams the
        collection in index order, so memory stays small."""
        out, prev = [], None
        cur = self.db.files.find({"z": {"$gt": 0}}, {"f": 1, "z": 1}).sort([("f", 1), ("z", 1)])
        async for d in cur:
            key = (d["f"], d["z"])
            if key == prev:
                out.append((d["_id"], d["f"]))
            prev = key
        return out

    async def delete_files(self, items: list[tuple[str, str]]) -> int:
        from pymongo import UpdateOne
        total, per_film = 0, {}
        for i in range(0, len(items), 1000):
            chunk = items[i:i + 1000]
            res = await self.db.files.delete_many({"_id": {"$in": [u for u, _ in chunk]}})
            total += res.deleted_count
            for _, f in chunk:
                per_film[f] = per_film.get(f, 0) + 1
        if per_film:
            await self.db.films.bulk_write([UpdateOne({"_id": f}, {"$inc": {"n": -n}}) for f, n in per_film.items()],
                                           ordered=False)
        return total

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

    async def touch_user(self, uid: int) -> bool:
        res = await self.db.users.update_one({"_id": uid}, {"$setOnInsert": {"t": int(time.time())}}, upsert=True)
        return res.upserted_id is not None

    async def banned_ids(self) -> set[int]:
        return {d["_id"] async for d in self.db.users.find({"b": True}, {"_id": 1})}

    async def set_banned(self, uid: int, flag: bool) -> None:
        await self.db.users.update_one({"_id": uid}, {"$set": {"b": flag}}, upsert=True)

    async def add_request(self, qkey: str, text: str, uid: int) -> tuple[int, bool]:
        d = await self.db.requests.find_one({"_id": qkey})
        if d is None or not d.get("open"):
            await self.db.requests.replace_one({"_id": qkey}, {"t": text, "u": [uid], "open": True,
                                                               "ts": int(time.time())}, upsert=True)
            return 1, True
        await self.db.requests.update_one({"_id": qkey}, {"$addToSet": {"u": uid}})
        d = await self.db.requests.find_one({"_id": qkey})
        return len(d["u"]), False

    async def open_requests(self, limit: int = 50) -> list[dict]:
        rows = [{"key": d["_id"], "text": d["t"], "users": d["u"]}
                async for d in self.db.requests.find({"open": True}).limit(500)]
        return sorted(rows, key=lambda r: -len(r["users"]))[:limit]

    async def close_request(self, qkey: str) -> None:
        await self.db.requests.update_one({"_id": qkey}, {"$set": {"open": False}})

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

    async def note_skip(self, kind: str, uid: str, name: str, ch: int, mid: int) -> None:
        await self.db.skips.update_one({"_id": uid}, {"$setOnInsert": {"k": kind, "n": name[:100], "c": ch, "m": mid}},
                                       upsert=True)

    async def skips(self, kind: str, limit: int = 25) -> tuple[int, list[str]]:
        n = await self.db.skips.count_documents({"k": kind})
        rows = [d["n"] async for d in self.db.skips.find({"k": kind}).sort([("$natural", -1)]).limit(limit)]
        return n, rows

    async def counts(self) -> dict:
        stats = await self.db.command("dbStats")
        return {"files": await self.db.files.estimated_document_count(),
                "films": await self.db.films.estimated_document_count(),
                "users": await self.db.users.estimated_document_count(),
                "requests": await self.db.requests.count_documents({"open": True}),
                "db_mb": round((stats.get("dataSize", 0) + stats.get("indexSize", 0)) / 1e6)}
