"""Read-only survey of the movie bot's Mongo databases. Run it once, on your
own machine (it needs `pip install pymongo`), before building the new bot:

    DATABASE_URI="mongodb+srv://..." SECONDDB_URI="mongodb+srv://..." \\
        python scripts/analyze_db.py

It only reads file_name and file_size, never writes, and prints how many
distinct films your files really are, how messy the names are, and a sample
of names the parser can't make sense of.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from typing import Iterable

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from normalize import core_key, normalize  # noqa: E402
from parse import is_junk, parse_filename  # noqa: E402

MB_PER_1000_FILMS = 0.44   # measured on synthetic data: ~132 MB for 300k films


def analyze(names: Iterable[str], odd_sample: int = 25, top: int = 10) -> dict:
    total = series = no_title = with_year = 0
    films: Counter = Counter()
    odd: list[str] = []
    for raw in names:
        total += 1
        p = parse_filename(raw or "")
        if is_junk(p.title) or not normalize(p.title):
            no_title += 1
            if len(odd) < odd_sample:
                odd.append(raw)
            continue
        if p.kind == "series":
            series += 1
            continue
        if p.year:
            with_year += 1
        films[(core_key(normalize(p.title)), p.year)] += 1
    movies = total - no_title - series
    n_films = len(films)
    sizes = Counter("1" if c == 1 else "2" if c == 2 else "3-5" if c <= 5 else "6+"
                    for c in films.values())
    return {
        "files": total, "movie_files": movies, "series_files": series,
        "unparseable": no_title, "with_year": with_year,
        "films": n_films, "files_per_film": round(movies / n_films, 2) if n_films else 0,
        "files_per_film_buckets": dict(sizes),
        "films_without_year": sum(1 for (_, y) in films if y is None),
        "est_index_mb": round(n_films / 1000 * MB_PER_1000_FILMS),
        "biggest": [(k[0], k[1], c) for k, c in films.most_common(top)],
        "odd_names": odd,
    }


def _mongo_names(limit: int | None):
    from pymongo import MongoClient   # imported late so tests don't need it
    db_name = os.environ.get("DATABASE_NAME", "Rajappan")
    coll = os.environ.get("COLLECTION_NAME", "Telegram_files")
    for var in ("DATABASE_URI", "SECONDDB_URI"):
        uri = os.environ.get(var)
        if not uri:
            continue
        cur = MongoClient(uri)[db_name][coll].find({}, {"file_name": 1}, batch_size=5000)
        if limit:
            cur = cur.limit(limit)
        print(f"reading {var}...", file=sys.stderr)
        for doc in cur:
            yield doc.get("file_name", "")


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None   # e.g. 50000 for a dry run
    r = analyze(_mongo_names(limit))
    print(f"\nfiles read:            {r['files']:,}")
    print(f"  movie files:         {r['movie_files']:,}")
    print(f"  look like series:    {r['series_files']:,}")
    print(f"  unparseable names:   {r['unparseable']:,}")
    print(f"distinct films:        {r['films']:,}   ({r['files_per_film']} files per film)")
    print(f"  files with a year:   {r['with_year']:,}")
    print(f"  films with no year:  {r['films_without_year']:,}")
    print(f"  files-per-film:      {r['files_per_film_buckets']}")
    print(f"estimated index RAM:   ~{r['est_index_mb']} MB")
    print("\nbiggest groups:")
    for title, year, c in r["biggest"]:
        print(f"  {c:5}  {title} ({year})")
    print("\nnames the parser gave up on:")
    for n in r["odd_names"]:
        print("  ", n)


if __name__ == "__main__":
    main()
