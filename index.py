"""Film index for large libraries: SQLite (stdlib) for lookups, a small
in-RAM word list for typo correction. No AI, no extra dependencies.

One record per *film* (title + year) plus aliases. Files stay in the main
database; this only decides which film the user means.

Search order: exact title -> squashed ("spiderman" == "spider man") ->
prefix / all-words -> spell-correct each unknown word and try again.
"""
from __future__ import annotations

import hashlib
import itertools
import math
import sqlite3
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from typing import Iterable

from normalize import core_key, normalize

# Tiers are >= 8 points apart; the popularity bonus is capped at 4.
EXACT, SQUASHED, PREFIX, WORDS_MAX = 100.0, 98.0, 90.0, 80.0
AUTOCORRECT_RATIO = 0.90    # a word fix this similar is applied silently
AUTOCORRECT_GAP = 0.06      # ...if it also beats the runner-up by this much
SUGGEST_RATIO = 0.72        # minimum similarity to be considered at all
MAX_ROWS = 2000             # cap on candidate names per query (most popular first)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS film(
    id INTEGER PRIMARY KEY, fid TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
    year INTEGER, file_count INTEGER NOT NULL DEFAULT 0, hits INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS name(
    id INTEGER PRIMARY KEY, film INTEGER NOT NULL, key TEXT NOT NULL, sq TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS name_film_key ON name(film, key);
CREATE INDEX IF NOT EXISTS name_key ON name(key);
CREATE INDEX IF NOT EXISTS name_sq ON name(sq);
CREATE TABLE IF NOT EXISTS posting(
    word TEXT NOT NULL, name INTEGER NOT NULL, PRIMARY KEY(word, name)) WITHOUT ROWID;
"""
_FILM_COLS = "f.fid, f.title, f.year, f.file_count, f.hits"


def film_id(title_norm: str, year: int | None) -> str:
    """Short, stable id that fits Telegram's 64-byte callback_data."""
    return hashlib.blake2s(f"{core_key(title_norm)}|{year or ''}".encode(),
                           digest_size=5).hexdigest()


@dataclass
class Film:
    id: str
    title: str
    year: int | None
    file_count: int = 0
    hits: int = 0                       # times users opened it; drives tie-breaks

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass
class Hit:
    film: Film
    score: float
    match: str                          # "exact" | "prefix" | "words"


@dataclass
class SearchResult:
    kind: str                           # "match" | "choose" | "suggest" | "none" | "empty"
    hits: list[Hit] = field(default_factory=list)
    query: str = ""                     # what was actually searched (after any correction)
    year: int | None = None
    year_relaxed: bool = False          # user gave a year we don't have for this title
    corrected: bool = False             # words were spell-corrected: show "results for ..."


def _grams(s: str) -> set[str]:
    p = f"  {s} "
    return {p[i:i + 3] for i in range(len(p) - 2)}


def _score_name(nt: list[str], qtoks: list[str]) -> tuple[float, str] | None:
    n = len(qtoks)
    if len(nt) >= n and nt[:n - 1] == qtoks[:n - 1] and nt[n - 1].startswith(qtoks[-1]):
        return PREFIX, "prefix"
    for i, q in enumerate(qtoks):
        if q not in nt and not (i == n - 1 and any(w.startswith(q) for w in nt)):
            return None
    return WORDS_MAX - min(15.0, (len(nt) - n) * 3.0), "words"


class FilmIndex:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._lock = threading.RLock()
        self._vocab: dict[str, int] = {}                    # word -> names containing it
        self._wtri: dict[str, list[str]] = defaultdict(list)  # trigram -> words
        for (w, c) in self._db.execute("SELECT word, COUNT(*) FROM posting GROUP BY word"):
            self._track_word(w, c)

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM film").fetchone()[0]

    # ---- building -------------------------------------------------------
    def add_film(self, title: str, year: int | None, *, file_count: int = 0,
                 hits: int = 0, aliases: tuple[str, ...] = ()) -> Film:
        with self._lock:
            cur = self._db.cursor()
            rid = self._upsert(cur, title, year, file_count, hits, aliases)
            self._db.commit()
            row = cur.execute(f"SELECT {_FILM_COLS} FROM film f WHERE f.id=?", (rid,)).fetchone()
        return Film(*row)

    def add_films(self, items: Iterable[tuple]) -> int:
        """Bulk load: items are (title, year, file_count, hits, aliases) tuples."""
        n = 0
        with self._lock:
            cur = self._db.cursor()
            for it in items:
                self._upsert(cur, *it)
                n += 1
            self._db.commit()
        return n

    def add_alias(self, fid: str, alias: str) -> bool:
        with self._lock:
            cur = self._db.cursor()
            row = cur.execute("SELECT id FROM film WHERE fid=?", (fid,)).fetchone()
            if not row:
                return False
            self._add_name(cur, row[0], alias)
            self._db.commit()
        return True

    def get(self, fid: str) -> Film | None:
        with self._lock:
            row = self._db.execute(f"SELECT {_FILM_COLS} FROM film f WHERE f.fid=?", (fid,)).fetchone()
        return Film(*row) if row else None

    def record_open(self, fid: str) -> None:
        with self._lock:
            self._db.execute("UPDATE film SET hits = hits + 1 WHERE fid=?", (fid,))
            self._db.commit()

    def remove_film(self, fid: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT id FROM film WHERE fid=?", (fid,)).fetchone()
            if not row:
                return False
            ids = [r[0] for r in self._db.execute("SELECT id FROM name WHERE film=?", (row[0],))]
            for nid in ids:
                self._db.execute("DELETE FROM posting WHERE name=?", (nid,))
            self._db.execute("DELETE FROM name WHERE film=?", (row[0],))
            self._db.execute("DELETE FROM film WHERE id=?", (row[0],))
            self._db.commit()
        return True

    def adjust_files(self, fid: str, delta: int) -> None:
        with self._lock:
            self._db.execute("UPDATE film SET file_count = MAX(0, file_count + ?) WHERE fid=?", (delta, fid))
            self._db.commit()

    def _upsert(self, cur, title, year, file_count=0, hits=0, aliases=()) -> int:
        norm = normalize(title)
        fid = film_id(norm, year)
        row = cur.execute("SELECT id FROM film WHERE fid=?", (fid,)).fetchone()
        if row:
            rid = row[0]
            cur.execute("UPDATE film SET file_count=file_count+?, hits=hits+? WHERE id=?",
                        (file_count, hits, rid))
            for a in aliases:
                self._add_name(cur, rid, a)
            return rid
        cur.execute("INSERT INTO film(fid,title,year,file_count,hits) VALUES(?,?,?,?,?)",
                    (fid, title, year, file_count, hits))
        rid = cur.lastrowid
        for nm in (norm, *aliases):
            self._add_name(cur, rid, nm)
        return rid

    def _add_name(self, cur, rid: int, raw: str) -> None:
        key = core_key(normalize(raw))
        if not key:
            return
        cur.execute("INSERT OR IGNORE INTO name(film,key,sq) VALUES(?,?,?)",
                    (rid, key, key.replace(" ", "")))
        if not cur.rowcount:
            return
        nid = cur.lastrowid
        words = set(key.split())
        cur.executemany("INSERT OR IGNORE INTO posting(word,name) VALUES(?,?)",
                        [(w, nid) for w in words])
        for w in words:
            self._track_word(w, 1)

    def _track_word(self, w: str, count: int) -> None:
        if w in self._vocab:
            self._vocab[w] += count
            return
        self._vocab[w] = count
        for g in _grams(w):
            self._wtri[g].append(w)

    # ---- searching ------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> SearchResult:
        toks = normalize(query).split()
        if not toks:
            return SearchResult("empty")

        year = None
        if len(toks) > 1:
            for i in range(len(toks) - 1, -1, -1):
                t = toks[i]
                if len(t) == 4 and t.isdigit() and 1888 <= int(t) <= date.today().year + 1:
                    year = int(t)
                    del toks[i]
                    break
        qkey = core_key(" ".join(toks))
        qtoks = qkey.split()

        corrected = unsure = False
        with self._lock:
            found = self._lookup(qkey, qtoks)
            if not any(s >= WORDS_MAX for _, s, _ in found.values()):
                fix = self._correct(qtoks)
                if fix:
                    ftoks, confident = fix
                    found2 = self._lookup(" ".join(ftoks), ftoks)
                    if any(s >= WORDS_MAX for _, s, _ in found2.values()):
                        found, qkey, corrected, unsure = found2, " ".join(ftoks), True, not confident
        if not found:
            return SearchResult("none", query=qkey, year=year)

        hits = [Hit(f, s + self._bonus(f), m) for f, s, m in found.values()]
        year_relaxed = False
        if year is not None:
            same = [h for h in hits if h.film.year == year]
            if same:
                hits = same
            else:
                year_relaxed = True
        hits.sort(key=lambda h: (-h.score, -(h.film.year or 0), h.film.title))
        hits = hits[:limit]
        res = SearchResult("choose", hits, qkey, year, year_relaxed, corrected)

        if unsure:
            res.kind, res.hits = "suggest", hits[:3]
            return res
        exact = [h for h in hits if h.match == "exact"]
        if len(exact) == 1:
            res.kind = "match"
        elif not exact and hits[0].match == "prefix" and \
                (len(hits) == 1 or hits[0].score - hits[1].score >= 8):
            res.kind = "match"
        return res

    # ---- internals ------------------------------------------------------
    @staticmethod
    def _bonus(f: Film) -> float:
        return min(3.0, math.log10(1 + f.hits)) + min(1.0, math.log10(1 + f.file_count) / 2)

    def _lookup(self, qkey: str, qtoks: list[str]) -> dict[str, tuple[Film, float, str]]:
        out: dict[str, tuple[Film, float, str]] = {}

        def put(row, score: float, match: str) -> None:
            film = Film(*row)
            if film.id not in out or out[film.id][1] < score:
                out[film.id] = (film, score, match)

        q = f"SELECT {_FILM_COLS} FROM name n JOIN film f ON f.id = n.film WHERE n.%s = ?"
        for row in self._db.execute(q % "key", (qkey,)):
            put(row, EXACT, "exact")
        for row in self._db.execute(q % "sq", (qkey.replace(" ", ""),)):
            put(row, SQUASHED, "exact")

        if qtoks:
            parts, params = [], []
            for i, t in enumerate(qtoks):
                if i == len(qtoks) - 1 and len(t) >= 3:        # last word may be half-typed
                    parts.append("SELECT name FROM posting WHERE word >= ? AND word < ?")
                    params += [t, t + "\uffff"]
                else:
                    parts.append("SELECT name FROM posting WHERE word = ?")
                    params.append(t)
            sql = (f"SELECT {_FILM_COLS}, n.key FROM name n JOIN film f ON f.id = n.film "
                   f"WHERE n.id IN ({' INTERSECT '.join(parts)}) "
                   "ORDER BY f.hits DESC, f.file_count DESC LIMIT ?")
            for *row, key in self._db.execute(sql, (*params, MAX_ROWS)):
                scored = _score_name(key.split(), qtoks)
                if scored:
                    put(row, *scored)
        return out

    def _has_prefix(self, t: str) -> bool:
        return self._db.execute("SELECT 1 FROM posting WHERE word >= ? AND word < ? LIMIT 1",
                                (t, t + "\uffff")).fetchone() is not None

    def _similar_words(self, w: str, k: int = 3) -> list[tuple[str, float]]:
        counts: Counter[str] = Counter()
        for g in _grams(w):
            counts.update(self._wtri.get(g, ()))
        sm = SequenceMatcher(None)
        sm.set_seq2(w)
        found = []
        for cand, _ in counts.most_common(40):
            sm.set_seq1(cand)
            if sm.quick_ratio() >= SUGGEST_RATIO:
                r = sm.ratio()
                if r >= SUGGEST_RATIO:
                    found.append((cand, r))
        found.sort(key=lambda x: (-x[1], -self._vocab.get(x[0], 0)))
        return found[:k]

    def _correct(self, qtoks: list[str]) -> tuple[list[str], bool] | None:
        """Fix each unknown word. Returns (new tokens, confident?) or None if nothing to fix."""
        fixed, confident, changed = [], True, False
        for i, t in enumerate(qtoks):
            known = (len(t) < 3 or t.isdigit() or t in self._vocab
                     or (i == len(qtoks) - 1 and self._has_prefix(t)))
            cands = [] if known else self._similar_words(t)
            if not cands:
                fixed.append(t)
                continue
            (best, r), runner = cands[0], (cands[1][1] if len(cands) > 1 else 0.0)
            if r < AUTOCORRECT_RATIO or r - runner < AUTOCORRECT_GAP:
                confident = False
            fixed.append(best)
            changed = True
        if not changed:
            return None
        if len(" ".join(qtoks)) < 5:
            confident = False
        return fixed, confident
