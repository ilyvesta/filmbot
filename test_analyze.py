import sys, unittest
sys.path.insert(0, __file__.rsplit("/tests", 1)[0])
from scripts.analyze_db import analyze


class AnalyzeTests(unittest.TestCase):
    def test_counts_films_not_files(self):
        names = [
            "What.2020.1080p.WEB-DL.mkv", "What (2020) 720p BluRay.mkv", "What 2020 480p.mkv",
            "What.2008.720p.mkv", "Inception.2010.1080p.mkv",
            "Breaking.Bad.S01E01.720p.mkv", "(2007) 720p.mkv",
        ]
        r = analyze(names)
        self.assertEqual((r["files"], r["films"], r["series_files"]), (7, 3, 1))
        self.assertEqual(r["biggest"][0], ("what", 2020, 3))
        self.assertEqual(r["unparseable"], 1)
        self.assertEqual(r["films_without_year"], 0)


if __name__ == "__main__":
    unittest.main()
