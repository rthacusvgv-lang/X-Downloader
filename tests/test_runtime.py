"""The bundled Python and Tk (bundled-runtime/linux-<arch>.tar.gz): the promise is "nothing from the host is needed"."""
import glob
import io
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from common import PROJECT, SANDBOX, Base, Config, EnvManager

BUNDLE = os.path.join(PROJECT, "bundled-runtime", f"linux-{platform.machine()}.tar.gz")
ALL_BUNDLES = sorted(glob.glob(os.path.join(PROJECT, "bundled-runtime", "linux-*.tar.gz")))
# A copyright file says "see /usr/share/common-licenses/GPL-2" (libffi8: ".../{MPL-1.1,GPL-2,LGPL-2.1}") instead of
# repeating a common license.
COMMON_LICENSE = re.compile(r"/usr/share/common-licenses/(\{[A-Za-z0-9.,-]+\}|[A-Za-z0-9.-]*[A-Za-z0-9])")
PADDING = "# padding " + "x" * 2048 + "\n"      # > 1 KiB, or EnvManager calls the file corrupted
# exec() fails with ENOENT, exactly as for a generic Linux binary on a system without its dynamic loader.
NO_LOADER_HERE = "#!/no/such/loader\necho version 1.2.3 of $0\n" + PADDING
# NixOS keeps a stub at the usual place, which refuses to run anything with exit code 127.
STUB_REFUSES = '#!/bin/sh\n[ -n "$UNDER_LOADER" ] || exit 127\necho version 1.2.3 of $0\n' + PADDING
RUNS_HERE = "#!/bin/sh\necho version 1.2.3 of $0\n" + PADDING
FAKE_LOADER = '#!/bin/sh\n# called as: loader --library-path <dir> <program> <args>\nUNDER_LOADER=1 export UNDER_LOADER\nshift 2\nexec /bin/sh "$@"\n'


@unittest.skipUnless(os.path.exists(BUNDLE), "no bundled runtime for this CPU")
class TestBundledRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A space and Chinese in the path, like the real project folders of the users.
        cls.root = tempfile.mkdtemp(prefix="執行 環境_", dir=SANDBOX)
        subprocess.run(["tar", "-xzf", BUNDLE, "-C", cls.root], check=True)
        cls.python = os.path.join(cls.root, "bin", "python")

    def run_code(self, code: str, **env):
        """Runs *code* on the bundled Python and returns what it printed as JSON."""
        result = subprocess.run([self.python, "-c", code], cwd=PROJECT, env={**os.environ, **env},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_the_whole_program_loads_with_no_library_of_the_host(self):
        # A library missing from the bundle would not show as an error on a machine that happens to have it:
        # the loader falls back to the system folders. So look at what is really mapped.
        mapped = self.run_code(
            "import json\n"
            "import xdownloader.gui\n"
            "print(json.dumps(sorted({l.split(None, 5)[-1].strip() for l in open('/proc/self/maps') if '.so' in l})))\n"
        )
        foreign = [path for path in mapped if not path.startswith(self.root)]
        self.assertEqual(foreign, [])
        # Without Xft, Tk has bitmap fonts only: no Chinese, and CustomTkinter's rounded corners break.
        self.assertTrue(any("libXft" in path for path in mapped), mapped)

    def test_the_host_python_setup_is_ignored(self):
        path = self.run_code("import json, sys\nprint(json.dumps(sys.path))\n", PYTHONPATH="/host/packages")
        self.assertEqual([p for p in path if p and not p.startswith(self.root)], [])

    def test_no_link_leads_out_of_the_bundle(self):
        # Debian's sitecustomize.py is a link to /etc/python3.13/: on a host with that Python, the host's
        # configuration would run inside ours. Dangling here, so only the link itself shows the problem.
        escaping = []
        for folder, dirs, files in os.walk(self.root):
            for name in dirs + files:
                path = os.path.join(folder, name)
                if os.path.islink(path) and not os.path.realpath(path).startswith(self.root):
                    escaping.append(os.path.relpath(path, self.root))
        self.assertEqual(escaping, [])

    def test_https_works_without_the_certificates_of_the_host(self):
        # Debian's OpenSSL looks in /usr/lib/ssl, which most other distributions do not have.
        count = self.run_code("import json, ssl\n"
                              "print(json.dumps(ssl.create_default_context().cert_store_stats()['x509_ca']))\n")
        self.assertGreater(count, 100)

    def test_programs_of_the_host_are_not_forced_onto_the_bundled_libraries(self):
        # zenity, xdg-open or yt-dlp would crash or misbehave with the libraries of another distribution.
        env = self.run_code("import json, os\nprint(json.dumps(dict(os.environ)))\n")
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertNotIn("LD_PRELOAD", env)

    def test_sys_executable_can_start_python_again(self):
        # The real interpreter needs the bundled loader, so sys.executable must be the wrapper.
        child = self.run_code(
            "import json, subprocess, sys\n"
            "out = subprocess.run([sys.executable, '-c', 'print(40 + 2)'], stdout=subprocess.PIPE, text=True)\n"
            "print(json.dumps(out.stdout.strip()))\n"
        )
        self.assertEqual(child, "42")


@unittest.skipUnless(ALL_BUNDLES, "no bundled runtime")
class TestBundledLicenses(unittest.TestCase):
    """The bundles hand on Debian packages in binary form. Most of their licenses (glibc's LGPL, the GPL of libgcc_s,
    OpenSSL's Apache license ...) allow that only together with the license texts. Nothing has to run, so the bundles
    of all CPUs are checked."""

    def test_every_package_in_a_bundle_brings_its_license(self):
        for bundle in ALL_BUNDLES:
            with self.subTest(os.path.basename(bundle)), tarfile.open(bundle) as tar:
                texts = {}
                for member in tar:
                    name = os.path.normpath(member.name)
                    if member.isfile() and (name == "PACKAGES.txt" or name.startswith("share/")):
                        texts[name] = tar.extractfile(member).read().decode("utf-8", "replace")
                debian = {line.split("_")[0] for line in texts["PACKAGES.txt"].splitlines()
                          if "_" in line and "==" not in line}       # pip's lines are name==version
                self.assertIn("libc6", debian)                       # glibc: the LGPL is why the texts must be there
                licensed = {name.split("/")[2] for name in texts
                            if name.startswith("share/doc/") and name.endswith("/copyright") and texts[name].strip()}
                self.assertEqual(sorted(debian - licensed), [], "packages without their copyright file")
                self.assertEqual(sorted(licensed - debian), [], "copyright files of packages not in PACKAGES.txt")
                copyrights = {name: text for name, text in texts.items() if name.startswith("share/doc/")}
                # A pointer in a form the pattern does not know would leave its license text out unnoticed.
                self.assertEqual([name for name, text in copyrights.items()
                                  if text.count("common-licenses/") != len(COMMON_LICENSE.findall(text))], [],
                                 "pointers to common licenses that the pattern misses")
                referenced = {lic for text in copyrights.values() for found in COMMON_LICENSE.findall(text)
                              for lic in found.strip("{}").split(",")}
                self.assertEqual(sorted(t for t in referenced if f"share/common-licenses/{t}" not in texts), [],
                                 "license texts the copyright files point to")

    def test_the_freetype_credit_is_in_the_documentation(self):
        # The FreeType License (share/doc/libfreetype6/copyright) asks the documentation of every binary distribution
        # to say that the software is based in part on the work of the FreeType Team, and suggests the words. The year
        # is the last one in the copyright of the bundled version, so a rebuild with a newer FreeType shows up here.
        with open(os.path.join(PROJECT, "bundled-runtime", "README.md"), encoding="utf-8") as f:
            documentation = f.read()
        for bundle in ALL_BUNDLES:
            with self.subTest(os.path.basename(bundle)), tarfile.open(bundle) as tar:
                members = {os.path.normpath(m.name): m for m in tar}
                member = members.get("share/doc/libfreetype6/copyright")
                if member is None:
                    continue                  # a bundle without FreeType owes no credit
                text = tar.extractfile(member).read().decode("utf-8", "replace")
                years = re.search(r"^Copyright:\s*\d{4}-(\d{4})", text, re.M)
                self.assertIsNotNone(years, "no copyright years in the copyright file of FreeType")
                self.assertIn(f"Portions of this software are copyright © {years.group(1)} The FreeType Project "
                              "(www.freetype.org). All rights reserved.", documentation)


class TestSystemsWithoutTheUsualGlibc(Base):
    """Alpine (musl) and NixOS cannot start the generic Linux builds of yt-dlp and ffmpeg by themselves."""

    def setUp(self):
        super().setUp()
        self.runtime = os.path.join(self.base, ".runtime", "x86_64")
        os.makedirs(os.path.join(self.runtime, "lib"))
        loader = os.path.join(self.runtime, "lib", "ld-linux-fake.so.2")
        with open(loader, "w") as f:
            f.write(FAKE_LOADER)
        os.chmod(loader, 0o755)
        patcher = mock.patch("sys.prefix", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fetched = []

    def serve(self, program: str):
        """Replaces the network: every download delivers *program*, as a plain file or as the ffmpeg archive."""
        def fetch(url, dest, checksums=None):
            self.fetched.append(url)
            if dest != Config.FFMPEG_ARCHIVE:
                with open(dest, "w") as f:
                    f.write(program)
                return
            with tarfile.open(dest, "w:xz") as tar:
                data = program.encode()
                entry = tarfile.TarInfo("ffmpeg-master/bin/ffmpeg")
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
        patcher = mock.patch.object(EnvManager, "_fetch", side_effect=fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def version(self, path: str) -> str:
        return subprocess.run([path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=10).stdout.strip()

    def test_yt_dlp_runs_through_the_bundled_loader(self):
        self.serve(NO_LOADER_HERE)
        self.assertTrue(EnvManager(self.log)._download_yt_dlp())
        self.assertEqual(self.version(Config.YT_DLP_EXE), f"version 1.2.3 of {Config.YT_DLP_EXE}.bin")

    def test_ffmpeg_runs_through_the_bundled_loader(self):
        self.serve(NO_LOADER_HERE)
        self.assertTrue(EnvManager(self.log)._download_ffmpeg())
        self.assertEqual(self.version(Config.FFMPEG_EXE), f"version 1.2.3 of {Config.FFMPEG_EXE}.bin")

    def test_the_stub_of_nixos_counts_as_not_runnable(self):
        self.serve(STUB_REFUSES)
        self.assertTrue(EnvManager(self.log)._download_yt_dlp())
        self.assertEqual(self.version(Config.YT_DLP_EXE), f"version 1.2.3 of {Config.YT_DLP_EXE}.bin")

    def test_children_of_yt_dlp_do_not_get_the_bundled_libc(self):
        # yt-dlp restarts itself and needs LD_LIBRARY_PATH for that, then hands it on to everything it starts.
        # With a plain path, the /bin/sh of NixOS (newer than our glibc) dies on our libc and ffmpeg counts as
        # missing, so videos stay unmerged. $ORIGIN only leads to our libraries from the project folder.
        self.serve('#!/no/such/loader\necho "$LD_LIBRARY_PATH"\n' + PADDING)
        EnvManager(self.log)._download_yt_dlp()
        self.assertEqual(self.version(Config.YT_DLP_EXE), "$ORIGIN/.runtime/x86_64/lib")

    def test_the_next_batch_does_not_download_again(self):
        # The script that takes the program's place is small: the "> 1 KiB" check must look at the real program.
        self.serve(NO_LOADER_HERE)
        self.assertTrue(EnvManager(self.log).check_all())
        self.assertEqual(len(self.fetched), 2)
        self.assertTrue(EnvManager(self.log).check_all())
        self.assertEqual(len(self.fetched), 2)

    def test_the_project_folder_can_be_moved(self):
        self.serve(NO_LOADER_HERE)
        EnvManager(self.log)._download_yt_dlp()
        moved = self.base + " 新位置"
        shutil.move(self.base, moved)
        self.addCleanup(shutil.rmtree, moved, ignore_errors=True)
        program = os.path.join(moved, "yt-dlp")
        self.assertEqual(self.version(program), f"version 1.2.3 of {program}.bin")

    def test_a_normal_system_keeps_the_plain_program(self):
        self.serve(RUNS_HERE)
        self.assertTrue(EnvManager(self.log)._download_yt_dlp())
        self.assertFalse(os.path.exists(Config.YT_DLP_EXE + ".bin"))
        self.assertEqual(self.version(Config.YT_DLP_EXE), f"version 1.2.3 of {Config.YT_DLP_EXE}")

    def test_nothing_changes_on_a_python_without_bundled_loader(self):
        self.serve(NO_LOADER_HERE)
        with mock.patch("sys.prefix", self.base):
            self.assertTrue(EnvManager(self.log)._download_yt_dlp())
        self.assertFalse(os.path.exists(Config.YT_DLP_EXE + ".bin"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
