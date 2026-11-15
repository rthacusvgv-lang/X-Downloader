"""sites.py: which URLs are accepted, and how they are normalized."""
import dataclasses
import unittest

from common import Base, DownloaderCore, merge_unique, sites


class TestNormalizeAndExtract(Base):
    def test_normalize(self):
        n = sites.normalize_url
        cases = {
            "https://x.com/u/status/123": "https://x.com/u/status/123",
            "https://x.com/u/status/123/": "https://x.com/u/status/123",
            "https://x.com/u/status/123?s=20&t=abc": "https://x.com/u/status/123",
            "https://x.com/u/status/123/video/1": "https://x.com/u/status/123",
            "https://x.com/u/status/123/photo/2#frag": "https://x.com/u/status/123",
            "https://twitter.com/u/status/123/analytics": "https://twitter.com/u/status/123",
            "https://x.com/i/web/status/555": "https://x.com/i/web/status/555",
            # Regressions: the old substring search truncated these to "https://x.com"
            "https://x.com/likesbot/status/42": "https://x.com/likesbot/status/42",
            "https://x.com/video/status/42": "https://x.com/video/status/42",
            "https://x.com/historyfan/status/42/video/1": "https://x.com/historyfan/status/42",
            "https://x.com/hiddengem/status/7": "https://x.com/hiddengem/status/7",
            # Non-status URLs are left alone (minus query/fragment/trailing slash)
            "https://x.com/i/spaces/1abc?x=1": "https://x.com/i/spaces/1abc",
        }
        for src, want in cases.items():
            self.assertEqual(n(src), want, src)

    def test_site_can_keep_identifying_query_parameters(self):
        yt = dataclasses.replace(sites.X, canonical_url=None, keep_query=("v",))
        self.assertEqual(yt.normalize("https://x.com/watch/?v=abc&t=5&s=1#frag"), "https://x.com/watch?v=abc")
        self.assertEqual(yt.normalize("https://x.com/watch?t=5"), "https://x.com/watch")
        self.assertEqual(sites.X.normalize("https://x.com/u/status/1?v=abc"), "https://x.com/u/status/1")

    def test_extract_urls(self):
        text = (
            "https://x.com/a/status/1\n"
            "https://x.com/a/status/1/video/1\n"          # duplicate after normalisation
            "https://x.com/b/status/2https://x.com/c/status/3\n"   # glued paste
            "https://x.com/d/status/4   my note here\n"   # trailing note
            "[2026-09-28 17:50:24] https://twitter.com/e/status/5\n"  # old failed_list format
            "http://evil.example/https://x.com/\n"
            "ftp://x.com/a\n"
            "just text\n"
            "https://www.x.com/f/status/6\n"
        )
        self.assertEqual(sites.extract_urls(text), [
            "https://x.com/a/status/1",
            "https://x.com/b/status/2",
            "https://x.com/c/status/3",
            "https://x.com/d/status/4",
            "https://twitter.com/e/status/5",
            "https://x.com/",
            "https://www.x.com/f/status/6",
        ])

    def test_merge_unique(self):
        self.assertEqual(merge_unique(["a", "b", " "], ["b", "c", "a"]), ["a", "b", "c"])

    def test_tco_short_links_are_accepted_unchanged(self):
        self.assertIs(sites.find_site("https://t.co/AbC123xyz"), sites.X)
        self.assertEqual(sites.normalize_url("https://t.co/AbC123xyz?amp=1/"), "https://t.co/AbC123xyz")
        self.assertIsNone(sites.find_site("http://t.co/AbC123xyz"))
        self.assertIsNone(sites.find_site("https://t.com/AbC123xyz"))
        self.assertEqual(sites.extract_urls("see https://t.co/AbC123xyz\nhttps://t.co/AbC123xyz/"),
                         ["https://t.co/AbC123xyz"])
        cmd = DownloaderCore(self.log)._base_cmd("https://t.co/AbC123xyz", [])
        self.assertEqual(cmd[-2:], ["--", "https://t.co/AbC123xyz"])


if __name__ == "__main__":
    unittest.main()
