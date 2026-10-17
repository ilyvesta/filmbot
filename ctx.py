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


class Ctx:
    def __init__(self, cfg: Config, store, index: FilmIndex) -> None:
        self.cfg, self.store, self.index = cfg, store, index
        self.sessions = Sessions()
        self.ready = False
        self.load_error: str | None = None
        self.bot_username = ""
        self.banned: set[int] = set()
        self.index_job: dict | None = None
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

    def first_time(self, key: str) -> bool:
        """True only once per process for this key (stops alert spam)."""
        if key in self._warned:
            return False
        self._warned.add(key)
        return True

    def deep_link(self, uid: str) -> str:
        return f"https://t.me/{self.bot_username}?start=f_{uid}"
