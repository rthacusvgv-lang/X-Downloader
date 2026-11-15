"""
The settings: the Settings window (SettingsDialog) and Config persistence in settings.json.
The first two classes run every feature repeatedly across edge cases, rapid switches and corrupted inputs; the
others cover a save that fails and the temp files a killed save leaves behind.
"""
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from unittest import mock

from common import (
    Base, Config, PROJECT, ProcessManager, SANDBOX, install_fake_tools, point_config_to, read_calls, reset_shutdown,
)

HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
if HAS_DISPLAY:
    from xdownloader import gui


def new_base():
    base = tempfile.mkdtemp(prefix="settings_test_", dir=SANDBOX)
    point_config_to(base)
    install_fake_tools()
    Config.RETRY_DELAY = 0
    lg = logging.getLogger("XDownloader")
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()
    reset_shutdown()
    return base


def pump(app, cond=lambda: False, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class TestSettingsBackendReliability(unittest.TestCase):
    """Stress tests on Config.load_settings and Config.save_settings without GUI."""

    def setUp(self):
        self.base = new_base()

    def tearDown(self):
        reset_shutdown()
        if os.path.exists(self.base):
            shutil.rmtree(self.base, ignore_errors=True)

    def test_rapid_save_and_load_cycles(self):
        """Repeatedly save and reload permutations of settings."""
        retries_options = [1, 2, 3, 5]
        format_options = list(Config.SUPPORTED_FORMATS)
        lang_options = ["en", "zh_TW"]

        iteration = 0
        for r in retries_options:
            for fmt in format_options:
                for lang in lang_options:
                    iteration += 1
                    target_dir = os.path.join(self.base, f"dir_{iteration}")
                    os.makedirs(target_dir, exist_ok=True)

                    Config.DOWNLOAD_DIR = target_dir
                    Config.MAX_RETRIES = r
                    Config.MERGE_FORMAT = fmt
                    Config.LANGUAGE = lang
                    Config.save_settings()

                    # Tamper in-memory values
                    Config.DOWNLOAD_DIR = self.base
                    Config.MAX_RETRIES = 99
                    Config.MERGE_FORMAT = "invalid"
                    Config.LANGUAGE = "unknown"

                    Config.load_settings()
                    self.assertEqual(Config.DOWNLOAD_DIR, os.path.abspath(target_dir))
                    self.assertEqual(Config.MAX_RETRIES, r)
                    self.assertEqual(Config.MERGE_FORMAT, fmt)
                    self.assertEqual(Config.LANGUAGE, lang)

    def test_corruption_and_edge_case_inputs(self):
        """Verify load_settings never crashes on any malformed or boundary inputs."""
        bad_payloads = [
            "",                                  # empty file
            "{",                                 # broken JSON
            "null",                              # null
            "[]",                                # array instead of dict
            "12345",                             # integer
            json.dumps({"download_dir": None, "max_retries": None, "merge_format": None, "language": None}),
            json.dumps({"download_dir": 12345, "max_retries": "3", "merge_format": 456, "language": ["en"]}),
            json.dumps({"max_retries": True}),   # bool is an instance of int in Python
            json.dumps({"max_retries": False}),
            json.dumps({"max_retries": -5}),
            json.dumps({"max_retries": 100}),
            json.dumps({"merge_format": "avi"}),
            json.dumps({"merge_format": "webm"}),
            json.dumps({"language": "fr_FR"}),
            json.dumps({"download_dir": "/non/existent/path/xyz"}),
            json.dumps({"download_dir": "   \n\t  "}),
        ]

        valid_dir = os.path.join(self.base, "valid_dir")
        os.makedirs(valid_dir, exist_ok=True)

        for payload in bad_payloads:
            with open(Config.SETTINGS_FILE, "w", encoding="utf-8") as f:
                f.write(payload)

            # Pre-set known good values
            Config.DOWNLOAD_DIR = valid_dir
            Config.MAX_RETRIES = 2
            Config.MERGE_FORMAT = "mp4"
            Config.LANGUAGE = "en"

            # Must not raise an exception, and must preserve or fall back gracefully
            Config.load_settings()
            self.assertEqual(Config.DOWNLOAD_DIR, valid_dir)
            self.assertIn(Config.MAX_RETRIES, range(1, 11))
            self.assertIn(Config.MERGE_FORMAT, Config.SUPPORTED_FORMATS)
            self.assertIn(Config.LANGUAGE, ("en", "zh_TW"))


@unittest.skipUnless(HAS_DISPLAY, "no display available")
class TestSettingsGuiComprehensive(unittest.TestCase):
    """Exhaustive GUI validation: tab navigation, language, directory inputs, and dialogs."""

    def setUp(self):
        self.base = new_base()
        self.app = None

    def tearDown(self):
        if self.app is not None:
            try:
                if not self.app._closing:
                    ProcessManager.kill_all()
                    self.app._lock.release()
                    self.app._shutdown_ui()
            except Exception:
                pass
            self.app = None
        reset_shutdown()
        if os.path.exists(self.base):
            shutil.rmtree(self.base, ignore_errors=True)

    def test_repeated_tab_navigation(self):
        """Repeatedly cycle between General, Advanced, About tabs 30 times."""
        self.app = app = gui.App()
        pump(app, lambda: app._start_btn.cget("state") == "normal", 10)

        app._on_settings()
        pump(app, timeout=0.2)
        dlg = app._settings_window
        self.assertIsNotNone(dlg)

        tabs = ["General", "Advanced", "About"]
        for cycle in range(10):
            for t_name in tabs:
                dlg._show_tab(t_name)
                pump(app, timeout=0.02)
                self.assertEqual(dlg._active_tab, t_name)
                # Verify active button styling
                for btn_key, btn in dlg._nav_buttons.items():
                    if btn_key == t_name:
                        self.assertEqual(btn.cget("fg_color"), gui.Theme.BOX_BG)
                    else:
                        self.assertEqual(btn.cget("fg_color"), "transparent")
        dlg.destroy()

    def test_directory_operations_repeated(self):
        """Test directory manual entry, Enter/FocusOut, Reset, and pending commit on close."""
        self.app = app = gui.App()
        pump(app, lambda: app._start_btn.cget("state") == "normal", 10)

        app._on_settings()
        pump(app, timeout=0.2)
        dlg = app._settings_window

        # 1. Multiple valid custom directory paths entered manually
        for i in range(5):
            custom_path = os.path.join(self.base, f"custom_folder_{i}")
            dlg._dir_entry.delete(0, "end")
            dlg._dir_entry.insert(0, custom_path)
            dlg._on_save()
            pump(app, timeout=0.05)
            self.assertEqual(Config.DOWNLOAD_DIR, os.path.abspath(custom_path))
            self.assertTrue(os.path.isdir(custom_path))
            with open(Config.SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["download_dir"], os.path.abspath(custom_path))

        # 2. Reset button restores Config.BASE_PATH
        dlg._on_reset()
        pump(app, timeout=0.05)
        self.assertEqual(Config.DOWNLOAD_DIR, os.path.abspath(Config.BASE_PATH))
        self.assertEqual(dlg._dir_entry.get(), os.path.abspath(Config.BASE_PATH))

        # 3. Pending uncommitted text in entry is saved when closing window
        pending_path = os.path.join(self.base, "pending_saved_on_close")
        dlg._dir_entry.delete(0, "end")
        dlg._dir_entry.insert(0, pending_path)
        # Note: we do NOT call _on_save(), directly trigger _on_close()
        dlg._on_close()
        pump(app, timeout=0.1)
        self.assertEqual(Config.DOWNLOAD_DIR, os.path.abspath(pending_path))
        self.assertTrue(os.path.isdir(pending_path))

        # 4. Busy guard: cannot change download dir while downloading
        app._on_settings()
        pump(app, timeout=0.2)
        dlg2 = app._settings_window
        app._is_downloading = True

        blocked_path = os.path.join(self.base, "should_be_blocked")
        with mock.patch("xdownloader.gui.CustomMessageBox.show", autospec=True, side_effect=lambda s: (s.destroy(), True)[1]) as m_box:
            dlg2._apply_dir(blocked_path)
            self.assertTrue(m_box.called)
            self.assertEqual(Config.DOWNLOAD_DIR, os.path.abspath(pending_path))

        app._is_downloading = False
        dlg2.destroy()

    def test_language_repeated_toggle_and_live_update(self):
        """Repeatedly toggle language 20 times and verify all widgets update immediately."""
        self.app = app = gui.App()
        pump(app, lambda: app._start_btn.cget("state") == "normal", 10)

        app._on_settings()
        pump(app, timeout=0.2)
        dlg = app._settings_window

        languages = ["繁體中文", "English"]
        for _ in range(10):
            for opt_text in languages:
                dlg._on_language_change(opt_text)
                pump(app, timeout=0.05)

                expected_code = "zh_TW" if opt_text == "繁體中文" else "en"
                self.assertEqual(Config.LANGUAGE, expected_code)

                # Verify window and button labels in both windows
                if expected_code == "zh_TW":
                    self.assertEqual(dlg.title(), "設定")
                    self.assertEqual(dlg._sidebar_header_lbl.cget("text"), "設定")
                    self.assertEqual(dlg._nav_buttons["General"].cget("text"), "一般")
                    self.assertEqual(app._start_btn.cget("text"), "開始下載")
                    self.assertEqual(app._clear_btn.cget("text"), "清空清單")
                else:
                    self.assertEqual(dlg.title(), "Settings")
                    self.assertEqual(dlg._sidebar_header_lbl.cget("text"), "SETTINGS")
                    self.assertEqual(dlg._nav_buttons["General"].cget("text"), "General")
                    self.assertEqual(app._start_btn.cget("text"), "Download")
                    self.assertEqual(app._clear_btn.cget("text"), "Clear List")

        dlg.destroy()

    def test_retries_and_format_rapid_changes(self):
        """Test repeated option menu changes for max retries and container format."""
        self.app = app = gui.App()
        pump(app, lambda: app._start_btn.cget("state") == "normal", 10)

        app._on_settings()
        pump(app, timeout=0.2)
        dlg = app._settings_window
        dlg._show_tab("Advanced")
        pump(app, timeout=0.1)

        # Verify in-memory config and settings.json persist correctly under repeated retry count changes
        for val in ["1", "2", "3", "5", "2", "1", "5"]:
            dlg._on_retry_change(val)
            self.assertEqual(Config.MAX_RETRIES, int(val))
            with open(Config.SETTINGS_FILE, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["max_retries"], int(val))

        # Verify format setting and persistence under repeated container format changes
        self.assertEqual(list(dlg._fmt_opt.cget("values")), list(Config.SUPPORTED_FORMATS))
        for fmt in ["mp4", "mkv", "mp4", "mkv"]:
            dlg._on_format_change(fmt)
            self.assertEqual(Config.MERGE_FORMAT, fmt)
            with open(Config.SETTINGS_FILE, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["merge_format"], fmt)

        # Busy protection during download (also restores option menu displayed values)
        app._is_downloading = True
        with mock.patch("xdownloader.gui.CustomMessageBox.show", autospec=True, side_effect=lambda s: (s.destroy(), True)[1]) as m_box:
            dlg._retry_opt.set("3")
            dlg._on_retry_change("3")
            self.assertTrue(m_box.called)
            self.assertEqual(Config.MAX_RETRIES, 5)
            self.assertEqual(dlg._retry_opt.get(), "5")

            dlg._fmt_opt.set("mp4")
            dlg._on_format_change("mp4")
            self.assertTrue(m_box.called)
            self.assertEqual(Config.MERGE_FORMAT, "mkv")
            self.assertEqual(dlg._fmt_opt.get(), "mkv")

        # Also check General tab _dir_entry restoration on busy rejection and no duplicate popup on close
        dlg._show_tab("General")
        pump(app, timeout=0.05)
        with mock.patch("xdownloader.gui.CustomMessageBox.show", autospec=True, side_effect=lambda s: (s.destroy(), True)[1]) as m_box2:
            dlg._dir_entry.delete(0, "end")
            dlg._dir_entry.insert(0, os.path.join(self.base, "dirty_while_busy"))
            dlg._on_save()
            self.assertEqual(m_box2.call_count, 1)
            self.assertEqual(dlg._dir_entry.get(), Config.DOWNLOAD_DIR)
            # Closing while downloading must not trigger another popup
            dlg._on_close()
            self.assertEqual(m_box2.call_count, 1)

        app._is_downloading = False

    def test_directory_chooser_dialog_robustness(self):
        """Test DirectoryChooserDialog directory navigation, permissions, selections, and zenity crash fallback."""
        self.app = app = gui.App()
        pump(app, lambda: app._start_btn.cget("state") == "normal", 10)

        folder_tree = os.path.join(self.base, "level1", "level2", "level3")
        os.makedirs(folder_tree, exist_ok=True)

        # If zenity crashes with exit code 255, _ask_directory falls back to DirectoryChooserDialog
        with mock.patch("xdownloader.gui.shutil.which", side_effect=lambda cmd: "/usr/bin/zenity" if cmd == "zenity" else None), \
             mock.patch("xdownloader.gui.subprocess.run", return_value=subprocess.CompletedProcess(args=[], returncode=255, stdout="")), \
             mock.patch.object(gui.DirectoryChooserDialog, "show", return_value=folder_tree) as m_fallback:
            chosen = gui._ask_directory(app, self.base, "Select")
            self.assertTrue(m_fallback.called)
            self.assertEqual(chosen, folder_tree)

        cdlg = gui.DirectoryChooserDialog(app, initialdir=self.base)
        pump(app, timeout=0.2)

        # Verify descending into subdirectories
        cdlg._navigate_to(os.path.join(self.base, "level1"))
        self.assertEqual(cdlg._current_path, os.path.join(self.base, "level1"))

        cdlg._navigate_to(os.path.join(self.base, "level1", "level2"))
        self.assertEqual(cdlg._current_path, os.path.join(self.base, "level1", "level2"))

        # Verify navigating back up to parent directory
        cdlg._on_up()
        self.assertEqual(cdlg._current_path, os.path.join(self.base, "level1"))

        # Verify manual path entry updates current directory
        cdlg._path_entry.delete(0, "end")
        cdlg._path_entry.insert(0, folder_tree)
        cdlg._on_path_entered()
        self.assertEqual(cdlg._current_path, os.path.abspath(folder_tree))

        # Entering an invalid path in entry must cleanly revert to the last valid path
        cdlg._path_entry.delete(0, "end")
        cdlg._path_entry.insert(0, "/invalid/nonexistent/xyz/path")
        cdlg._on_path_entered()
        self.assertEqual(cdlg._current_path, os.path.abspath(folder_tree))
        self.assertEqual(cdlg._path_entry.get(), os.path.abspath(folder_tree))

        # Verify dialog result is confirmed upon selection
        cdlg._on_select()
        self.assertEqual(cdlg.result, os.path.abspath(folder_tree))
        cdlg.destroy()

    def test_storage_buttons_focus_out_and_error_recovery(self):
        """Test Open button, relative paths, FocusOut no-op vs save, and error recovery without duplicate popups."""
        self.app = app = gui.App()
        pump(app, lambda: app._start_btn.cget("state") == "normal", 10)

        app._on_settings()
        pump(app, timeout=0.2)
        dlg = app._settings_window

        # 1. Relative path resolves inside Config.BASE_PATH (never leaks into process cwd).
        #    Losing focus alone must not create the folder (see tests/test_download_folder.py); Enter does.
        dlg._dir_entry.delete(0, "end")
        dlg._dir_entry.insert(0, "rel_subfolder/videos")
        dlg._on_focus_out()
        expected_rel = os.path.join(self.base, "rel_subfolder", "videos")
        self.assertFalse(os.path.exists(expected_rel))
        dlg._on_save()
        self.assertEqual(Config.DOWNLOAD_DIR, expected_rel)
        self.assertTrue(os.path.isdir(expected_rel))
        self.assertEqual(dlg._dir_entry.get(), expected_rel)

        # 2. FocusOut when path is unchanged (or only padded with spaces) does not spam or error
        dlg._dir_entry.delete(0, "end")
        dlg._dir_entry.insert(0, f"   {expected_rel}   ")
        app._is_downloading = True
        with mock.patch("xdownloader.gui.CustomMessageBox.show", autospec=True) as m_busy:
            dlg._on_focus_out()
            self.assertFalse(m_busy.called)
            self.assertEqual(dlg._dir_entry.get(), expected_rel)
        app._is_downloading = False

        # 3. Uncreatable path (e.g., file exists at path) shows error once and reverts entry to valid DOWNLOAD_DIR
        blocker_file = os.path.join(self.base, "blocker_file")
        with open(blocker_file, "w", encoding="utf-8") as f:
            f.write("not a dir")
        impossible_dir = os.path.join(blocker_file, "sub")

        with mock.patch("xdownloader.gui.CustomMessageBox.show", autospec=True, side_effect=lambda s: (s.destroy(), True)[1]) as m_err:
            dlg._dir_entry.delete(0, "end")
            dlg._dir_entry.insert(0, impossible_dir)
            dlg._on_save()
            self.assertEqual(m_err.call_count, 1)
            self.assertEqual(Config.DOWNLOAD_DIR, expected_rel)
            self.assertEqual(dlg._dir_entry.get(), expected_rel)
            # Subsequent FocusOut or tab switch must NOT re-trigger the error dialog
            dlg._on_focus_out()
            dlg._show_tab("Advanced")
            dlg._show_tab("General")
            self.assertEqual(m_err.call_count, 1)

        # 4. Open folder button commits pending directory and invokes xdg-open
        open_target = os.path.join(self.base, "open_btn_dir")
        dlg._dir_entry.delete(0, "end")
        dlg._dir_entry.insert(0, open_target)
        with mock.patch("xdownloader.gui.subprocess.Popen") as m_popen:
            dlg._on_open_folder()
            self.assertEqual(Config.DOWNLOAD_DIR, open_target)
            self.assertTrue(os.path.isdir(open_target))
            self.assertTrue(m_popen.called)
            self.assertEqual(m_popen.call_args[0][0], ["xdg-open", open_target])

        # 5. Browse button while downloading is rejected immediately without opening chooser
        app._is_downloading = True
        with mock.patch("xdownloader.gui.CustomMessageBox.show", autospec=True, side_effect=lambda s: (s.destroy(), True)[1]) as m_busy2, \
             mock.patch("xdownloader.gui._ask_directory") as m_ask:
            dlg._on_browse()
            self.assertEqual(m_busy2.call_count, 1)
            self.assertFalse(m_ask.called)
        app._is_downloading = False
        dlg.destroy()

    def test_all_options_end_to_end_with_downloads(self):
        """End-to-end test: verify every Max Retries (1, 2, 3, 5), Format (mp4, mkv), and Save Location in real download runs."""
        self.app = app = gui.App()
        pump(app, lambda: app._start_btn.cget("state") == "normal", 10)

        app._on_settings()
        pump(app, timeout=0.2)
        dlg = app._settings_window

        # Test each retry count (1, 2, 3, 5) with a flaky URL -> verify exact number of yt-dlp attempts
        dlg._show_tab("Advanced")
        pump(app, timeout=0.1)
        for retry_str in ["1", "2", "3", "5"]:
            dlg._on_retry_change(retry_str)
            before_calls = len(read_calls(self.base))
            app._on_clear_list()
            app._listbox.insert("end", f"https://x.com/flaky/status/{retry_str}00\n")
            app._refresh_count()
            app._on_start_download()
            self.assertTrue(pump(app, lambda: not app._is_downloading, 15))
            after_calls = read_calls(self.base)[before_calls:]
            self.assertEqual(len(after_calls), int(retry_str), f"Expected {retry_str} attempts, got {len(after_calls)}")

        # Test each container format (mp4, mkv) + uncommitted pending dir in Settings committed on Download start
        for fmt in Config.SUPPORTED_FORMATS:
            dlg._show_tab("Advanced")
            pump(app, timeout=0.05)
            dlg._on_format_change(fmt)

            dlg._show_tab("General")
            pump(app, timeout=0.05)
            fmt_dir = os.path.join(self.base, f"out_{fmt}")
            dlg._dir_entry.delete(0, "end")
            dlg._dir_entry.insert(0, fmt_dir)
            # Do NOT press Enter in Settings; click Start Download directly on main window
            before_calls = len(read_calls(self.base))
            app._on_clear_list()
            app._listbox.insert("end", f"https://x.com/ok/status/99{fmt}\n")
            app._refresh_count()
            app._on_start_download()
            self.assertTrue(pump(app, lambda: not app._is_downloading, 15))

            self.assertEqual(Config.DOWNLOAD_DIR, fmt_dir)
            new_calls = read_calls(self.base)[before_calls:]
            self.assertEqual(len(new_calls), 1)
            args = new_calls[0].split()
            self.assertEqual(args[args.index("--merge-output-format") + 1], fmt)
            self.assertEqual(args[args.index("--remux-video") + 1], fmt)
            self.assertEqual(args[args.index("-P") + 1], fmt_dir)

        dlg.destroy()


class TestSettingsSaveFailure(Base):
    """settings.json could fail to save silently while the settings dialog still said "✓ Auto-saved"."""

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_save_settings_reports_a_failure(self):
        self.assertTrue(Config.save_settings())
        os.chmod(self.base, 0o555)                  # read-only project folder
        self.addCleanup(os.chmod, self.base, 0o755)
        self.assertFalse(Config.save_settings())

    @unittest.skipUnless(HAS_DISPLAY, "needs a display")
    def test_the_settings_dialog_shows_it(self):
        from xdownloader import gui
        from xdownloader.i18n import t
        self.install_fake_tools()
        app = gui.App()

        def close():
            try:
                app._on_closing()
            except Exception:
                pass
        self.addCleanup(close)
        app._on_settings()
        dialog = app._settings_window
        target = tempfile.mkdtemp(prefix="target_", dir=SANDBOX)
        with mock.patch.object(Config, "save_settings", return_value=False), \
                self.assertLogs("XDownloader", "ERROR") as logs:
            dialog._apply_dir(target)
        self.assertEqual(dialog._status_lbl.cget("text"), t("status_save_failed"))
        self.assertIn("Cannot save", logs.output[0])
        self.assertEqual(Config.DOWNLOAD_DIR, target)     # still used until the app is closed


class TestStaleSettingsTempFiles(Base):
    """A save that was killed between writing its temp file and renaming it left settings.json.<hex>.tmp
    behind for good: nothing ever removed it, so one piled up next to the program per killed save."""

    def temp_file(self, name=None, age=3600.0, folder=None):
        path = os.path.join(folder or self.base, name or f"settings.json.{uuid.uuid4().hex}.tmp")
        with open(path, "w") as f:
            f.write("{")
        mtime = time.time() - age
        os.utime(path, (mtime, mtime))
        return path

    def test_a_save_killed_before_the_rename_is_cleaned_up_at_the_next_start(self):
        # The real thing: SIGKILL arrives right before the temp file would replace settings.json.
        child = textwrap.dedent(f"""\
            import os, signal, sys
            sys.path.insert(0, {PROJECT!r})
            from xdownloader import fsutil
            from xdownloader.config import Config
            Config.SETTINGS_FILE = {Config.SETTINGS_FILE!r}
            fsutil.os.replace = lambda *args: os.kill(os.getpid(), signal.SIGKILL)
            Config.save_settings()
        """)
        run = subprocess.run([sys.executable, "-I", "-B", "-c", child], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, -signal.SIGKILL, run.stderr)
        leftovers = [n for n in os.listdir(self.base) if n.endswith(".tmp")]
        self.assertEqual(len(leftovers), 1, leftovers)             # the premise: a killed save does leave one
        self.assertFalse(os.path.exists(Config.SETTINGS_FILE))
        old = time.time() - 3600                                   # ... and the next start comes later
        os.utime(os.path.join(self.base, leftovers[0]), (old, old))
        Config.load_settings()
        self.assertEqual([n for n in os.listdir(self.base) if n.endswith(".tmp")], [])

    def test_settings_still_load_and_the_leftovers_go(self):
        Config.MAX_RETRIES = 5
        self.assertTrue(Config.save_settings())
        stale = [self.temp_file() for _ in range(3)]
        Config.MAX_RETRIES = 1
        Config.load_settings()
        self.assertEqual(Config.MAX_RETRIES, 5)
        self.assertEqual([p for p in stale if os.path.exists(p)], [])
        self.assertTrue(os.path.exists(Config.SETTINGS_FILE))

    def test_a_save_that_is_running_right_now_is_left_alone(self):
        fresh = self.temp_file(age=0)                              # e.g. a second instance starts mid-save
        Config.load_settings()
        self.assertTrue(os.path.exists(fresh))

    def test_only_the_temp_files_of_settings_json_are_removed(self):
        elsewhere = os.path.join(self.base, "precious.txt")
        with open(elsewhere, "w") as f:
            f.write("keep me")
        link = self.temp_file()                                    # replaced by a symlink with the same name
        os.remove(link)
        os.symlink(elsewhere, link)
        folder = os.path.join(self.base, f"settings.json.{uuid.uuid4().hex}.tmp")
        os.mkdir(folder)
        others = [
            self.temp_file("settings.json.bak"),
            self.temp_file("settings.json.tmp"),                              # no id
            self.temp_file(f"settings.json.{'g' * 32}.tmp"),                  # not hex
            self.temp_file(f"settings.json.{uuid.uuid4().hex}.tmp.old"),      # not ours
            self.temp_file(f"xsettings.json.{uuid.uuid4().hex}.tmp"),         # another file
            self.temp_file(f"list.txt.{uuid.uuid4().hex}.tmp"),               # another file's temp file
        ]
        Config.load_settings()
        for path in others + [folder, elsewhere]:
            self.assertTrue(os.path.exists(path), path)
        self.assertTrue(os.path.islink(link))
        with open(elsewhere) as f:
            self.assertEqual(f.read(), "keep me")

    def test_a_symlinked_settings_file_is_handled_where_it_really_lives(self):
        real = os.path.join(self.base, "real")
        os.mkdir(real)
        Config.SETTINGS_FILE = os.path.join(self.base, "settings.json")
        os.symlink(os.path.join(real, "settings.json"), Config.SETTINGS_FILE)
        self.assertTrue(Config.save_settings())                    # temp files are created next to the target
        stale = self.temp_file(folder=real)
        Config.load_settings()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.islink(Config.SETTINGS_FILE))

    def test_a_missing_folder_is_not_an_error(self):
        Config.SETTINGS_FILE = os.path.join(self.base, "no-such-folder", "settings.json")
        Config.load_settings()


if __name__ == "__main__":
    unittest.main(verbosity=2)

