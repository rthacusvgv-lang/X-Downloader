"""Download logic: DownloaderCore (one URL, with retries) and DownloadThread (a whole batch)."""
import logging
import os
import subprocess
import threading

from .config import Config
from .cookies import BrowserCookieManager
from .env import EnvManager
from .fsutil import folder_problem
from .sites import Site, find_site
from .system import (
    CancelRequested, ProcessManager, QueueManager, ShutdownRequested,
    atomic_write_lines, merge_unique, read_lines,
)

# yt-dlp messages meaning "the network is down", not "this URL is bad".
NETWORK_ERRORS = (
    "Temporary failure in name resolution", "Name or service not known", "Failed to resolve",
    "Network is unreachable", "No route to host", "Connection refused",
    "Connection reset", "timed out",
)
# Consecutive network failures after which a batch stops instead of working through the whole queue.
OFFLINE_STREAK_LIMIT = 3


class DownloaderCore:
    def __init__(self, logger: logging.Logger,
                 cookie_managers: dict[str, BrowserCookieManager] | None = None,
                 post_hooks: list = None):
        self._logger = logger
        self._cookie_mgrs = dict(cookie_managers or {})   # Site.name -> manager
        self._post_hooks: list = post_hooks or []
        self.network_failure = False    # the last run() failed because the network is down

    def add_post_hook(self, fn) -> None:
        """Registers fn(url), called after every successful download."""
        self._post_hooks.append(fn)

    def cookies_for(self, site: Site) -> BrowserCookieManager:
        mgr = self._cookie_mgrs.get(site.name)
        if mgr is None:
            mgr = self._cookie_mgrs[site.name] = BrowserCookieManager(self._logger, site)
        return mgr

    def prepare(self, urls: list[str]) -> None:
        """Resolves the cookie source of every site used by *urls* (once per batch)."""
        try:
            os.makedirs(Config.DOWNLOAD_DIR, exist_ok=True)
        except OSError:
            pass
        sites = {s.name: s for s in map(find_site, urls) if s}
        for site in sites.values():
            if not self.cookies_for(site).ensure_cookies():
                self._logger.warning(
                    "⚠️ No valid %s authentication detected. Attempting download anyway "
                    "(success rate may be low).", site.name
                )

    def _base_cmd(self, url: str, cookie_args: list[str]) -> list[str]:
        cmd = [
            Config.YT_DLP_EXE,
            # Otherwise yt-dlp also reads a yt-dlp.conf from the -P folder, which may be shared with other people.
            "--ignore-config",
            # Its progress comes as updates separated by \r, which the log would show as one line each (hundreds per
            # failed attempt, pushing yt-dlp's error out of the log panel). Nothing here shows progress.
            "--no-progress",
            "-f", Config.FORMAT,
            "-S", Config.FORMAT_SORT,
            "-N", str(Config.CONCURRENT_FRAGMENTS),
            "--http-chunk-size", Config.HTTP_CHUNK_SIZE,
        ]
        cmd.extend(cookie_args)
        cmd.extend([
            "-P", Config.DOWNLOAD_DIR,
            "-o", Config.OUTPUT_TEMPLATE,
            "--merge-output-format", Config.MERGE_FORMAT,
            "--remux-video", Config.MERGE_FORMAT,
            "--ffmpeg-location", os.path.dirname(Config.FFMPEG_EXE),   # project dir, or the system's (env.py)
            "--no-playlist",
            "--", url,
        ])
        return cmd

    def run(self, url: str) -> bool:
        """Downloads one URL. Raises ShutdownRequested if the application is closing."""
        self.network_failure = False        # an answer about this URL only, also when it is turned away below
        site = find_site(url)
        if site is None:
            self._logger.error("❌ Unsupported site, skipping: %s", url)
            return False

        original_url = url
        url = site.normalize(url)
        if url != original_url:
            self._logger.info("🔗 URL normalized: %s → %s", original_url, url)

        cookies = self.cookies_for(site)
        non_retryable = Config.NON_RETRYABLE_PATTERNS + list(site.non_retryable)

        for attempt in range(1, Config.MAX_RETRIES + 1):
            self._logger.info("[%d/%d] Preparing download: %s", attempt, Config.MAX_RETRIES, url)
            try:
                with cookies.cookie_args() as cookie_args:        # for Firefox: a copy that lives as long as this run
                    result = ProcessManager.run(
                        self._base_cmd(url, cookie_args), check=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                if result.stderr:
                    for line in result.stderr.strip().splitlines():
                        self._logger.debug("  yt-dlp: %s", line)
                self._logger.info("✅ Download successful")
                for hook in self._post_hooks:
                    try:
                        hook(url)
                    except Exception as e:
                        self._logger.warning("Post-hook failed: %s", e)
                return True
            except OSError as e:
                # The binary is missing or not executable: retrying cannot help, and letting the
                # exception escape used to abort the whole batch.
                self._logger.error("❌ Cannot execute yt-dlp: %s", e)
                return False
            except subprocess.CalledProcessError as e:
                # What went wrong is what yt-dlp reports on stderr. Its stdout carries progress lines such as
                # "[download] Destination: <title>", and a title can hold any phrase ("does not exist", "timed out").
                errors = e.stderr or ""
                self.network_failure = any(pat in errors for pat in NETWORK_ERRORS)

                self._logger.warning(
                    "❌ Download failed (attempt %d/%d): return code %d",
                    attempt, Config.MAX_RETRIES, e.returncode
                )
                output = errors + (e.stdout or "")        # the log shows both
                if output.strip():
                    for line in output.strip().splitlines():
                        if line.strip():
                            self._logger.warning("  yt-dlp: %s", line.strip())

                # Only for a guest: with the login cookies sent, X's answer means what it says (no video there).
                if site.login_error and site.login_error in errors and not cookies.get_cookie_args():
                    self._logger.error(
                        "💡 Hint: This error usually means you are not logged into %s.", site.name
                    )
                    self._logger.error("   %s", site.login_hint)

                if any(pat in errors for pat in non_retryable):
                    self._logger.error(
                        "⚠️ Unrecoverable error detected, skipping retries: %s", url
                    )
                    return False

                # No .part cleanup here: yt-dlp resumes its own partial files, and the download
                # folder may also hold other programs' partial downloads.
                if attempt < Config.MAX_RETRIES:
                    self._logger.info("Waiting %d second(s) before retrying...", Config.RETRY_DELAY)
                    if ProcessManager.wait(Config.RETRY_DELAY):
                        ProcessManager.raise_if_stopped()
                else:
                    self._logger.error("⚠️ Reached the maximum number of attempts, giving up on download: %s", url)
        return False


class DownloadThread(threading.Thread):
    """Background thread: syncs queue -> checks dependencies -> downloads one by one.

    on_finish(pending) is called with the URLs that still need to be downloaded
    (empty on normal completion) unless the application is shutting down.
    """
    def __init__(self, urls: list[str], on_finish, post_hooks: list = None):
        super().__init__(daemon=True)
        self.urls = urls
        self._on_finish = on_finish
        self._post_hooks = list(post_hooks) if post_hooks else []
        self._logger = logging.getLogger("XDownloader")
        try:
            self._stale_list: set[str] = set(read_lines(Config.LIST_FILE))
        except (OSError, UnicodeDecodeError):
            self._stale_list: set[str] = set()

    def run(self):
        urls = [u.strip() for u in self.urls if u.strip()]
        remaining = list(urls)
        queue_mgr = QueueManager(self._logger)
        interrupted = False
        try:
            if not urls:
                self._logger.info("Queue is empty, nothing to do.")
                return

            # Double-buffering: the active queue lives in .processing.list (persisted first,
            # so a crash at any later point can be resumed); list.txt is released and is
            # not touched again until the batch ends, so URLs may be appended to it meanwhile.
            queue_mgr.save(urls)
            try:
                # The GUI list (= urls) is the source of truth; keep any new external additions
                # that arrived since thread creation rather than wiping them out.
                current = read_lines(Config.LIST_FILE)
                batch_set = set(urls)
                new_additions = [u for u in current if u not in self._stale_list and u not in batch_set]
                if new_additions != current:       # an unchanged file is left alone (see _finish)
                    atomic_write_lines(Config.LIST_FILE, new_additions)
                # Successful reset means list.txt now holds only clean new additions;
                # clear stale_list so re-added URLs during download won't be discarded.
                self._stale_list = set()
            except OSError as e:
                self._logger.warning("Cannot reset list.txt: %s", e)
                try:
                    # Its current lines are batch leftovers, not new additions (see _finish).
                    self._stale_list = set(read_lines(Config.LIST_FILE))
                except (OSError, UnicodeDecodeError):
                    pass

            if not EnvManager(self._logger).check_all():
                self._logger.error("Dependencies missing, download aborted.")
                return

            # Fail now, once, instead of after MAX_RETRIES attempts for every single URL.
            try:
                os.makedirs(Config.DOWNLOAD_DIR, exist_ok=True)
            except OSError:
                pass                                   # reported by folder_problem() below
            problem = folder_problem(Config.DOWNLOAD_DIR)
            if problem:
                self._logger.error("Cannot use the download folder %s: %s", Config.DOWNLOAD_DIR, problem[1])
                return

            core = DownloaderCore(self._logger, post_hooks=self._post_hooks)
            core.prepare(urls)
            success_count = failed_count = 0
            offline: list[str] = []     # failed only because the network is down: kept, not recorded as failed
            streak = 0                  # consecutive network failures
            remaining = []

            for i, url in enumerate(urls, 1):
                self._logger.info("-" * 40)
                self._logger.info("Processing [%d/%d]: %s", i, len(urls), url)

                # Atomic update of .processing.list (core of crash-resume)
                remaining = offline + urls[i-1:]
                queue_mgr.save(remaining)

                try:
                    ok = core.run(url)
                except ShutdownRequested:
                    raise
                except Exception:
                    # One URL must not take the rest of the queue down with it.
                    self._logger.error("Unexpected error while downloading %s", url, exc_info=True)
                    ok = False

                if ok:
                    success_count += 1
                    streak = 0
                elif core.network_failure:
                    offline.append(url)
                    streak += 1
                    if streak >= OFFLINE_STREAK_LIMIT:
                        self._logger.warning(
                            "The network seems to be down (%d failure(s) in a row); stopping. "
                            "%d URL(s) stay in the list.", streak, len(offline) + len(urls) - i
                        )
                        remaining = offline + urls[i:]
                        break
                else:
                    failed_count += 1
                    streak = 0
                    self._record_failure(url)
            else:
                remaining = offline

            self._logger.info("=" * 44)
            self._logger.info(
                "🎉 Task complete! Successfully downloaded %d / %d video(s).",
                success_count, len(urls)
            )
            if failed_count:
                self._logger.warning("Some URLs failed to download. Check failed_list.txt for details.")

        except CancelRequested:
            # Not 'interrupted': _finish() still runs and hands the unfinished URLs back to the list.
            self._logger.warning("Download cancelled; %d URL(s) stay in the list.", len(remaining))
        except ShutdownRequested:
            interrupted = True
            self._logger.warning(
                "Download interrupted by shutdown; %d URL(s) kept in .processing.list for resume.",
                len(remaining)
            )
        except Exception:
            self._logger.critical("Unexpected fatal error in background thread.", exc_info=True)
        finally:
            # On shutdown keep .processing.list untouched: it is resumed on the next launch.
            if not (interrupted or ProcessManager.is_shutting_down()):
                self._finish(remaining, queue_mgr)

    def _record_failure(self, url: str):
        # Listed once: the file used to gain another copy of a URL each time a batch retried it.
        try:
            failed = read_lines(Config.FAILED_FILE)
            if url not in failed:
                atomic_write_lines(Config.FAILED_FILE, failed + [url])
        except OSError as e:
            # The URL is not queued any more: the log is the only place left that names it.
            self._logger.error("Cannot record the failed URL in failed_list.txt (%s): %s", e, url)

    def _finish(self, remaining: list[str], queue_mgr: QueueManager):
        # Hand unfinished URLs back to list.txt (plus anything appended meanwhile)
        # and retire the processing queue.
        pending = remaining
        try:
            current = read_lines(Config.LIST_FILE)
            added = [u for u in current if u not in self._stale_list]
            pending = merge_unique(remaining, added)
            # Rewriting replaces the file (temp file + fsync takes milliseconds): a URL appended by
            # another program in that window would be lost, so only rewrite when something changed.
            if pending != current:
                atomic_write_lines(Config.LIST_FILE, pending)
            queue_mgr.clear()
        except (OSError, UnicodeDecodeError) as e:
            self._logger.error("Cannot update list.txt: %s", e)
        self._on_finish(pending)
