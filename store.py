"""Persistent data. MongoStore talks to Atlas; MemoryStore is the same API in
plain dicts, used by the tests (and handy for a dry run)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import AsyncIterator

DEFAULTS = {"force_sub": None, "autodelete_min": 0, "protect": False, "groups": True}


@dataclass
class FileRec:
    uid: str                      # Telegram file_unique_id: same file => same id, forever
    film: str                     # film id (see index.film_id)
    ch: int                       # channel the file lives in
    mid: int                      # message id in that channel (delivery = copy_message)
    size: int = 0
    res: str | None = None
    src: str | None = None
    langs: tuple[str, ...] = ()
    name: str = ""


def _doc(r: FileRec) -> dict:
    return {"f": r.film, "c": r.ch, "m": r.mid, "z": r.size, "r": r.res, "s": r.src,
            "l": list(r.langs), "n": r.name}


def _rec(d: dict) -> FileRec:
    return FileRec(d["_id"], d["f"], d["c"], d["m"], d.get("z", 0), d.get("r"), d.get("s"),
                   tuple(d.get("l", ())), d.get("n", ""))


class MemoryStore:
    def __init__(self) -> None:
        self.files: dict[str, FileRec] = {}
        self.films: dict[str, dict] = {}
        self.users: dict[int, dict] = {}
        self.reqs: dict[str, dict] = {}
        self.miss: dict[str, dict] = {}
        self.cfg: dict = {}

    async def setup(self) -> None: ...
    async def ping(self) -> bool: return True

    async def iter_films(self) -> AsyncIterator[dict]:
        for fid, d in list(self.films.items()):
            yield {"fid": fid, "title": d["t"], "year": d["y"], "n": d["n"], "hits": d["h"],
                   "aliases": list(d["a"])}

    async def add_files(self, recs: list[FileRec], films: dict[str, tuple]) -> list[FileRec]:
        new = []
        for r in recs:
            if r.uid not in self.files:
                self.files[r.uid] = r
                new.append(r)
        for r in new:
            d = self.films.setdefault(r.film, {"t": films[r.film][0], "y": films[r.film][1],
                                               "n": 0, "h": 0, "a": []})
            d["n"] += 1
        return new

    async def film_files(self, fid: str) -> list[FileRec]:
        return [r for r in self.files.values() if r.film == fid]

    async def get_file(self, uid: str) -> FileRec | None:
        return self.files.get(uid)

    async def remove_film(self, fid: str) -> int:
        gone = [u for u, r in self.files.items() if r.film == fid]
        for u in gone:
            del self.files[u]
        self.films.pop(fid, None)
        return len(gone)

    async def remove_file(self, uid: str) -> FileRec | None:
        r = self.files.pop(uid, None)
        if r and r.film in self.films:
            self.films[r.film]["n"] -= 1
        return r

    async def bump_hit(self, fid: str) -> None:
        if fid in self.films:
            self.films[fid]["h"] += 1

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

    async def counts(self) -> dict:
        return {"files": len(self.files), "films": len(self.films), "users": len(self.users),
                "requests": sum(1 for d in self.reqs.values() if d["open"]), "db_mb": None}


class MongoStore:
    """Atlas-backed store. Needs pymongo >= 4.9 (async client)."""

    def __init__(self, uri: str, db_name: str) -> None:
        from pymongo import AsyncMongoClient
        self.db = AsyncMongoClient(uri, serverSelectionTimeoutMS=20000)[db_name]

    async def setup(self) -> None:
        await self.db.files.create_index("f")
        await self.db.files.create_index([("c", 1), ("m", 1)])
        await self.db.requests.create_index("open")

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

    async def add_files(self, recs: list[FileRec], films: dict[str, tuple]) -> list[FileRec]:
        from pymongo import UpdateOne
        if not recs:
            return []
        res = await self.db.files.bulk_write(
            [UpdateOne({"_id": r.uid}, {"$setOnInsert": _doc(r)}, upsert=True) for r in recs],
            ordered=False)
        new = [recs[i] for i in sorted(res.upserted_ids)]
        per_film: dict[str, int] = {}
        for r in new:
            per_film[r.film] = per_film.get(r.film, 0) + 1
        if per_film:
            await self.db.films.bulk_write([
                UpdateOne({"_id": f}, {"$setOnInsert": {"t": films[f][0], "y": films[f][1], "h": 0, "a": []},
                                       "$inc": {"n": n}}, upsert=True)
                for f, n in per_film.items()], ordered=False)
        return new

    async def film_files(self, fid: str) -> list[FileRec]:
        return [_rec(d) async for d in self.db.files.find({"f": fid})]

    async def get_file(self, uid: str) -> FileRec | None:
        d = await self.db.files.find_one({"_id": uid})
        return _rec(d) if d else None

    async def remove_film(self, fid: str) -> int:
        res = await self.db.files.delete_many({"f": fid})
        await self.db.films.delete_one({"_id": fid})
        return res.deleted_count

    async def remove_file(self, uid: str) -> FileRec | None:
        d = await self.db.files.find_one_and_delete({"_id": uid})
        if not d:
            return None
        await self.db.films.update_one({"_id": d["f"]}, {"$inc": {"n": -1}})
        return _rec(d)

    async def bump_hit(self, fid: str) -> None:
        await self.db.films.update_one({"_id": fid}, {"$inc": {"h": 1}})

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

    async def counts(self) -> dict:
        stats = await self.db.command("dbStats")
        return {"files": await self.db.files.estimated_document_count(),
                "films": await self.db.films.estimated_document_count(),
                "users": await self.db.users.estimated_document_count(),
                "requests": await self.db.requests.count_documents({"open": True}),
                "db_mb": round((stats.get("dataSize", 0) + stats.get("indexSize", 0)) / 1e6)}
