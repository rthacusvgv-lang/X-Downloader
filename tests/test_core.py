"""core.py: one URL with its retries (DownloaderCore) and a whole batch (DownloadThread)."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from common import (
    BY_HOST, Base, BrowserCookieManager, Config, DownloadThread, DownloaderCore, EnvManager, PROJECT, ProcessManager,
    REAL_YTDLP, ShutdownRequested, atomic_write_lines, cookies_path, read_lines, sites, write_fake_ytdlp,
)
from xdownloader import core
from xdownloader.system import CancelRequested


class FakeToolsBase(Base):
    def setUp(self):
        super().setUp()
        self.install_fake_tools()
        Config.RETRY_DELAY = 0
        mgr = BrowserCookieManager(self.log, sites.X)
        mgr._cookie_args = []                      # guest: never touch the (sandboxed) browsers
        self.core = DownloaderCore(self.log, {sites.X.name: mgr})


class BatchBase(FakeToolsBase):
    def run_batch(self, urls, timeout=30):
        """Runs a DownloadThread like the GUI does; returns the URLs handed back through on_finish."""
        pending = []
        done = threading.Event()
        thread = DownloadThread(urls, lambda p: (pending.extend(p), done.set()))
        thread.start()
        self.assertTrue(done.wait(timeout), "on_finish was never called")
        thread.join(5)
        return pending


class TestDownloaderCore(Base):
    def setUp(self):
        super().setUp()
        self.install_fake_tools()
        Config.RETRY_DELAY = 0
        self.cm = BrowserCookieManager(self.log, sites.X)
        self.cm._cookie_args = ["--cookies", cookies_path()]

    def test_success_and_command_shape(self):
        hooks = []
        core = DownloaderCore(self.log, {sites.X.name: self.cm})
        core.add_post_hook(hooks.append)
        self.assertTrue(core.run("https://x.com/u/status/1/video/1?ok=1"))
        self.assertEqual(hooks, ["https://x.com/u/status/1"])
        args = self.calls()[0].split()
        self.assertIn("-S", args)
        self.assertEqual(args[args.index("-S") + 1], Config.FORMAT_SORT)
        self.assertIn("--merge-output-format", args)
        self.assertEqual(args[args.index("--merge-output-format") + 1], Config.MERGE_FORMAT)
        self.assertIn("--remux-video", args)
        self.assertEqual(args[args.index("--remux-video") + 1], Config.MERGE_FORMAT)
        self.assertIn("-N", args)
        self.assertEqual(args[args.index("-N") + 1], str(Config.CONCURRENT_FRAGMENTS))
        self.assertIn("--http-chunk-size", args)
        self.assertEqual(args[args.index("--http-chunk-size") + 1], Config.HTTP_CHUNK_SIZE)
        self.assertIn("-P", args)
        self.assertEqual(args[args.index("-P") + 1], self.base)
        self.assertIn("%(id)s", args[args.index("-o") + 1])
        self.assertEqual(args[-2:], ["--", "https://x.com/u/status/1"])

    def test_non_retryable(self):
        core = DownloaderCore(self.log, {sites.X.name: self.cm})
        self.assertFalse(core.run("https://x.com/gone/status/2"))
        self.assertEqual(len(self.calls()), 1)

    def test_retries(self):
        core = DownloaderCore(self.log, {sites.X.name: self.cm})
        self.assertFalse(core.run("https://x.com/flaky/status/3"))
        self.assertEqual(len(self.calls()), Config.MAX_RETRIES)

    def test_missing_binary_does_not_raise(self):
        os.remove(Config.YT_DLP_EXE)
        core = DownloaderCore(self.log, {sites.X.name: self.cm})
        self.assertFalse(core.run("https://x.com/u/status/4"))

    def test_shutdown_during_retry_wait(self):
        Config.RETRY_DELAY = 30
        core = DownloaderCore(self.log, {sites.X.name: self.cm})
        threading.Timer(0.5, ProcessManager.kill_all).start()
        t0 = time.time()
        with self.assertRaises(ShutdownRequested):
            core.run("https://x.com/flaky/status/5")
        self.assertLess(time.time() - t0, 5)

    def test_custom_download_dir_in_command(self):
        custom_target = os.path.join(self.base, "target_folder")
        Config.DOWNLOAD_DIR = custom_target
        core = DownloaderCore(self.log, {sites.X.name: self.cm})
        core.prepare(["https://x.com/u/status/100"])
        self.assertTrue(os.path.isdir(custom_target))  # auto-created in prepare()
        self.assertTrue(core.run("https://x.com/u/status/100?ok=1"))
        args = self.calls()[-1].split()
        self.assertIn("-P", args)
        self.assertEqual(args[args.index("-P") + 1], custom_target)


class TestDownloadThread(Base):
    def setUp(self):
        super().setUp()
        Config.RETRY_DELAY = 0
        self.done = []

    def run_thread(self, urls, timeout=30):
        ev = threading.Event()

        def on_finish(pending):
            self.done.append(pending)
            ev.set()
        t = DownloadThread(urls, on_finish)
        t.start()
        t.join(timeout)
        self.assertFalse(t.is_alive())
        return ev.is_set()

    def test_full_batch(self):
        self.install_fake_tools()
        atomic_write_lines(Config.LIST_FILE, ["https://x.com/old/status/9?ok"])
        urls = ["https://x.com/a/status/1?ok", "https://x.com/gone/status/2"]
        self.assertTrue(self.run_thread(urls))
        # The GUI list is the source of truth: stale list.txt lines (removed in the UI) are dropped
        self.assertFalse(os.path.exists(Config.PROCESSING_FILE))
        with open(Config.FAILED_FILE, encoding="utf-8") as f:
            self.assertEqual(f.read(), "https://x.com/gone/status/2\n")
        self.assertEqual(self.done, [[]])
        self.assertEqual(read_lines(Config.LIST_FILE), [])

    def test_external_additions_survive(self):
        self.install_fake_tools()
        urls = ["https://x.com/slowok/status/1?ok"]

        orig = DownloaderCore.run

        def run_and_append(core, url):
            # simulate the user appending to list.txt while downloading
            with open(Config.LIST_FILE, "a") as f:
                f.write("https://x.com/new/status/77\n")
            return orig(core, url)
        DownloaderCore.run = run_and_append
        try:
            self.assertTrue(self.run_thread(urls))
        finally:
            DownloaderCore.run = orig
        self.assertEqual(self.done, [["https://x.com/new/status/77"]])
        self.assertEqual(read_lines(Config.LIST_FILE), ["https://x.com/new/status/77"])

    def test_dependency_failure_keeps_urls(self):
        # Old bug: the GUI cleared the list even though nothing was downloaded.
        urls = ["https://x.com/a/status/1", "https://x.com/b/status/2"]
        self.assertTrue(self.run_thread(urls))
        self.assertEqual(self.done, [urls])
        self.assertEqual(read_lines(Config.LIST_FILE), urls)
        self.assertFalse(os.path.exists(Config.PROCESSING_FILE))

    def test_failed_list_reset_does_not_resurrect_done_urls(self):
        self.install_fake_tools()
        done_url = "https://x.com/a/status/1?ok"
        atomic_write_lines(Config.LIST_FILE, [done_url])
        real_write, real_run = core.atomic_write_lines, DownloaderCore.run
        state = {"failed": False}

        def flaky_write(path, lines, mode=None):
            if path == Config.LIST_FILE and lines == [] and not state["failed"]:
                state["failed"] = True              # only the reset at batch start fails
                raise PermissionError("simulated")
            return real_write(path, lines, mode)

        def run_and_append(c, url):
            with open(Config.LIST_FILE, "a") as f:
                f.write("https://x.com/new/status/77\n")
            return real_run(c, url)

        core.atomic_write_lines, DownloaderCore.run = flaky_write, run_and_append
        try:
            self.assertTrue(self.run_thread([done_url]))
        finally:
            core.atomic_write_lines, DownloaderCore.run = real_write, real_run
        self.assertTrue(state["failed"])
        self.assertEqual(self.done, [["https://x.com/new/status/77"]])
        self.assertEqual(read_lines(Config.LIST_FILE), ["https://x.com/new/status/77"])

    def test_shutdown_keeps_resume_queue(self):
        self.install_fake_tools()
        urls = ["https://x.com/a/status/1", "https://x.com/slow/status/2", "https://x.com/c/status/3"]
        threading.Timer(1.5, ProcessManager.kill_all).start()
        finished = self.run_thread(urls, timeout=20)
        self.assertFalse(finished)                     # on_finish not called during shutdown
        self.assertEqual(read_lines(Config.PROCESSING_FILE), urls[1:])
        self.assertFalse(os.path.exists(Config.FAILED_FILE))   # interrupted URL is not "failed"
        # exactly two yt-dlp invocations: no process spawned after kill_all
        self.assertEqual(len(self.calls()), 2)

    def test_batch_with_custom_download_dir(self):
        self.install_fake_tools()
        custom_target = os.path.join(self.base, "batch_target_dir")
        Config.DOWNLOAD_DIR = custom_target
        urls = ["https://x.com/batch/status/1?ok", "https://x.com/batch/status/2?ok"]
        self.assertTrue(self.run_thread(urls))
        self.assertEqual(self.done, [[]])
        self.assertTrue(os.path.isdir(custom_target))
        # Ensure yt-dlp received -P <custom_target> for every call
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        for c in calls:
            args = c.split()
            self.assertIn("-P", args)
            self.assertEqual(args[args.index("-P") + 1], custom_target)


@unittest.skipUnless(os.path.exists(REAL_YTDLP), "real yt-dlp not present")
class TestOutputTemplateLength(Base):
    """CJK / emoji display names used to overflow the 255-byte file name limit ("File name too long")."""

    def filename(self, uploader, text, template=None):
        info = {"id": "2098189997572870571", "title": f"{uploader} - {text}", "uploader": uploader,
                "ext": "mp4", "url": "http://127.0.0.1:9/x.mp4", "protocol": "http", "_type": "video"}
        path = os.path.join(self.base, "info.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        r = subprocess.run([REAL_YTDLP, "--ignore-config", "--load-info-json", path, "--print", "filename",
                            "-o", template or Config.OUTPUT_TEMPLATE],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_multibyte_names_stay_under_the_limit(self):
        for uploader, text in (("あ" * 50, "今日の絵です。" * 12), ("😀" * 50, "😀" * 80), ("株式会社" * 12, "中" * 80)):
            name = self.filename(uploader, text)
            # leave room for yt-dlp's own suffixes (.f1234, .part, .temp, ...)
            self.assertLess(len(name.encode()), 255 - 30, name)

    def test_ascii_names_are_unchanged(self):
        # Existing downloads must still be recognised as "already downloaded".
        legacy = "%(uploader)s_%(title).50s_%(id)s.%(ext)s"
        for uploader, text in (("1101sui", "x" * 120), ("N" * 50, "short"), ("Café Zoë", "Ça va très bien " * 5)):
            self.assertEqual(self.filename(uploader, text), self.filename(uploader, text, legacy))


@unittest.skipUnless(os.path.exists(REAL_YTDLP), "real yt-dlp not present")
class TestAFailedDownloadLogsWhatWentWrongNotItsProgress(Base):
    """yt-dlp writes its progress as updates separated by \\r, and the log turned each update into a line: one failed
    attempt of a 40 MiB download logged 329 lines, 311 of them progress (measured with the real yt-dlp). From about
    85 MiB on, yt-dlp's error was pushed out of the log panel, which keeps the last 900 to 1000 lines."""
    TOTAL, FAIL_AT = 4 << 20, 3 << 20        # the server answers 500 from the fourth MiB on

    def serve(self) -> str:
        total, fail_at = self.TOTAL, self.FAIL_AT

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                start = int(m.group(1)) if m else 0
                end = min(int(m.group(2)) if m and m.group(2) else total - 1, total - 1)
                if start >= fail_at:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206 if m else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(end - start + 1))
                if m:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.end_headers()
                try:
                    self.wfile.write(b"\0" * (end - start + 1))
                except OSError:
                    pass

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    def test_one_failed_attempt_logs_the_error_and_no_progress(self):
        self.install_fake_tools()
        shutil.copy(REAL_YTDLP, Config.YT_DLP_EXE)
        Config.MAX_RETRIES = 1
        prefix = self.serve()
        local = sites.Site(name="Loopback", url_prefixes=(prefix,), cookie_domains=("127.0.0.1",), login_cookie="sid",
                           required_cookies=("sid",), probe_url=prefix)
        mgr = BrowserCookieManager(self.log, local)
        mgr._cookie_args = []
        core = DownloaderCore(self.log, {local.name: mgr})
        with mock.patch.object(sites, "SITES", [local]), self.assertLogs(self.log, "DEBUG") as logs:
            self.assertFalse(core.run(prefix + "clip.mp4"))
        ytdlp = [line for line in logs.output if "yt-dlp:" in line]
        self.assertTrue(any("HTTP Error 500" in line for line in ytdlp), ytdlp[:5])
        self.assertEqual([line for line in ytdlp if re.search(r"\d% of", line)], [])
        self.assertLess(len(ytdlp), 40)


class TestTheNotLoggedInHintIsForGuestsOnly(FakeToolsBase):
    """X answers "No video could be found" for a post without a video too. The hint "you are not logged in" came with it
    even when the download had the login cookies (the real log: 1000 cookies extracted from Firefox, then that hint),
    which sent people after a login that was fine."""

    def hints(self, cookie_args):
        write_fake_ytdlp('echo "ERROR: [twitter] 1: No video could be found in this tweet" >&2; exit 1')
        mgr = BrowserCookieManager(self.log, sites.X)
        mgr._cookie_args = cookie_args
        core = DownloaderCore(self.log, {sites.X.name: mgr})
        with self.assertLogs(self.log, "INFO") as logs:
            self.assertFalse(core.run("https://x.com/u/status/1"))
        return [line for line in logs.output if "not logged into" in line]

    def test_no_hint_when_the_login_cookies_were_sent(self):
        self.assertEqual(self.hints(["--cookies", cookies_path()]), [])

    def test_a_guest_still_gets_the_hint(self):
        self.assertEqual(len(self.hints([])), 1)


class TestErrorsThatRetryingCannotFix(FakeToolsBase):
    """These were retried (with a wait in between) up to MAX_RETRIES times."""
    MESSAGES = ("unable to open for writing: [Errno 36] File name too long: 'x'",
                "unable to write data: [Errno 28] No space left on device")

    def test_are_not_retried(self):
        for message in self.MESSAGES:
            with self.subTest(message=message):
                before = len(self.calls())
                write_fake_ytdlp(f"""\
                    echo "$@" >> "$(dirname "$0")/calls.log"
                    echo "ERROR: {message}" >&2
                    exit 1""")
                self.assertFalse(self.core.run("https://x.com/u/status/1"))
                self.assertEqual(len(self.calls()) - before, 1)


class TestUndecodableOutput(FakeToolsBase):
    """A stray non-UTF-8 byte in yt-dlp's output used to raise UnicodeDecodeError and abort the whole batch."""
    BAD_OUTPUT = """\
        echo "$@" >> "$(dirname "$0")/calls.log"
        printf 'ERROR: bad \\377\\376 bytes\\n' >&2
        exit 1"""

    def test_process_manager_replaces_bad_bytes(self):
        r = ProcessManager.run(["sh", "-c", "printf 'bad \\377\\376 bytes' >&2; exit 1"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("bad", r.stderr)

    def test_batch_keeps_going(self):
        write_fake_ytdlp(self.BAD_OUTPUT)
        done = threading.Event()
        thread = DownloadThread(["https://x.com/a/status/1", "https://x.com/b/status/2"],
                                lambda pending: done.set())
        thread.start()
        thread.join(30)
        self.assertTrue(done.is_set())
        self.assertEqual(len(self.calls()), 2 * Config.MAX_RETRIES)      # both URLs were really attempted
        self.assertEqual(read_lines(Config.FAILED_FILE),
                         ["https://x.com/a/status/1", "https://x.com/b/status/2"])


class TestMaxAttemptsMeaning(FakeToolsBase):
    """The setting was labelled "Max Retries" but N meant N *attempts* in total (first try included):
    3 gave 2 retries, and 1 meant no retry at all. The text now says what the code does."""

    def attempts_for(self, setting, fails_before_success):
        # a URL whose first `fails_before_success` tries fail with a transient error
        Config.MAX_RETRIES = setting
        counter = os.path.join(self.base, "tries")
        if os.path.exists(counter):
            os.remove(counter)
        write_fake_ytdlp(f"""\\
            echo "$@" >> "$(dirname "$0")/calls.log"
            n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"
            if [ "$n" -le {fails_before_success} ]; then echo "ERROR: HTTP Error 500" >&2; exit 1; fi
            exit 0""")
        before = len(self.calls())
        ok = self.core.run("https://x.com/u/status/1")
        return ok, len(self.calls()) - before

    def test_the_setting_is_the_total_number_of_tries(self):
        for setting in (1, 2, 3, 5):
            with self.subTest(setting=setting):
                ok, tries = self.attempts_for(setting, fails_before_success=999)     # never succeeds
                self.assertEqual((ok, tries), (False, setting))

    def test_one_means_no_retry_two_means_one_retry(self):
        self.assertEqual(self.attempts_for(1, fails_before_success=1), (False, 1))
        self.assertEqual(self.attempts_for(2, fails_before_success=1), (True, 2))
        self.assertEqual(self.attempts_for(2, fails_before_success=2), (False, 2))
        self.assertEqual(self.attempts_for(3, fails_before_success=2), (True, 3))

    def test_the_texts_say_what_the_number_means_in_both_languages(self):
        from xdownloader import i18n
        for lang, label_word, first_try_word in (("en", "Attempts", "first try"), ("zh_TW", "嘗試", "含第一次")):
            self.assertIn(label_word, i18n.TRANSLATIONS[lang]["label_max_retries"], lang)
            self.assertIn(first_try_word, i18n.TRANSLATIONS[lang]["desc_max_retries"], lang)
        self.assertNotIn("Retries", i18n.TRANSLATIONS["en"]["label_max_retries"])
        self.assertNotIn("重試次數", i18n.TRANSLATIONS["zh_TW"]["label_max_retries"])


class TestFailedListHasNoDuplicates(FakeToolsBase):
    """failed_list.txt gained another copy of a URL every time a batch failed on it again."""

    def test_a_url_that_keeps_failing_is_listed_once(self):
        with open(Config.FAILED_FILE, "w") as f:
            f.write("https://x.com/earlier/status/1\n")
        for _ in range(3):
            done = threading.Event()
            thread = DownloadThread(["https://x.com/gone/status/9"], lambda pending: done.set())
            thread.start()
            thread.join(20)
            self.assertTrue(done.is_set())
        self.assertEqual(read_lines(Config.FAILED_FILE),
                         ["https://x.com/earlier/status/1", "https://x.com/gone/status/9"])


class TestOtherFilesAreNeverDeleted(FakeToolsBase):
    """A failed download used to delete *any* .part/.ytdl file older than a day in the download folder
    (other programs' partial downloads, e.g. Firefox / Transmission, live there too)."""

    def test_failed_download_leaves_the_folder_alone(self):
        old = time.time() - 3 * 86400
        names = ("ubuntu.iso.part", "paper.docx.part", "old.mp4.ytdl", "notes.txt")
        for name in names:
            path = os.path.join(self.base, name)
            open(path, "w").close()
            os.utime(path, (old, old))
        self.assertFalse(self.core.run("https://x.com/flaky/status/3"))
        self.assertEqual(len(self.calls()), Config.MAX_RETRIES)
        for name in names:
            self.assertTrue(os.path.exists(os.path.join(self.base, name)), name)


class TestListFileIsNotRewrittenNeedlessly(FakeToolsBase):
    """A batch used to replace list.txt (temp file + fsync, i.e. milliseconds) even when its content did not
    change, so a URL another program appended in that window was silently lost."""

    def run_batch(self, urls):
        done = threading.Event()
        thread = DownloadThread(urls, lambda pending: done.set())
        thread.start()
        thread.join(30)
        self.assertTrue(done.is_set())

    def test_unchanged_list_file_is_left_alone(self):
        open(Config.LIST_FILE, "w").close()                  # nothing stale, nothing appended
        inode = os.stat(Config.LIST_FILE).st_ino
        self.run_batch(["https://x.com/a/status/1?ok"])
        self.assertEqual(os.stat(Config.LIST_FILE).st_ino, inode)      # never replaced

    def test_urls_appended_during_the_batch_are_kept_without_a_rewrite(self):
        open(Config.LIST_FILE, "w").close()
        inode = os.stat(Config.LIST_FILE).st_ino
        orig_run = DownloaderCore.run

        def run_and_append(core, url):
            with open(Config.LIST_FILE, "a") as f:             # another program appends mid-download
                f.write("https://x.com/new/status/7\n")
            return orig_run(core, url)

        with mock.patch.object(DownloaderCore, "run", run_and_append):
            self.run_batch(["https://x.com/a/status/1?ok"])
        self.assertEqual(read_lines(Config.LIST_FILE), ["https://x.com/new/status/7"])
        self.assertEqual(os.stat(Config.LIST_FILE).st_ino, inode)


class TestErrorsAreJudgedByWhatYtDlpReportsOnStderr(FakeToolsBase):
    """yt-dlp prints "[download] Destination: <title>" on stdout, and a title can hold any phrase. A tweet whose title
    said "does not exist" was given up after one try, and one with "Connection reset" in it was taken for a dead
    network (its URL stayed in the list, and three in a row stopped the batch), whatever had really gone wrong."""

    def fail_with_title(self, title, error="ERROR: unable to download video data: HTTP Error 503: Service Unavailable"):
        write_fake_ytdlp(f"""\
            echo "$@" >> "$(dirname "$0")/calls.log"
            echo "[download] Destination: /x/NA_{title}_1.mp4"
            echo "{error}" >&2
            exit 1""")

    def test_a_title_that_sounds_like_an_unrecoverable_error_is_still_retried(self):
        self.fail_with_title("Mars does not exist anymore")
        self.assertFalse(self.core.run("https://x.com/u/status/1"))
        self.assertEqual(len(self.calls()), Config.MAX_RETRIES)

    def test_a_title_that_sounds_like_a_dead_network_is_not_one(self):
        self.fail_with_title("Connection reset by peer lol")
        self.assertFalse(self.core.run("https://x.com/u/status/1"))
        self.assertFalse(self.core.network_failure)

    def test_what_yt_dlp_reports_on_stderr_still_counts(self):
        self.fail_with_title("Sunset timelapse", "ERROR: [twitter] 1: No video could be found in this tweet")
        self.assertFalse(self.core.run("https://x.com/u/status/1"))
        self.assertEqual(len(self.calls()), 1)                                  # unrecoverable: not retried
        self.fail_with_title("Sunset timelapse", "ERROR: <urlopen error [Errno -3] Temporary failure in name resolution>")
        self.assertFalse(self.core.run("https://x.com/u/status/2"))
        self.assertTrue(self.core.network_failure)


class TestNetworkFlagBelongsToTheLastUrl(FakeToolsBase):
    """DownloaderCore.network_failure says whether the *last* run() failed because the network is down; a URL that
    run() turned away at once (unsupported site) kept the answer of the URL before it."""

    def test_a_url_that_is_not_attempted_is_not_blamed_on_the_network(self):
        write_fake_ytdlp("""\
            echo "ERROR: <urlopen error [Errno 111] Connection refused>" >&2
            exit 1""")
        self.assertFalse(self.core.run("https://x.com/u/status/1"))
        self.assertTrue(self.core.network_failure)
        self.assertFalse(self.core.run("https://example.org/not-a-supported-site"))
        self.assertFalse(self.core.network_failure)


class TestFailedListIsWrittenLikeEveryOtherFile(FakeToolsBase):
    """failed_list.txt was the one file that was appended to in place instead of getting the atomic write (temp file +
    fsync + rename) of all the others. A list that ends without a newline (edited by hand) also glued the next URL
    onto its last line."""

    def test_a_list_without_a_final_newline_gets_the_next_url_on_its_own_line(self):
        with open(Config.FAILED_FILE, "w") as f:
            f.write("https://x.com/old/status/1")                          # no newline at the end
        done = threading.Event()
        thread = DownloadThread(["https://x.com/gone/status/9"], lambda pending: done.set())
        thread.start()
        thread.join(20)
        self.assertTrue(done.is_set())
        self.assertEqual(read_lines(Config.FAILED_FILE), ["https://x.com/old/status/1", "https://x.com/gone/status/9"])

    def test_a_write_that_dies_before_the_rename_leaves_the_old_list_intact(self):
        with open(Config.FAILED_FILE, "w") as f:
            f.write("https://x.com/old/status/1\n")
        child = textwrap.dedent(f"""\
            import os, signal, sys
            sys.path.insert(0, {PROJECT!r})
            from xdownloader import fsutil
            from xdownloader.config import Config
            from xdownloader.core import DownloadThread
            Config.FAILED_FILE = {Config.FAILED_FILE!r}
            Config.LIST_FILE = {Config.LIST_FILE!r}
            fsutil.os.replace = lambda *args: os.kill(os.getpid(), signal.SIGKILL)
            DownloadThread([], lambda pending: None)._record_failure("https://x.com/gone/status/9")
        """)
        run = subprocess.run([sys.executable, "-I", "-B", "-c", child], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, -signal.SIGKILL, run.stderr)     # the premise: it died right before the rename
        self.assertEqual(read_lines(Config.FAILED_FILE), ["https://x.com/old/status/1"])


class TestYtDlpIgnoresConfigFiles(FakeToolsBase):
    """yt-dlp also reads a yt-dlp.conf from the folder given to -P (the download folder) and, without -P, from the
    current folder. Only the cookie probe passed --ignore-config, so a download folder that other people can write
    to (a shared drive, a synced folder) could change what yt-dlp does."""

    def test_every_yt_dlp_call_ignores_config_files(self):
        write_fake_ytdlp("""\
            echo "$@" >> "$(dirname "$0")/calls.log"
            exit 0
        """)
        self.assertTrue(self.core.run("https://x.com/u/status/1"))
        EnvManager(self.log).update_yt_dlp()
        BrowserCookieManager(self.log, sites.X)._test_browser_cookies("firefox")
        calls = self.calls()
        self.assertEqual(len(calls), 3, calls)        # download, update, cookie probe
        self.assertEqual([c for c in calls if "--ignore-config" not in c.split()], [])


class TestUndecodableQueueFiles(BatchBase):
    """A list.txt note saved as Big5 / UTF-16 made read_lines() raise: the batch aborted, nothing was downloaded."""

    def write_bad_list(self, *lines):
        with open(Config.LIST_FILE, "wb") as f:
            f.write(b"\xff\xfe\xfa note\n" + "".join(l + "\n" for l in lines).encode())

    def test_read_lines_keeps_the_urls(self):
        self.write_bad_list("https://x.com/ok/status/1")
        self.assertIn("https://x.com/ok/status/1", read_lines(Config.LIST_FILE))

    def test_batch_runs_and_list_survives(self):
        self.write_bad_list("https://x.com/ok/status/1")
        pending = self.run_batch(["https://x.com/ok/status/1", "https://x.com/ok/status/2"])
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(pending, [])


class TestOneBadUrlDoesNotStopTheBatch(BatchBase):
    """An exception other than OSError / CalledProcessError (NUL byte -> ValueError) aborted the batch,
    and the offending URL was put back first, so every rerun stopped at the same place."""
    POISON = "https://x.com/a\x00b/status/1"        # a NUL byte reaches subprocess, which raises ValueError

    def test_rest_of_the_batch_is_downloaded_and_the_url_is_recorded(self):
        pending = self.run_batch(["https://x.com/ok/status/1", self.POISON, "https://x.com/ok/status/2"])
        self.assertEqual(len(self.calls()), 2)                     # both good URLs were downloaded
        self.assertEqual(read_lines(Config.FAILED_FILE), [self.POISON])
        self.assertEqual(pending, [])


class TestFailedListUnwritable(BatchBase):
    """A failed URL used to vanish without a trace when failed_list.txt could not be written."""

    def test_url_is_named_in_the_log(self):
        os.mkdir(Config.FAILED_FILE)                              # reading it, like replacing it -> IsADirectoryError
        with self.assertLogs("XDownloader", level="ERROR") as logs:
            self.run_batch(["https://x.com/gone/status/9"])
        self.assertTrue(any("failed_list.txt" in line and "https://x.com/gone/status/9" in line
                            for line in logs.output), logs.output)


class TestNetworkDown(BatchBase):
    """A dead network used to push the whole queue into failed_list.txt and empty the GUI list."""

    def setUp(self):
        super().setUp()
        write_fake_ytdlp(BY_HOST)

    def test_batch_stops_and_keeps_every_url(self):
        urls = [f"https://x.com/dns{i}/status/{i}" for i in range(1, 7)]
        pending = self.run_batch(urls)
        self.assertEqual(len(self.calls()), 3 * Config.MAX_RETRIES)   # stopped after the 3rd URL
        self.assertEqual(pending, urls)                               # nothing lost, original order
        self.assertFalse(os.path.exists(Config.FAILED_FILE))

    def test_a_success_resets_the_count(self):
        urls = ["https://x.com/dns1/status/1", "https://x.com/dns2/status/2", "https://x.com/ok/status/3",
                "https://x.com/dns4/status/4", "https://x.com/dns5/status/5", "https://x.com/ok/status/6"]
        pending = self.run_batch(urls)
        self.assertEqual(len(self.calls()), 4 * Config.MAX_RETRIES + 2)   # every URL was tried
        self.assertEqual(pending, [urls[0], urls[1], urls[3], urls[4]])   # only the offline ones come back
        self.assertFalse(os.path.exists(Config.FAILED_FILE))

    def test_ordinary_failures_still_go_to_failed_list(self):
        urls = [f"https://x.com/gone/status/{i}" for i in range(1, 6)]
        pending = self.run_batch(urls)
        self.assertEqual(read_lines(Config.FAILED_FILE), urls)            # not mistaken for a network problem
        self.assertEqual(pending, [])


class TestCancel(BatchBase):
    """Nothing could end a stuck download except closing the program."""

    def setUp(self):
        super().setUp()
        ProcessManager.clear_cancel()
        self.addCleanup(ProcessManager.clear_cancel)
        write_fake_ytdlp(BY_HOST)

    def start(self, urls):
        pending, done = [], threading.Event()
        thread = DownloadThread(urls, lambda p: (pending.extend(p), done.set()))
        thread.start()
        return thread, pending, done

    def wait_for_calls(self, n):
        end = time.time() + 10
        while len(self.calls()) < n and time.time() < end:
            time.sleep(0.02)
        self.assertGreaterEqual(len(self.calls()), n)

    def test_cancel_stops_the_running_download_and_returns_the_urls(self):
        urls = ["https://x.com/ok/status/1", "https://x.com/slow/status/2", "https://x.com/ok/status/3"]
        thread, pending, done = self.start(urls)
        self.wait_for_calls(2)                                        # the slow download is running
        ProcessManager.cancel()
        self.assertTrue(done.wait(10), "cancel did not end the batch")
        self.assertEqual(pending, urls[1:])                           # the interrupted URL is not 'failed'
        self.assertFalse(os.path.exists(Config.FAILED_FILE))
        self.assertFalse(ProcessManager.is_shutting_down())           # the program itself keeps running

    def test_cancel_interrupts_the_wait_between_retries(self):
        Config.RETRY_DELAY = 60
        write_fake_ytdlp(BY_HOST.replace("*slow*) sleep 30 ;;", "*slow*) echo 'ERROR: HTTP Error 500' >&2; exit 1 ;;"))
        thread, pending, done = self.start(["https://x.com/slow/status/1"])
        self.wait_for_calls(1)
        time.sleep(0.3)                                               # now inside the 60 s wait
        ProcessManager.cancel()
        self.assertTrue(done.wait(5), "cancel did not interrupt the retry wait")
        self.assertEqual(pending, ["https://x.com/slow/status/1"])

    def test_no_new_process_after_cancel_until_cleared(self):
        ProcessManager.cancel()
        with self.assertRaises(CancelRequested):
            DownloaderCore(self.log).run("https://x.com/ok/status/1")
        self.assertEqual(self.calls(), [])
        ProcessManager.clear_cancel()
        self.assertTrue(DownloaderCore(self.log).run("https://x.com/ok/status/1"))


class TestListChangesDuringABatch(Base):
    """list.txt edits around a running batch: a URL added again during its own download is kept, one removed in the
    window stays gone, and URLs appended as the batch starts are kept."""

    # --- 2. Core: DownloadThread Queue Concurrency & Stale List ---
    def test_core_readded_identical_url_during_download_survives(self):
        """CRITICAL: If user re-adds an identical URL during download, it MUST NOT be dropped as stale."""
        self.install_fake_tools()
        batch_url = "https://x.com/user/status/100?ok"
        atomic_write_lines(Config.LIST_FILE, [batch_url])

        done = []
        ev = threading.Event()

        def on_finish(pending):
            done.append(pending)
            ev.set()

        orig_run = DownloaderCore.run

        def slow_run(core_inst, url):
            # Mid-download: user appends the EXACT SAME URL back into list.txt
            with open(Config.LIST_FILE, "a") as f:
                f.write(f"{batch_url}\n")
            return orig_run(core_inst, url)

        with mock.patch.object(DownloaderCore, "run", side_effect=slow_run, autospec=True):
            thread = DownloadThread([batch_url], on_finish=on_finish)
            thread.start()
            thread.join(timeout=10)

        self.assertTrue(ev.is_set())
        # The re-added URL MUST survive into list.txt!
        self.assertIn(batch_url, read_lines(Config.LIST_FILE))
        self.assertEqual(done, [[batch_url]])

    def test_core_dropped_url_in_ui_does_not_resurrect(self):
        """If user removed a URL in UI before download, it must NOT come back after download."""
        self.install_fake_tools()
        stale_url = "https://x.com/stale_removed_in_ui/status/1"
        kept_url = "https://x.com/ok/status/2"

        # Initially list.txt has both
        atomic_write_lines(Config.LIST_FILE, [stale_url, kept_url])

        done = []
        # UI only submits kept_url
        thread = DownloadThread([kept_url], on_finish=done.append)
        thread.start()
        thread.join(timeout=10)

        # stale_url must be gone!
        self.assertEqual(read_lines(Config.LIST_FILE), [])
        self.assertEqual(done, [[]])

    def test_core_external_additions_before_run_start_survive(self):
        """A URL appended to list.txt after the batch was set up (its list.txt lines are known) but before it starts
        counts as new and is kept. (The append used to race the start of the batch, 50 times over: one that lands in the
        milliseconds while list.txt is rewritten is lost, as the README says, and under load about half the runs
        failed. That window cannot be closed without the appending program's cooperation.)"""
        self.install_fake_tools()
        batch_url = "https://x.com/ok/status/1"
        ext_url = "https://x.com/ext/status/2"
        atomic_write_lines(Config.LIST_FILE, [batch_url])
        done = threading.Event()
        thread = DownloadThread([batch_url], on_finish=lambda p: done.set())
        with open(Config.LIST_FILE, "a") as f:
            f.write(f"{ext_url}\n")
        thread.start()
        self.assertTrue(done.wait(10))
        self.assertEqual(read_lines(Config.LIST_FILE), [ext_url])


if __name__ == "__main__":
    unittest.main()
