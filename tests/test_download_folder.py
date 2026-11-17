"""The download folder: what may be chosen, and what happens when it cannot be used.

Everything here was verified by experiment: the settings page used to accept /etc, /usr/bin, /root or a
read-only folder with "✓ Auto-saved", and a batch then retried every URL three times (30 URLs: about 5 minutes).
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from common import Base, BrowserCookieManager, Config, DownloadThread, DownloaderCore, REAL_YTDLP, SANDBOX, sites
from xdownloader import fsutil

HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
NOT_ROOT = unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")


class TestFolderProblem(Base):
    def test_a_normal_folder_is_fine_and_nothing_is_left_behind(self):
        self.assertIsNone(fsutil.folder_problem(self.base))
        self.assertEqual(os.listdir(self.base), [])

    @NOT_ROOT
    def test_a_folder_that_cannot_be_written_is_reported(self):
        for path in ("/usr/bin", "/proc", "/"):                     # not writable for a normal user
            with self.subTest(path=path):
                code, detail = fsutil.folder_problem(path)
                self.assertEqual(code, "write")
                self.assertTrue(detail)

    @NOT_ROOT
    def test_a_read_only_folder_is_reported(self):
        folder = os.path.join(self.base, "readonly")
        os.makedirs(folder)
        os.chmod(folder, 0o555)
        self.addCleanup(os.chmod, folder, 0o755)
        self.assertEqual(fsutil.folder_problem(folder)[0], "write")


class TestFolderNamesThatYtDlpWouldChange(Base):
    """yt-dlp expands $VAR / ${VAR} in -P and strips surrounding spaces, so the file lands in a different folder
    than the one shown on the settings page (measured with the real binary)."""

    def test_variables_spaces_and_bad_encodings_are_reported(self):
        with mock.patch.dict(os.environ, {"XDL_TEST_VAR": "expanded"}):
            for path in (os.path.join(self.base, "$XDL_TEST_VAR"), os.path.join(self.base, "${XDL_TEST_VAR}-videos"),
                         os.path.join(self.base, "trailing space "), os.path.join(self.base, os.fsdecode(b"bad-\xa4\xa4"))):
                with self.subTest(path=path):
                    self.assertEqual(fsutil.folder_problem(path)[0], "name")

    def test_a_variable_that_does_not_exist_is_left_alone_by_yt_dlp_and_by_us(self):
        path = os.path.join(self.base, "$XDL_NO_SUCH_VARIABLE")
        os.makedirs(path)
        self.assertIsNone(fsutil.folder_problem(path))

    def test_ordinary_unusual_names_are_fine(self):
        for name in ("with space", "中文 資料夾", "100%(id)s", "back\\slash", "it's \"quoted\"", "emoji-😀", "trailing-dot."):
            with self.subTest(name=name):
                path = os.path.join(self.base, name)
                os.makedirs(path)
                self.assertIsNone(fsutil.folder_problem(path))

    def test_typed_variables_are_expanded_like_a_shell_does(self):
        with mock.patch.dict(os.environ, {"XDL_TEST_VAR": self.base}):
            self.assertEqual(Config.resolve_path("$XDL_TEST_VAR/videos"), os.path.join(self.base, "videos"))
            self.assertEqual(Config.resolve_path("${XDL_TEST_VAR}/v"), os.path.join(self.base, "v"))

    @unittest.skipUnless(os.path.exists(REAL_YTDLP), "real yt-dlp not present")
    def test_the_real_yt_dlp_really_does_this(self):
        info = os.path.join(self.base, "info.json")
        with open(info, "w") as f:
            json.dump({"id": "1", "title": "t", "ext": "mp4", "url": "http://127.0.0.1:9/x.mp4",
                       "protocol": "http", "_type": "video"}, f)
        wanted = os.path.join(self.base, "$XDL_TEST_VAR", "videos")
        env = dict(os.environ, XDL_TEST_VAR="expanded")
        out = subprocess.run([REAL_YTDLP, "--ignore-config", "--load-info-json", info, "--print", "filename",
                              "-P", wanted, "-o", "%(id)s.%(ext)s"],
                             capture_output=True, text=True, env=env).stdout.strip()
        self.assertEqual(out, os.path.join(self.base, "expanded", "videos", "1.mp4"))     # not the folder we passed
        with mock.patch.dict(os.environ, {"XDL_TEST_VAR": "expanded"}):
            self.assertEqual(fsutil.folder_problem(wanted)[0], "name")                      # ... so it is refused


@NOT_ROOT
class TestUnusableFolderStopsTheBatch(Base):
    """A batch into a folder that cannot be written used to fail every URL after all retries."""

    def setUp(self):
        super().setUp()
        self.install_fake_tools()
        Config.RETRY_DELAY = 0
        self.folder = os.path.join(self.base, "readonly")
        os.makedirs(self.folder)
        os.chmod(self.folder, 0o555)
        self.addCleanup(os.chmod, self.folder, 0o755)
        Config.DOWNLOAD_DIR = self.folder

    def run_batch(self, urls):
        pending = []
        thread = DownloadThread(urls, pending.extend)
        thread.start()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        return pending

    def test_nothing_is_attempted_and_the_urls_come_back(self):
        urls = [f"https://x.com/u/status/{i}?ok" for i in range(1, 6)]
        pending = self.run_batch(urls)
        self.assertEqual(self.calls(), [])                          # yt-dlp was never started
        self.assertEqual(pending, urls)                             # ... and the queue is handed back
        self.assertFalse(os.path.exists(Config.FAILED_FILE))        # not recorded as failures

    def test_the_log_says_why(self):
        with self.assertLogs("XDownloader", "ERROR") as logs:
            self.run_batch(["https://x.com/u/status/1?ok"])
        self.assertTrue(any("Cannot use the download folder" in line for line in logs.output), logs.output)


class TestPermissionErrorsAreNotRetried(Base):
    """Retrying cannot make a folder writable (the real yt-dlp messages were captured in an experiment)."""
    MESSAGES = (
        "unable to open for writing: [Errno 13] Permission denied: '/usr/bin/x.mp4.part'",
        "unable to open for writing: [Errno 30] Read-only file system: '/mnt/x.mp4.part'",
    )

    def test_are_not_retried(self):
        self.install_fake_tools()
        Config.RETRY_DELAY = 0
        mgr = BrowserCookieManager(self.log, sites.X)
        mgr._cookie_args = []
        core = DownloaderCore(self.log, {sites.X.name: mgr})
        for message in self.MESSAGES:
            with self.subTest(message=message):
                with open(Config.YT_DLP_EXE, "w") as f:
                    f.write(f"#!/usr/bin/env bash\necho \"$@\" >> \"$(dirname \"$0\")/calls.log\"\n"
                            f"echo \"ERROR: {message}\" >&2\nexit 1\n# pad {'x' * 2048}\n")
                before = len(self.calls())
                self.assertFalse(core.run("https://x.com/u/status/1"))
                self.assertEqual(len(self.calls()) - before, 1)


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class TestSettingsPageRejectsUnusableFolders(Base):
    def setUp(self):
        super().setUp()
        from xdownloader import gui
        self.gui = gui
        self.install_fake_tools()
        self.app = gui.App()
        self.addCleanup(self.close_app)
        end = time.time() + 10
        while self.app._updating and time.time() < end:
            self.app.update()
            time.sleep(0.02)
        self.app._on_settings()
        self.dialog = self.app._settings_window
        self.shown = []
        shown = self.shown

        class RecordingBox:
            def __init__(self, parent, title, message, mode="info"):
                shown.append(message)

            def show(self):
                return True
        patcher = mock.patch.object(gui, "CustomMessageBox", RecordingBox)
        patcher.start()
        self.addCleanup(patcher.stop)

    def close_app(self):
        try:
            self.app._on_closing()
        except Exception:
            pass

    @NOT_ROOT
    def test_a_folder_that_cannot_be_written_is_refused_not_saved(self):
        for path in ("/usr/bin", "/etc", "/"):
            with self.subTest(path=path):
                self.shown.clear()
                before = Config.DOWNLOAD_DIR
                self.dialog._apply_dir(path)
                self.assertEqual(Config.DOWNLOAD_DIR, before)
                self.assertEqual(len(self.shown), 1)
                self.assertEqual(self.dialog._dir_entry.get(), before)     # the entry shows the folder in use

    def test_a_usable_folder_is_still_accepted(self):
        target = os.path.join(SANDBOX, "usable_folder")
        os.makedirs(target, exist_ok=True)
        self.dialog._apply_dir(target)
        self.assertEqual(Config.DOWNLOAD_DIR, target)
        self.assertEqual(self.shown, [])

    def test_a_typed_variable_means_what_it_means_in_a_shell(self):
        # "$HOME/Videos" used to create a folder literally named $HOME inside the project, and yt-dlp then
        # expanded the $HOME anyway, writing somewhere else.
        with mock.patch.dict(os.environ, {"XDL_TEST_VAR": self.base}):
            self.dialog._apply_dir("$XDL_TEST_VAR/typed videos")
        self.assertEqual(Config.DOWNLOAD_DIR, os.path.join(self.base, "typed videos"))
        self.assertTrue(os.path.isdir(Config.DOWNLOAD_DIR))
        self.assertEqual(self.shown, [])
        self.assertFalse(os.path.exists(os.path.join(Config.BASE_PATH, "$XDL_TEST_VAR")))

    def test_a_folder_name_that_cannot_be_stored_is_explained_not_an_exception(self):
        # A folder with Big5 bytes in its name (Windows archives) used to raise UnicodeEncodeError out of the
        # dialog, leaving the app using the folder while settings.json still held the old one.
        bad = os.path.join(os.fsencode(self.base), b"mojibake-\xa4\xa4\xa4\xe5")
        os.mkdir(bad)
        before = Config.DOWNLOAD_DIR
        self.dialog._apply_dir(os.fsdecode(bad))
        self.assertEqual(Config.DOWNLOAD_DIR, before)
        self.assertEqual(len(self.shown), 1)
        self.assertIn("mojibake-", self.shown[0])
        self.assertNotIn("\udca4", self.shown[0])                     # shown with replacement characters

    def test_browsing_to_a_folder_with_a_bad_name_explains_it(self):
        # The chooser's output used to be decoded strictly: the worker thread died and "Browse" silently did nothing.
        bad = os.path.join(os.fsencode(self.base), b"browse-\xa4\xa4\xa4\xe5")
        os.mkdir(bad)
        bindir = os.path.join(self.base, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "zenity"), "wb") as f:
            f.write(b"#!/bin/sh\nprintf '%s\\n' '" + bad + b"'\n")
        os.chmod(os.path.join(bindir, "zenity"), 0o755)
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
            before = Config.DOWNLOAD_DIR
            self.dialog._on_browse()
        self.assertEqual(Config.DOWNLOAD_DIR, before)
        self.assertEqual(len(self.shown), 1)                  # the user is told, not left wondering
        self.assertIn("browse-", self.shown[0])

    def test_losing_focus_does_not_create_a_folder(self):
        # Half-typed paths (or an Alt+Tab) used to create an empty folder and save it as the download folder.
        from xdownloader.i18n import t
        before = Config.DOWNLOAD_DIR
        target = os.path.join(self.base, "half typed, then Alt+Tab")
        self.dialog._dir_entry.delete(0, "end")
        self.dialog._dir_entry.insert(0, target)
        self.dialog._on_focus_out()
        self.assertFalse(os.path.exists(target))
        self.assertEqual(Config.DOWNLOAD_DIR, before)
        self.assertEqual(self.dialog._dir_entry.get(), target)                 # what was typed is kept
        self.assertEqual(self.dialog._status_lbl.cget("text"), t("status_folder_pending"))
        self.assertEqual(self.shown, [])
        # Enter is the explicit "create it":
        self.dialog._on_save()
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(Config.DOWNLOAD_DIR, target)

    def test_closing_the_page_creates_the_typed_folder(self):
        target = os.path.join(self.base, "typed then closed")
        self.dialog._dir_entry.delete(0, "end")
        self.dialog._dir_entry.insert(0, target)
        self.dialog._on_close()
        self.assertEqual(Config.DOWNLOAD_DIR, target)
        self.assertTrue(os.path.isdir(target))

    def test_losing_focus_still_applies_an_existing_folder(self):
        target = os.path.join(self.base, "already there")
        os.makedirs(target)
        self.dialog._dir_entry.delete(0, "end")
        self.dialog._dir_entry.insert(0, target)
        self.dialog._on_focus_out()
        self.assertEqual(Config.DOWNLOAD_DIR, target)

    @NOT_ROOT
    def test_a_refused_folder_does_not_let_the_download_start_in_the_old_one(self):
        # The folder typed in Settings was refused with a message, and the batch then started anyway, into the folder
        # the user had just tried to replace.
        before = Config.DOWNLOAD_DIR
        self.dialog._dir_entry.delete(0, "end")
        self.dialog._dir_entry.insert(0, "/usr/bin")
        self.app._listbox.insert("end", "https://x.com/ok/status/1\n")
        calls_before = len(self.calls())
        self.app._on_start_download()
        started = self.app._is_downloading
        end = time.time() + 15
        while self.app._is_downloading and time.time() < end:        # a batch started by mistake runs to its end:
            self.app.update()                                         # closing during one asks a question, and the
            time.sleep(0.02)                                          # test would hang there instead of failing
        self.assertFalse(started)
        self.assertEqual(len(self.shown), 1)                          # the reason, once
        self.assertEqual(Config.DOWNLOAD_DIR, before)
        self.assertEqual(len(self.calls()), calls_before)             # yt-dlp never started

    @NOT_ROOT
    def test_a_refused_folder_is_not_replaced_by_the_old_one_in_the_file_manager(self):
        self.dialog._dir_entry.delete(0, "end")
        self.dialog._dir_entry.insert(0, "/usr/bin")
        with mock.patch.object(self.gui.subprocess, "Popen") as popen:
            self.dialog._on_open_folder()
        self.assertEqual(len(self.shown), 1)
        popen.assert_not_called()

    def test_spaces_around_a_typed_path_are_trimmed(self):
        # The usual copy & paste accident. (A path that arrives untrimmed, e.g. from a hand-edited
        # settings.json, is refused by folder_problem(): yt-dlp would strip it and write elsewhere.)
        target = os.path.join(self.base, "pasted with spaces")
        self.dialog._apply_dir(f"   {target}   ")
        self.assertEqual(Config.DOWNLOAD_DIR, target)
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(self.shown, [])


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class TestFolderChooserDoesNotFreeze(Base):
    """The native folder chooser (zenity) blocked the Tk event loop for as long as it stayed open."""

    def setUp(self):
        super().setUp()
        import tkinter as tk
        from xdownloader import gui
        self.gui = gui
        self.root = tk.Tk()

        def destroy_root():
            try:
                self.root.destroy()
            except tk.TclError:
                pass
        self.addCleanup(destroy_root)
        bindir = os.path.join(self.base, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "zenity"), "w") as f:
            f.write("#!/bin/sh\nsleep 1\necho /chosen/dir\n")
        os.chmod(os.path.join(bindir, "zenity"), 0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_event_loop_keeps_running(self):
        ticks = []

        def tick():
            ticks.append(1)
            self.root.after(50, tick)
        self.root.after(50, tick)
        self.assertEqual(self.gui._ask_directory(self.root, self.base, "x"), "/chosen/dir")
        self.assertGreater(len(ticks), 5)        # about 20 during the 1 s dialog; it used to be 0

    def test_closing_the_app_meanwhile_does_not_hang(self):
        self.root.after(200, self.root.destroy)
        started = time.time()
        self.assertEqual(self.gui._ask_directory(self.root, self.base, "x"), "")
        self.assertLess(time.time() - started, 5)


class TestTemporarilyMissingDownloadFolder(Base):
    """A saved download folder that was missing at startup (USB drive not plugged in) was silently replaced,
    and saving any other setting afterwards erased the user's choice from settings.json for good."""
    USB = "/media/nobody/USB-DRIVE/Videos"            # never exists

    def write_settings(self, download_dir):
        with open(Config.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"download_dir": download_dir}, f)

    def saved_dir(self):
        with open(Config.SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)["download_dir"]

    def test_saving_other_settings_keeps_it(self):
        self.write_settings(self.USB)
        Config.load_settings()
        self.assertEqual(Config.DOWNLOAD_DIR, self.base)      # downloads still go to a folder that exists
        Config.LANGUAGE = "zh_TW"
        self.assertTrue(Config.save_settings())
        self.assertEqual(self.saved_dir(), self.USB)

    def test_it_is_used_again_once_it_is_back(self):
        usb = os.path.join(self.base, "usb")
        self.write_settings(usb)
        Config.load_settings()
        self.assertEqual(Config.DOWNLOAD_DIR, self.base)
        os.makedirs(usb)                                      # the drive is plugged in; next start:
        Config.load_settings()
        self.assertEqual(Config.DOWNLOAD_DIR, usb)
        self.assertIsNone(Config.MISSING_DOWNLOAD_DIR)

    def test_an_invalid_value_is_not_kept(self):
        self.write_settings("/dev/null")                      # exists, but is not a folder
        Config.load_settings()
        Config.save_settings()
        self.assertEqual(self.saved_dir(), "")                # the project folder, the default

    @unittest.skipUnless(HAS_DISPLAY, "needs a display")
    def test_the_app_warns_and_an_explicit_choice_replaces_it(self):
        from xdownloader import gui
        self.write_settings(self.USB)
        self.install_fake_tools()
        with self.assertLogs("XDownloader", "WARNING") as logs:
            app = gui.App()

        def close():
            try:
                app._on_closing()
            except Exception:
                pass
        self.addCleanup(close)
        self.assertTrue(any("Saved download folder not found" in line for line in logs.output))
        app._on_settings()
        chosen = tempfile.mkdtemp(prefix="chosen_", dir=SANDBOX)
        app._settings_window._apply_dir(chosen)
        self.assertEqual(self.saved_dir(), chosen)


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class TestChosenFolderIsTakenExactly(Base):
    """Browse hands over a folder that really exists, but it was trimmed like typed text: a folder whose name ends with a
    space turned into its look-alike without the space (made up on the spot and used), so the refusal that the README
    promises for such a name could never fire. A chosen folder is now taken as it is, and refused when yt-dlp would
    write somewhere else."""

    def setUp(self):
        super().setUp()
        from xdownloader import gui
        self.gui = gui
        self.install_fake_tools()
        self.app = gui.App()
        self.addCleanup(self.close_app)
        self.app._on_settings()
        self.dialog = self.app._settings_window

    def close_app(self):
        try:
            self.app._on_closing()
        except Exception:
            pass

    def browse(self, folder):
        with mock.patch.object(self.gui, "_ask_directory", return_value=folder), \
                mock.patch.object(self.gui, "CustomMessageBox") as box:
            self.dialog._on_browse()
        return box

    def test_a_folder_whose_name_ends_with_a_space_is_refused_not_replaced(self):
        from xdownloader.i18n import t
        chosen = os.path.join(self.base, "videos ")
        os.mkdir(chosen)
        before = Config.DOWNLOAD_DIR
        box = self.browse(chosen)
        self.assertEqual(Config.DOWNLOAD_DIR, before)
        self.assertFalse(os.path.exists(os.path.join(self.base, "videos")))       # no look-alike was made up
        self.assertEqual(box.call_args.args[2], t("msg_dir_bad_name", path=chosen))

    def test_an_ordinary_chosen_folder_is_still_used(self):
        chosen = os.path.join(self.base, "with a space inside")
        os.mkdir(chosen)
        box = self.browse(chosen)
        self.assertEqual(Config.DOWNLOAD_DIR, chosen)
        box.assert_not_called()


if __name__ == "__main__":
    unittest.main()
