import sys, unittest
sys.path.insert(0, __file__.rsplit("/tests", 1)[0])

from normalize import normalize, core_key
from parse import parse_filename, parse_best, variant_label, human_size
from index import FilmIndex


class NormalizeTests(unittest.TestCase):
    def test_symbols_and_case(self):
        self.assertEqual(normalize("Spider-Man: No Way Home!!"), "spider man no way home")
        self.assertEqual(normalize("  WHAT?!  "), "what")
        self.assertEqual(normalize("Tom & Jerry"), "tom and jerry")
        self.assertEqual(normalize("What's Eating Gilbert Grape"), "whats eating gilbert grape")
        self.assertEqual(normalize("What’s"), "whats")

    def test_accents_and_fancy_fonts(self):
        self.assertEqual(normalize("Amélie"), "amelie")
        self.assertEqual(normalize("𝗪𝗵𝗮𝘁 (𝟮𝟬𝟮𝟬)"), "what 2020")
        self.assertEqual(normalize("ｗｈａｔ"), "what")

    def test_hausa_yoruba_letters(self):
        self.assertEqual(normalize("Ɓarawo"), "barawo")
        self.assertEqual(normalize("Ọlọ́run"), "olorun")

    def test_other_scripts_survive(self):
        self.assertEqual(normalize("बाहुबली: द बिगिनिंग"), "बाहुबली द बिगिनिंग")

    def test_symbols_only_is_empty(self):
        self.assertEqual(normalize("?!,.-"), "")
        self.assertEqual(normalize(None), "")

    def test_core_key_drops_leading_article(self):
        self.assertEqual(core_key("the dark knight"), "dark knight")
        self.assertEqual(core_key("the"), "the")


class ParseTests(unittest.TestCase):
    def p(self, name):
        return parse_filename(name)

    def test_standard_release(self):
        p = self.p("What.2020.1080p.WEB-DL.x264.AAC-GROUP.mkv")
        self.assertEqual((p.title, p.year, p.resolution, p.source, p.codec),
                         ("What", 2020, "1080p", "WEB-DL", "x264"))

    def test_promo_prefix_and_languages(self):
        p = self.p("[SiteName.com] - Spider-Man.No.Way.Home.(2021).720p.BluRay.Hindi-English.mkv")
        self.assertEqual((p.title, p.year), ("Spider-Man No Way Home", 2021))
        self.assertEqual(p.languages, ("Hindi", "English"))

    def test_channel_handle_and_site_prefixes(self):
        self.assertEqual(self.p("@Movies_Channel - Lionheart.2018.mkv").title, "Lionheart")
        self.assertEqual(self.p("www.SceneSite.com - What Happened to Monday 2017 720p.mkv").title,
                         "What Happened to Monday")

    def test_number_titles(self):
        p = self.p("Blade.Runner.2049.2017.2160p.mkv")
        self.assertEqual((p.title, p.year), ("Blade Runner 2049", 2017))
        p = self.p("Blade Runner 2049 1080p.mkv")           # 2049 is not a plausible year
        self.assertEqual((p.title, p.year), ("Blade Runner 2049", None))
        p = self.p("2012 (2009) 1080p BluRay.mp4")
        self.assertEqual((p.title, p.year), ("2012", 2009))
        p = self.p("1917.2019.720p.HDRip.mkv")
        self.assertEqual((p.title, p.year), ("1917", 2019))

    def test_title_that_looks_like_metadata(self):
        self.assertEqual(self.p("Cam.2018.720p.WEBRip.mkv").title, "Cam")
        p = self.p("Hindi Medium (2017) 480p.mkv")
        self.assertEqual((p.title, p.languages), ("Hindi Medium", ()))
        self.assertEqual(self.p("[REC] (2007) 720p.mkv").year, 2007)

    def test_case_cleanup_keeps_mixed_case(self):
        self.assertEqual(self.p("THE DARK KNIGHT 2008 BRRip 1080p x265 HEVC.mkv").title, "The Dark Knight")
        self.assertEqual(self.p("Amélie (2001).mkv").title, "Amélie")
        self.assertEqual(self.p("What's Eating Gilbert Grape (1993).mkv").title,
                         "What's Eating Gilbert Grape")

    def test_series_are_flagged(self):
        p = self.p("Breaking.Bad.S01E02.720p.mkv")
        self.assertEqual((p.kind, p.title, p.season, p.episode), ("series", "Breaking Bad", 1, 2))
        self.assertEqual(self.p("Show Name Season 2 1080p").kind, "series")

    def test_cam_flag_and_sort_key(self):
        cam = self.p("Movie.Name.2019.HDCAM.mkv")
        hd = self.p("Movie.Name.2019.720p.WEB-DL.mkv")
        self.assertTrue(cam.is_cam)
        self.assertGreater(hd.quality_key, cam.quality_key)

    def test_caption_fallback_for_junk_filenames(self):
        p = parse_best("video_123.mp4", "What (2020) 720p WEB-DL\nsome other line")
        self.assertEqual((p.title, p.year), ("What", 2020))

    def test_labels(self):
        p = self.p("What.2020.1080p.WEB-DL.Hindi.mkv")
        self.assertEqual(variant_label(p, 1932735283), "1080p · WEB-DL · 1.8 GB · Hindi")
        self.assertEqual(human_size(650 * 1024 * 1024), "650 MB")


class EncoderTests(unittest.TestCase):
    def p(self, n):
        return parse_filename(n)

    def test_your_three_examples(self):
        a = self.p("Adam 2019 720p WEBRip 800MB x264 GalaxyRG mkv")
        self.assertEqual((a.title, a.year, a.codec, a.group, a.ext), ("Adam", 2019, "x264", "GalaxyRG", "MKV"))
        b = self.p("Bamboozled 2000 480p BluRay x264 AAC [Mkvking com] mkv")
        self.assertEqual((b.title, b.group), ("Bamboozled", "Mkvking.com"))
        c = self.p("days of being wild 1990 phonofilm mkv")
        self.assertEqual((c.title, c.year, c.group), ("Days of Being Wild", 1990, "phonofilm"))

    def test_hyphen_suffix_and_domain_groups(self):
        self.assertEqual(self.p("Adam.2019.720p.WEBRip.x264-GalaxyRG.mkv").group, "GalaxyRG")
        self.assertEqual(self.p("Movie.Name.2019.WEB-DL.www.phonofilm.net.mkv").group, "phonofilm.net")
        self.assertEqual(self.p("Movie.2019.1080p.BluRay.x265-RARBG.mkv").codec, "x265")

    def test_no_encoder_when_filename_has_none(self):
        for n in ("What.2020.1080p.WEB-DL.mkv", "Lionheart.mkv", "Movie.2019.720p.HDRip.Hindi-English.ESub.mkv",
                  "Cam.2018.720p.WEBRip.mkv", "Heat.1995.1080p.BluRay.x264.DDP5.1.mkv"):
            self.assertIsNone(self.p(n).group, n)

    def test_dubbed_is_surfaced_not_hidden(self):
        p = self.p("Adam.2019.1080p.BluRay.x265.10bit.Hindi.Dubbed.DD5.1-RARBG.mkv")
        self.assertEqual((p.languages, p.tags, p.group), (("Hindi",), ("10-bit", "Dubbed"), "RARBG"))
        q = self.p("Adam Hindi Dubbed 2019 720p.mkv")
        self.assertEqual((q.title, q.year, q.languages, q.tags), ("Adam", 2019, ("Hindi",), ("Dubbed",)))

    def test_subtitles_are_recognised(self):
        p = self.p("Movie Name (2019) English.srt")
        self.assertEqual((p.title, p.ext, p.is_sub, p.languages), ("Movie Name", "SRT", True, ("English",)))

    def test_titles_with_tld_like_words_survive(self):
        self.assertEqual(self.p("Way.Home.In.Time.2019.720p.mkv").title, "Way Home In Time")

    def test_detail_lines(self):
        from parse import detail_lines
        self.assertEqual(detail_lines("1080p", "WEB-DL", "x265", 1932735283, ["Hindi"], ("Dubbed",), "MKV", "GalaxyRG"),
                         ("1080p · WEB-DL · x265 · 1.8 GB", "Hindi · Dubbed · MKV · by GalaxyRG"))
        self.assertEqual(detail_lines(None, None, None, 85000, ["English"], (), "SRT", None),
                         ("Subtitles · SRT · 83.0 KB", "English"))


def build_index():
    ix = FilmIndex()
    films = [
        ("What", 1996), ("What", 2008), ("What", 2020),
        ("What Happened to Monday", 2017), ("What We Do in the Shadows", 2014),
        ("The Dark Knight", 2008), ("The Dark Knight Rises", 2012),
        ("Avengers: Endgame", 2019), ("Avengers: Infinity War", 2018),
        ("Avengers: Age of Ultron", 2015), ("The Avengers", 2012),
        ("Inception", 2010), ("Parasite", 2019), ("Lionheart", 2018),
        ("Spider-Man: No Way Home", 2021), ("The Godfather", 1972),
        ("The Godfather Part II", 1974), ("Crash", 2004), ("Clash", 2016),
    ]
    for t, y in films:
        ix.add_film(t, y, file_count=3)
    return ix


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.ix = build_index()

    def labels(self, res):
        return [h.film.label for h in res.hits]

    def test_ambiguous_title_lists_each_year(self):
        r = self.ix.search("what")
        self.assertEqual(r.kind, "choose")
        self.assertEqual(self.labels(r)[:3], ["What (2020)", "What (2008)", "What (1996)"])
        self.assertIn("What Happened to Monday (2017)", self.labels(r))

    def test_year_in_query_resolves_it(self):
        for q in ("what 2020", "What (2020)", "2020 what", "𝗪𝗵𝗮𝘁 2020", "WHAT?! 2020"):
            r = self.ix.search(q)
            self.assertEqual((r.kind, r.hits[0].film.label), ("match", "What (2020)"), q)

    def test_unknown_year_is_flagged_not_silent(self):
        r = self.ix.search("what 1999")
        self.assertTrue(r.year_relaxed)
        self.assertTrue(r.hits)

    def test_symbols_never_break_search(self):
        for q in ("spider-man: no way home", "spiderman no way home", "SPIDER MAN, NO WAY HOME!!!"):
            r = self.ix.search(q)
            self.assertEqual(r.hits[0].film.title, "Spider-Man: No Way Home", q)

    def test_articles_optional(self):
        self.assertEqual(self.ix.search("dark knight").hits[0].film.label, "The Dark Knight (2008)")
        self.assertEqual(self.ix.search("the godfather").kind, "match")
        self.assertEqual(self.ix.search("godfather").hits[0].film.label, "The Godfather (1972)")

    def test_partial_typing_matches_by_prefix(self):
        r = self.ix.search("avengers end")
        self.assertEqual((r.kind, r.hits[0].film.title), ("match", "Avengers: Endgame"))

    def test_typo_is_autocorrected(self):
        for q in ("avengrs endgame", "incepton", "dark knigt"):
            r = self.ix.search(q)
            self.assertEqual((r.kind, r.corrected), ("match", True), q)
        self.assertEqual(self.ix.search("incepton").hits[0].film.title, "Inception")

    def test_close_call_is_suggested_not_forced(self):
        r = self.ix.search("crasch")        # equidistant-ish from Crash / Clash
        self.assertIn(r.kind, ("suggest", "match"))
        self.assertTrue(r.hits)

    def test_nonsense_and_empty(self):
        self.assertEqual(self.ix.search("zzqx wvvk").kind, "none")
        self.assertEqual(self.ix.search("???").kind, "empty")
        self.assertEqual(self.ix.search("").kind, "empty")

    def test_alias_finds_native_title(self):
        parasite = self.ix.search("parasite").hits[0].film
        self.assertEqual(self.ix.search("gisaengchung").kind, "none")
        self.ix.add_alias(parasite.id, "Gisaengchung")
        self.ix.add_alias(parasite.id, "기생충")
        r = self.ix.search("gisaengchung")
        self.assertEqual((r.kind, r.hits[0].film.title), ("match", "Parasite"))
        self.assertEqual(self.ix.search("기생충").hits[0].film.title, "Parasite")

    def test_popularity_breaks_ties(self):
        ix = FilmIndex()
        a = ix.add_film("Echo", 2010)
        b = ix.add_film("Echo", 2015)
        for _ in range(20):
            ix.record_open(a.id)
        self.assertEqual(ix.search("echo").hits[0].film.id, a.id)

    def test_duplicate_film_merges(self):
        ix = FilmIndex()
        ix.add_film("What", 2020, file_count=2)
        f = ix.add_film("WHAT!", 2020, file_count=3)
        self.assertEqual((len(ix), f.file_count), (1, 5))


class ScaleBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.ix = build_index()

    def test_single_word_typo_reaches_long_titles(self):
        r = self.ix.search("avngers")
        self.assertTrue(r.corrected)
        self.assertEqual(r.query, "avengers")
        self.assertIn("Avengers: Endgame", [h.film.title for h in r.hits])

    def test_unsure_correction_is_a_suggestion(self):
        r = self.ix.search("crsh")           # too short to fix silently
        self.assertEqual(r.kind, "suggest")
        self.assertEqual(r.hits[0].film.title, "Crash")

    def test_squashed_spelling(self):
        r = self.ix.search("darkknight")
        self.assertEqual((r.kind, r.hits[0].film.title), ("match", "The Dark Knight"))

    def test_bulk_load_and_reopen_from_disk(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "films.db")
        ix = FilmIndex(path)
        ix.add_films([("Lionheart", 2018, 4, 0, ()), ("Black Panther", 2018, 9, 0, ("Pantera Negra",))])
        del ix
        ix2 = FilmIndex(path)                # vocab for typo-fixing is rebuilt on open
        self.assertEqual(len(ix2), 2)
        self.assertEqual(ix2.search("lionhart").hits[0].film.title, "Lionheart")
        self.assertEqual(ix2.search("pantera negra").hits[0].film.title, "Black Panther")

    def test_threads_can_search_concurrently(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            out = list(ex.map(lambda q: self.ix.search(q).kind, ["what 2020", "inception"] * 20))
        self.assertEqual(set(out), {"match"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
