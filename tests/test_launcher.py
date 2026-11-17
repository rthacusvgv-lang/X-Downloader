"""start_gui.sh: starts the program on a machine that has nothing but the basic shell tools.

PATH holds only those tools, so a launcher that reaches for python3, sudo, pip, curl or a package manager fails here,
and the script runs under `sh` (dash on most systems, so bash-only syntax fails too).
"""
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest

from common import PROJECT, SANDBOX

ARCH = platform.machine()
BASIC_TOOLS = ("sh", "tar", "gzip", "uname", "dirname", "mkdir", "rm", "touch", "tail", "sleep", "nohup", "grep")
STAYS_ALIVE = '#!/bin/sh\necho "$(pwd)|$*" >> started.log\nexec sleep 3\n'


class TestLauncher(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="專案 資料夾_", dir=SANDBOX)
        shutil.copy(os.path.join(PROJECT, "start_gui.sh"), self.project)
        os.mkdir(os.path.join(self.project, "bundled-runtime"))
        self.bundle = os.path.join(self.project, "bundled-runtime", f"linux-{ARCH}.tar.gz")
        self.unpacked = os.path.join(self.project, ".runtime", ARCH)
        self.tools = tempfile.mkdtemp(prefix="tools_", dir=SANDBOX)
        for tool in BASIC_TOOLS:
            os.symlink(shutil.which(tool), os.path.join(self.tools, tool))

    def make_bundle(self, python: str = STAYS_ALIVE, mode: int = 0o755):
        """A bundle whose bin/python is the given script."""
        script = os.path.join(self.tools, "python-script")
        with open(script, "w") as f:
            f.write(python)
        os.chmod(script, mode)
        with tarfile.open(self.bundle, "w:gz") as tar:
            tar.add(script, arcname="bin/python")

    def add_tool(self, name: str, script: str):
        path = os.path.join(self.tools, name)
        with open(path, "w") as f:
            f.write(script)
        os.chmod(path, 0o755)

    def launch(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", "start_gui.sh"], cwd=self.project, env={"PATH": self.tools, "DISPLAY": ":0"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )

    def started(self) -> list[str]:
        try:
            with open(os.path.join(self.project, "started.log")) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_first_launch_unpacks_the_runtime_and_starts_the_program(self):
        self.make_bundle()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.started(), [f"{self.project}|-m xdownloader"])
        self.assertTrue(os.path.isfile(os.path.join(self.unpacked, "bin", "python")))

    def test_later_launches_reuse_the_unpacked_runtime(self):
        self.make_bundle()
        self.launch()
        kept = os.path.join(self.unpacked, "kept")
        open(kept, "w").close()
        self.launch()
        self.assertTrue(os.path.exists(kept), "the runtime was unpacked again")
        self.assertEqual(len(self.started()), 2)

    def test_an_updated_bundle_replaces_the_unpacked_runtime(self):
        self.make_bundle()
        self.launch()
        old = os.path.join(self.unpacked, "from-the-old-version")
        open(old, "w").close()
        self.make_bundle()
        later = time.time() + 5        # what `git pull` does to the file: a newer modification time
        os.utime(self.bundle, (later, later))
        self.launch()
        self.assertFalse(os.path.exists(old))

    def test_an_interrupted_unpacking_is_not_trusted(self):
        # Killed while unpacking: bin/python may be there while the libraries are not.
        self.make_bundle()
        os.makedirs(os.path.join(self.unpacked, "bin"))
        with open(os.path.join(self.unpacked, "bin", "python"), "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(self.unpacked, "bin", "python"), 0o755)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.started()), 1)

    def test_a_damaged_bundle_is_reported(self):
        with open(self.bundle, "wb") as f:
            f.write(b"this is not a gzip file")
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("bundled-runtime/linux-", result.stdout)
        self.assertEqual(self.started(), [])

    def test_a_folder_that_cannot_run_programs_is_explained(self):
        # A noexec mount (a USB stick, a shared partition) fails like a file without the execute bit: access(2) says no.
        # What nohup then prints, a bare "Permission denied", does not tell the user what to do.
        self.make_bundle(mode=0o644)
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("noexec", result.stdout)
        self.assertEqual(self.started(), [])

    def test_a_colon_in_the_folder_path_is_explained(self):
        # --library-path is a colon-separated list, so "a:b/X-Downloader" splits in two and the bundled Python cannot
        # find its libraries (measured on Alpine: "libm.so.6: cannot open shared object file").
        self.make_bundle()
        moved = os.path.join(tempfile.mkdtemp(prefix="a:b_", dir=SANDBOX), "X-Downloader")
        shutil.move(self.project, moved)
        self.project = moved
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("冒號", result.stdout)
        self.assertFalse(os.path.exists(os.path.join(moved, ".runtime")), "it failed only after unpacking")
        self.assertEqual(self.started(), [])

    def test_a_program_that_dies_at_startup_shows_why(self):
        self.make_bundle('#!/bin/sh\necho "no display found" >&2\nexit 1\n')
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no display found", result.stdout)

    def test_a_cpu_without_bundle_uses_a_complete_system_python(self):
        self.add_tool("python3", '#!/bin/sh\n[ "$1" = -c ] && exit 0\necho "$(pwd)|$*" >> started.log\nexec sleep 3\n')
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.started(), [f"{self.project}|-m xdownloader"])

    def test_a_cpu_without_bundle_or_python_says_what_is_needed(self):
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn(ARCH, result.stdout)
        self.assertIn("Python 3.10", result.stdout)

    def test_the_startup_log_is_made_anew_for_every_launch(self):
        # It was only emptied: a log made once under umask 0 stayed writable for everyone (found 0666 in the user's
        # project folder), and one that cannot be opened (left by a run with sudo) stopped every launch, with the old
        # log shown as the reason.
        self.make_bundle()
        log = os.path.join(self.project, "gui_startup.log")
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)
        for n, mode in enumerate((0o666, 0o444), 1):
            with self.subTest(mode=oct(mode)):
                with open(log, "w") as f:
                    f.write("the log of an earlier run\n")
                os.chmod(log, mode)
                result = self.launch()
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(len(self.started()), n)                # the program was started
                self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o644)

    def test_a_second_launch_waits_while_another_one_unpacks(self):
        # A double click on the first start ran two launchers that unpacked into the same folder at once, and one of
        # them started the program on a runtime that was still being written (measured: 4 rounds of 15).
        self.make_bundle()
        other = subprocess.Popen(["sleep", "60"])                       # the launcher that is unpacking right now
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        lock = self.unpacked + ".lock"
        os.makedirs(lock)
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write(f"{other.pid}\n")
        launcher = subprocess.Popen(["sh", "start_gui.sh"], cwd=self.project, env={"PATH": self.tools, "DISPLAY": ":0"},
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: launcher.poll() is None and launcher.kill())
        time.sleep(2)
        self.assertEqual(self.started(), [])                            # it waits
        os.makedirs(self.unpacked, exist_ok=True)                       # the other one finishes and lets go
        subprocess.run(["tar", "-xzf", self.bundle, "-C", self.unpacked], check=True)
        mine = os.path.join(self.unpacked, "from the other launcher")
        open(mine, "w").close()
        open(os.path.join(self.unpacked, ".unpacked"), "w").close()
        shutil.rmtree(lock)
        out, _ = launcher.communicate(timeout=30)
        self.assertEqual(launcher.returncode, 0, out)
        self.assertEqual(len(self.started()), 1)
        self.assertTrue(os.path.exists(mine), "the runtime was unpacked a second time")

    def test_a_lock_left_by_a_killed_launch_does_not_block(self):
        self.make_bundle()
        gone = subprocess.Popen(["true"])
        gone.wait()                                                     # its pid belongs to nobody now
        lock = self.unpacked + ".lock"
        os.makedirs(lock)
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write(f"{gone.pid}\n")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.started()), 1)
        self.assertFalse(os.path.exists(lock))

    def test_cdpath_leads_neither_the_script_nor_bin_python_astray(self):
        # With CDPATH set, cd looks there first, and with "." in it (a common setting) dash and bash print where they
        # went: the real bin/python starts with rt=$(cd "$(dirname "$0")/.." && pwd -P), which then held the path
        # twice, and the program could not start (measured). The script was started as "sh <folder>/start_gui.sh".
        self.make_bundle('#!/bin/sh\nrt=$(cd "$(dirname "$0")/.." && pwd -P)\n'
                         'case $rt in *"\n"*) rt="(the path twice)" ;; esac\n'
                         'echo "$(pwd)|$rt|$*" >> started.log\nexec sleep 3\n')
        name = os.path.basename(self.project)
        decoy = tempfile.mkdtemp(prefix="decoy_", dir=SANDBOX)
        os.mkdir(os.path.join(decoy, name))                           # another folder of the same name
        for cdpath in (f".:{decoy}", f"{decoy}:."):
            with self.subTest(CDPATH=cdpath):
                open(os.path.join(self.project, "started.log"), "w").close()
                result = subprocess.run(
                    ["sh", os.path.join(name, "start_gui.sh")], cwd=os.path.dirname(self.project),
                    env={"PATH": self.tools, "DISPLAY": ":0", "CDPATH": cdpath},
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.started(), [f"{self.project}|{self.unpacked}|-m xdownloader"])
                self.assertEqual(os.listdir(os.path.join(decoy, name)), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
