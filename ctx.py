"""Shared runtime state passed to every handler."""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field

from config import Config
from index import FilmIndex
from store import FileRec


@dataclass
class Session:
    owner: int
    query: str
    kind: str                                   # result kind from the index
    hits: list[tuple[str, str, int]]            # (film id, label, file count)
    corrected: bool = False
    year: int | None = None
    year_relaxed: bool = False
    page: int = 0
    film: tuple[str, str, int | None] | None = None   # (fid, title, year) being viewed
    variants: list[FileRec] = field(default_factory=list)
    vpage: int = 0
    qkey: str = ""
    header: str | None = None                   # set for Discover lists
    back: str | None = None                     # callback of the "back" button for lists
    mode: str = ""                              # "remove" while an admin is choosing a file to delete
    group: bool = False                         # in groups anyone may tap the results, not just the searcher


class Sessions:
    """Remembers what each results message was showing, so buttons can stay tiny."""

    def __init__(self, cap: int = 4000) -> None:
        self._d: OrderedDict[tuple[int, int], Session] = OrderedDict()
        self._cap = cap

    def put(self, chat: int, msg: int, s: Session) -> None:
        self._d[(chat, msg)] = s
        self._d.move_to_end((chat, msg))
        while len(self._d) > self._cap:
            self._d.popitem(last=False)

    def get(self, chat: int, msg: int) -> Session | None:
        return self._d.get((chat, msg))

    def drop(self, chat: int, msg: int) -> None:
        self._d.pop((chat, msg), None)


class Ctx:
    def __init__(self, cfg: Config, store, index: FilmIndex) -> None:
        self.cfg, self.store, self.index = cfg, store, index
        self.sessions = Sessions()
        self.ready = False
        self.load_error: str | None = None
        self.bot_username = ""
        self.banned: set[int] = set()
        self.index_job: dict | None = None
        self.expiry: dict[tuple[int, int], float] = {}
        self.join_ok: dict[tuple[int, int], float] = {}        # (user, channel) -> until when "joined" is trusted
        self.pending: dict[int, dict] = {}                     # user -> what they were doing when asked to join
        self.pending_dedupe: list | None = None
        self.top_cache: tuple[float, list] | None = None
        self._settings: tuple[float, dict] | None = None
        self._last_search: dict[int, float] = {}
        self._warned: set[str] = set()

    def is_admin(self, uid: int) -> bool:
        return uid in self.cfg.admins

    async def settings(self) -> dict:
        now = time.monotonic()
        if self._settings is None or now - self._settings[0] > 30:
            self._settings = (now, await self.store.settings())
        return self._settings[1]

    async def set_setting(self, key: str, value) -> None:
        await self.store.set_setting(key, value)
        self._settings = None

    def throttled(self, uid: int, gap: float = 1.0) -> bool:
        now = time.monotonic()
        if now - self._last_search.get(uid, 0.0) < gap:
            return True
        self._last_search[uid] = now
        if len(self._last_search) > 20000:
            self._last_search.clear()
        return False

    def watch(self, chat: int, msg: int, minutes: int) -> None:
        """Delete this bot message `minutes` from now (pushed back whenever the user taps it)."""
        if minutes:
            self.expiry[(chat, msg)] = time.monotonic() + minutes * 60

    def due(self) -> list[tuple[int, int]]:
        now = time.monotonic()
        gone = [k for k, t in self.expiry.items() if t <= now]
        for k in gone:
            del self.expiry[k]
            self.sessions.drop(*k)
        return gone

    JOIN_CACHE_SECONDS = 300
    PENDING_SECONDS = 300
    MAX_FORCE_SUB = 4

    def joined_recently(self, uid: int, chat: int) -> bool:
        return self.join_ok.get((uid, chat), 0) > time.monotonic()

    def mark_joined(self, uid: int, chat: int) -> None:
        """Only "joined" is ever remembered, never "not joined", so joining takes effect at once."""
        if len(self.join_ok) > 50000:
            self.join_ok.clear()
        self.join_ok[(uid, chat)] = time.monotonic() + self.JOIN_CACHE_SECONDS

    def set_pending(self, uid: int, action: dict, missing: set[int]) -> None:
        if len(self.pending) > 5000:
            self.pending.clear()
        self.pending[uid] = {**action, "missing": set(missing), "until": time.monotonic() + self.PENDING_SECONDS}

    def get_pending(self, uid: int) -> dict | None:
        p = self.pending.get(uid)
        if p and p["until"] < time.monotonic():
            del self.pending[uid]
            return None
        return p

    async def subs(self) -> list[dict]:
        """Force-join channels, [{chat, link, title}]. Moves the old single setting across on first use."""
        s = await self.settings()
        subs = list(s.get("force_subs") or [])
        old = s.get("force_sub")
        if old:
            if not any(c["chat"] == old["chat"] for c in subs):
                subs.append(old)
            await self.set_setting("force_subs", subs)
            await self.set_setting("force_sub", None)
        return subs

    def first_time(self, key: str) -> bool:
        """True only once per process for this key (stops alert spam)."""
        if key in self._warned:
            return False
        self._warned.add(key)
        return True

    def deep_link(self, uid: str) -> str:
        return f"https://t.me/{self.bot_username}?start=f_{uid}"
