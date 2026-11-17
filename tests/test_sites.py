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
            "http://evil.example/https://x.com/g/status/7\n"
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
            "https://x.com/g/status/7",
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


class TestUrlsAsPeopleWriteThem(Base):
    """Real spellings of X post URLs that the queue did not count (measured): a host or scheme in capitals (typed, or
    capitalised by a phone), and the m. hosts that X and yt-dlp use. They stayed out of the batch without a word."""

    def test_capitals_in_the_scheme_or_host_are_accepted_and_written_small(self):
        text = "https://X.com/User/status/1\nHTTPS://Twitter.COM/u/status/2\nHttps://T.co/AbC123xyz"
        self.assertEqual([sites.normalize_url(u) for u in sites.extract_urls(text)],
                         ["https://x.com/User/status/1", "https://twitter.com/u/status/2", "https://t.co/AbC123xyz"])

    def test_the_m_hosts_are_accepted(self):
        text = "https://m.twitter.com/u/status/3/video/1 https://m.x.com/u/status/4?s=20"
        self.assertEqual([sites.normalize_url(u) for u in sites.extract_urls(text)],
                         ["https://m.twitter.com/u/status/3", "https://m.x.com/u/status/4"])

    def test_the_same_post_written_differently_is_one_url(self):
        self.assertEqual(len(sites.extract_urls("https://x.com/u/status/5 https://X.COM/u/status/5/")), 1)

    def test_punctuation_of_the_text_around_a_url_is_not_part_of_it(self):
        # yt-dlp got "https://t.co/AbC123xyz)," and the like: a short link that no longer exists.
        text = ('(https://t.co/AbC123xyz), see https://t.co/Def456uvw. 「https://x.com/a/status/1」'
                '<https://t.co/Ghi789rst> "https://t.co/Jkl012mno"; https://t.co/Pqr345stu!')
        self.assertEqual(sites.extract_urls(text), [
            "https://t.co/AbC123xyz", "https://t.co/Def456uvw", "https://x.com/a/status/1",
            "https://t.co/Ghi789rst", "https://t.co/Jkl012mno", "https://t.co/Pqr345stu",
        ])

    def test_a_url_cut_short_with_an_ellipsis_does_not_count(self):
        # Display text shortens links ("https://t.co/AbC12…"): taken without the "…", it is another short link (or
        # another post), which can lead anywhere.
        self.assertEqual(sites.extract_urls("https://t.co/AbC12… https://x.com/u/status/12…"), [])

    def test_urls_glued_together_are_split_whatever_their_case(self):
        self.assertEqual(sites.extract_urls("https://x.com/a/status/1HTTPS://x.com/b/status/2"),
                         ["https://x.com/a/status/1", "HTTPS://x.com/b/status/2"])

    def test_pages_of_x_that_are_not_a_post_do_not_count(self):
        # They were counted as valid URLs, went to yt-dlp, which hands them to its generic extractor (measured), and
        # ended in failed_list.txt as "Unsupported URL".
        for url in ("https://x.com/", "https://x.com/someone", "https://x.com/someone/media", "https://twitter.com/home",
                    "https://x.com/search?q=cats", "https://x.com/i/bookmarks", "https://t.co/"):
            with self.subTest(url):
                self.assertIsNone(sites.find_site(url))
                self.assertEqual(sites.extract_urls(url), [])

    def test_an_id_or_code_followed_by_something_else_is_not_cut_short(self):
        # Cut at the NUL, the short link below would have become https://t.co/AbC, another link.
        for url in ("https://t.co/AbC\x00def", "https://t.co/AbC-def", "https://x.com/u/status/12abc"):
            with self.subTest(url=url):
                self.assertIsNone(sites.find_site(url))

    def test_everything_the_x_extractors_of_yt_dlp_take_still_counts(self):
        for url in ("https://x.com/u/status/1", "https://x.com/i/web/status/2", "https://x.com/i/status/3",
                    "https://twitter.com/statuses/4", "https://x.com/i/broadcasts/1ABCdefGHIjkl",
                    "https://x.com/i/spaces/1ABCdefGHIjkl", "https://twitter.com/i/cards/tfw/v1/5",
                    "https://twitter.com/i/videos/tweet/6", "https://t.co/AbC123xyz"):
            with self.subTest(url):
                self.assertIs(sites.find_site(url), sites.X)
                self.assertEqual(sites.normalize_url(url + "/?s=20"), url)

    def test_invisible_characters_copied_from_a_web_page_are_dropped(self):
        text = "https://t.co/AbC123xyz\u200b\nhttps://x.com/a/status/2\ufeff https://t.co/Def\u200d456uvw"
        self.assertEqual(sites.extract_urls(text),
                         ["https://t.co/AbC123xyz", "https://x.com/a/status/2", "https://t.co/Def456uvw"])


if __name__ == "__main__":
    unittest.main()
