"""Shared runtime state passed to every handler."""
from __future__ import annotations

import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

from config import Config
from index import FilmIndex
from store import FileRec


LAGOS = timezone(timedelta(hours=1))      # Africa/Lagos has no daylight saving, so a fixed +1 is exact


def lagos_now() -> datetime:
    return datetime.now(LAGOS)


def lagos_day(back: int = 0) -> str:
    return (lagos_now().date() - timedelta(days=back)).isoformat()


class Stats:
    """Counts events in memory; the background job writes the changes to one small document per day."""

    def __init__(self) -> None:
        self.pending: dict[str, dict[str, int]] = {}

    def add(self, key: str, n: int = 1) -> None:
        d = self.pending.setdefault(lagos_day(), {})
        d[key] = d.get(key, 0) + n

    def peek(self, day: str) -> dict[str, int]:
        return dict(self.pending.get(day, {}))

    def drain(self) -> dict[str, dict[str, int]]:
        out, self.pending = self.pending, {}
        return out


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
    seen: set = field(default_factory=set)      # films already shown by Surprise me / Shuffle in this session
    pick_order: list = field(default_factory=list)   # this viewer's random order of the hand-picked list
    shuffle: str | None = None                  # "picks" or "wl" while Surprise me / Shuffle is showing a film
    reqs: list = field(default_factory=list)    # request keys listed on the My list page
    note: str = ""                              # one-off line shown above the next film page


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
        self.stats = Stats()
        self.seen_day: tuple[str, set[int]] = ("", set())
        self.search_pending: dict[int, int] = {}
        self.wl_cache: dict[int, list[str]] = {}
        self.expiry: dict[tuple[int, int], float] = {}
        self.join_ok: dict[tuple[int, int], float] = {}        # (user, channel) -> until when "joined" is trusted
        self.pending: dict[int, dict] = {}                     # user -> what they were doing when asked to join
        self.pending_dedupe: list | None = None
        self.pending_purge: list | None = None
        self.maintenance: str | None = None        # "indexing", "purge"...: heavy jobs never overlap
        self.top_cache: tuple[float, list] | None = None
        self._settings: tuple[float, dict] | None = None
        self._last_search: dict[int, float] = {}
        self._warned: set[str] = set()
        self.pending_stars_amount: set[int] = set()
        self.pending_donation_replies: dict[int, str] = {}
        self.pending_claim_days: dict[int, str] = {}
        self.pending_claim_bypass: set[int] = set()
        self.pending_donation_contact: dict[int, str] = {}

    def is_admin(self, uid: int) -> bool:
        return uid in self.cfg.admins

    async def is_supporter(self, uid: int) -> bool:
        """Whether this Telegram user has an active supporter period."""
        if uid <= 0:
            return False
        user = await self.store.get_user(uid)
        expiry = user.get("su")
        return bool(expiry and expiry >= lagos_day())

    async def has_clean_files(self, uid: int) -> bool:
        """Whether this user has an active benefit that removes the standard file footer."""
        if uid <= 0:
            return False
        user = await self.store.get_user(uid)
        today = lagos_day()
        return bool(user.get("su") and user["su"] >= today and user.get("cf") and user["cf"] >= today)

    async def has_join_bypass(self, uid: int) -> bool:
        """Only contributors whose qualifying bypass period is still active skip force-join."""
        if uid <= 0:
            return False
        user = await self.store.get_user(uid)
        today = lagos_day()
        return bool(user.get("su") and user["su"] >= today and user.get("jb") and user["jb"] >= today)

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

    async def track_user(self, uid: int) -> None:
        """Record that this person was active today: at most one database write per person per day."""
        day = lagos_day()
        if self.seen_day[0] != day:
            self.seen_day = (day, set())
        if uid in self.seen_day[1]:
            return
        self.seen_day[1].add(uid)
        new, first_today = await self.store.touch_user(uid, day)
        if new:
            self.stats.add("n")
        if first_today:
            self.stats.add("u")

    def count_search(self, uid: int) -> None:
        self.stats.add("s")
        self.search_pending[uid] = self.search_pending.get(uid, 0) + 1

    async def flush_stats(self) -> None:
        for day, deltas in self.stats.drain().items():
            await self.store.stats_add(day, deltas)
        pending, self.search_pending = self.search_pending, {}
        if pending:
            await self.store.add_search_counts(pending)

    async def watchlist(self, uid: int) -> list[str]:
        if uid not in self.wl_cache:
            if len(self.wl_cache) > 5000:
                self.wl_cache.clear()
            self.wl_cache[uid] = list((await self.store.get_user(uid)).get("wl", []))
        return self.wl_cache[uid]

    async def save_watchlist(self, uid: int, ids: list[str]) -> None:
        self.wl_cache[uid] = ids
        await self.store.set_watchlist(uid, ids)

    @staticmethod
    def cap_for(user: dict) -> int:
        su = user.get("su")
        return 50 if su and su >= lagos_day() else 20

    async def watchlist_cap(self, uid: int) -> int:
        return self.cap_for(await self.store.get_user(uid))

    def first_time(self, key: str) -> bool:
        """True only once per process for this key (stops alert spam)."""
        if key in self._warned:
            return False
        self._warned.add(key)
        return True

    def deep_link(self, uid: str) -> str:
        return f"https://t.me/{self.bot_username}?start=f_{uid}"
