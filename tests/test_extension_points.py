"""The documented extension points (see README "How to extend") really work with data only."""
import threading
import unittest
from unittest import mock

from common import Base, BrowserCookieManager, Config, DownloaderCore, DownloadThread, git_ignores, sites

EXAMPLE = sites.Site(
    name="Example", url_prefixes=("https://example.com/",), cookie_domains=("example.com",),
    login_cookie="sid", required_cookies=("sid",), probe_url="https://example.com/v/1",
)


class TestAddingASite(Base):
    def test_cookie_files_can_never_be_committed(self):
        # They hold the account's session token. A new Site's default cookies_<domain>.txt was once not covered.
        names = [s.cookies_name for s in sites.SITES] + [EXAMPLE.cookies_name]
        ignored = {name: git_ignores(name) for name in names}
        if None in ignored.values():
            self.skipTest("not a git checkout")
        self.assertEqual([name for name, ok in ignored.items() if not ok], [])

    def test_every_site_gets_its_own_cookies_file(self):
        self.assertEqual(sites.X.cookies_name, "cookies.txt")             # documented name, must not change
        self.assertEqual(EXAMPLE.cookies_name, "cookies_example.com.txt")
        names = [s.cookies_name for s in (sites.X, EXAMPLE)]
        self.assertEqual(len(set(names)), len(names))
        self.assertNotEqual(BrowserCookieManager(self.log, EXAMPLE).cookies_file,
                            BrowserCookieManager(self.log, sites.X).cookies_file)

    def test_url_helpers_pick_up_the_new_site(self):
        with mock.patch.object(sites, "SITES", [sites.X, EXAMPLE]):
            self.assertIs(sites.find_site("https://example.com/v/1"), EXAMPLE)
            self.assertEqual(sites.extract_urls("https://example.com/v/1 https://x.com/a/status/1 https://other.org/"),
                             ["https://example.com/v/1", "https://x.com/a/status/1"])

    def test_a_new_site_is_downloaded_with_the_defaults(self):
        self.install_fake_tools()
        mgr = BrowserCookieManager(self.log, EXAMPLE)
        mgr._cookie_args = []                                              # guest: no browser lookups
        core = DownloaderCore(self.log, {EXAMPLE.name: mgr})
        with mock.patch.object(sites, "SITES", [sites.X, EXAMPLE]):
            self.assertTrue(core.run("https://example.com/v/1"))
        args = self.calls()[0].split()
        first = args.index("-f")                                           # not necessarily the first option
        self.assertEqual(args[first:first + 2], ["-f", Config.FORMAT])
        self.assertEqual(args[-3:], ["--no-playlist", "--", "https://example.com/v/1"])


class TestPostDownloadHooks(Base):
    def test_hooks_given_to_the_download_thread_run_after_each_success(self):
        # README: pass post_hooks=[fn] where the GUI creates the DownloadThread.
        self.install_fake_tools()
        Config.RETRY_DELAY = 0
        done, seen = threading.Event(), []
        thread = DownloadThread(["https://x.com/a/status/1/video/1?ok", "https://x.com/gone/status/2"],
                                lambda pending: done.set(), post_hooks=[seen.append])
        thread.start()
        thread.join(30)
        self.assertTrue(done.is_set())
        self.assertEqual(seen, ["https://x.com/a/status/1"])              # normalised; not called for the failure


if __name__ == "__main__":
    unittest.main()
