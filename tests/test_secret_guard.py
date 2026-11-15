"""What keeps logins out of Git: the .gitignore allow-list, and tools/check-secrets.sh with the git hooks that run it
(nothing that looks like a login may be committed or pushed)."""
import os
import shutil
import subprocess
import unittest

from common import Base, PROJECT, git_ignores

SCRIPT = os.path.join(PROJECT, "tools", "check-secrets.sh")
HOOKS = ("pre-commit", "pre-push")
TAB = "\t"
# The samples are assembled here at run time: a line that looks like a login would make the guard refuse this very file.
TOKEN = "ab12" * 10                      # 40 hex digits, the shape of an auth_token


def diff_of(path, *added):
    """A unified diff that adds the file *path* holding the lines *added*."""
    body = "".join(f"+{line}\n" for line in added)
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(added)} @@\n{body}")


def scan(diff):
    return subprocess.run(["sh", SCRIPT], input=diff, capture_output=True, text=True)


def cookie_export():
    """What a browser extension writes: a Netscape cookie file (auth_token is an HttpOnly cookie)."""
    return "# Netscape HTTP Cookie File\n" + TAB.join(
        ["#HttpOnly_.x.com", "TRUE", "/", "TRUE", "1893456000", "auth_token", TOKEN]) + "\n"


class TestScanner(unittest.TestCase):
    LOGINS = {
        "Netscape cookie file, HttpOnly line": ("notes.txt", *cookie_export().splitlines()),
        "Netscape line of a session cookie": ("a.txt", TAB.join([".x.com", "TRUE", "/", "FALSE", "0", "guest_id", "v1"])),
        "JSON export on one line": ("c.json", f'[{{"domain": ".x.com", "name": "auth_token", "value": "{TOKEN}"}}]'),
        "JSON export, value on its own line": ("c.json", '  "name": "ct0",', f'  "value": "{TOKEN}",'),
        "Cookie header in a script": ("t.sh", f"curl -H 'Cookie: auth_token={TOKEN}; ct0={TOKEN}' https://x.com"),
        "environment variable": ("e.txt", f"X_AUTH_TOKEN={TOKEN}"),
        "Python dict": ("t.py", f"COOKIES = {{'auth_token': '{TOKEN}'}}"),
        "twid cookie (the numeric X user id)": ("a.txt", "twid=u%3D" + "1234567890123456789"),
        "private key": ("k.txt", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
    }
    NAMES = ("x.com_cookies.txt", "sub/Cookies.sqlite", "cookies.json", "settings.json", "logs/downloader.log",
             "list.txt", "failed_list.txt", "video.mp4", "video.mkv.part")
    ORDINARY = {
        "this project's own test fixtures": ("tests/t.py", '("auth_token", "AAA", ".x.com"), ("ct0", "BBB", ".x.com"),',
                                             'login_cookie="auth_token",'),
        "a long hex that is no token": ("README.md", "sha256: " + "cd34" * 16),
        "long digits in a tweet URL": ("tests/t.py", "https://x.com/user/status/" + "9" * 40),
        "tab separated text": ("data.tsv", TAB.join(["name", "size", "date"])),
        "a source file with cookies in its name": ("xdownloader/cookies.py", "def export_cookies(self): pass"),
    }

    def test_a_login_is_refused_whatever_the_file_is_called(self):
        for label, (path, *lines) in self.LOGINS.items():
            with self.subTest(label):
                result = scan(diff_of(path, *lines))
                self.assertEqual(result.returncode, 1)
                self.assertIn(path, result.stderr)
                self.assertNotIn(TOKEN, result.stderr)               # the value itself is never printed

    def test_files_that_must_never_be_committed_are_refused_by_name(self):
        for name in self.NAMES:
            with self.subTest(name):
                result = scan(diff_of(name, "x"))
                self.assertEqual(result.returncode, 1)
                self.assertIn(name, result.stderr)

    def test_ordinary_code_is_left_alone(self):
        for label, (path, *lines) in self.ORDINARY.items():
            with self.subTest(label):
                result = scan(diff_of(path, *lines))
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_binary_file_is_refused_unless_it_is_a_runtime_bundle_or_a_readme_screenshot(self):
        # Binary content never reaches the line checks: a browser's cookie database without "cookie" in its name
        # (Chromium calls it "Cookies") or a renamed copy went through. The only binary files are the runtime bundles
        # and the screenshots of the README: PNGs directly in docs/images/ (.gitignore opens exactly that, too).
        def binary(path, deleted=False):
            change = "deleted file mode 100644" if deleted else "new file mode 100644"
            ends = f"a/{path} and /dev/null" if deleted else f"/dev/null and b/{path}"
            return f"diff --git a/{path} b/{path}\n{change}\nindex 0000000..1234567\nBinary files {ends} differ\n"
        for path in ("profile/Default/Cookies", "backup.dat", "screenshot.png", "docs/screenshot.png",
                     "docs/images/sub/x.png", "docs/images/Cookies", "docs/images/clip.mov"):
            with self.subTest(path):
                result = scan(binary(path))
                self.assertEqual(result.returncode, 1)
                self.assertIn(path, result.stderr)
        self.assertEqual(scan(binary("bundled-runtime/linux-riscv64.tar.gz")).returncode, 0)
        self.assertEqual(scan(binary("docs/images/main-window.png")).returncode, 0)
        self.assertEqual(scan(binary("profile/Default/Cookies", deleted=True)).returncode, 0)    # removing one is the fix

    def test_removing_a_leaked_file_is_not_blocked(self):
        deletion = ("diff --git a/settings.json b/settings.json\ndeleted file mode 100644\n"
                    "--- a/settings.json\n+++ /dev/null\n@@ -1 +0,0 @@\n-{}\n")
        self.assertEqual(scan(deletion).returncode, 0)

    def test_the_tracked_files_hold_no_login(self):
        if git_ignores("README.md") is None:
            self.skipTest("not a git checkout")
        empty = subprocess.run(["git", "-C", PROJECT, "hash-object", "-t", "tree", os.devnull],
                               capture_output=True, text=True, check=True).stdout.strip()
        everything = subprocess.run(["git", "-C", PROJECT, "-c", "core.quotepath=off", "diff", "--no-color",
                                     "--no-ext-diff", "-U0", empty],
                                    capture_output=True, text=True, errors="replace", check=True).stdout
        result = scan(everything)
        self.assertEqual(result.returncode, 0, result.stderr)


class TestHooks(Base):
    """A hook that silently does nothing is worse than none: run the real ones in a throw-away repository."""

    def setUp(self):
        super().setUp()
        if shutil.which("git") is None:
            self.skipTest("git is missing")
        self.repo = os.path.join(self.base, "repo")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        os.makedirs(os.path.join(self.repo, "tools", "githooks"))
        shutil.copy2(SCRIPT, os.path.join(self.repo, "tools"))
        for hook in HOOKS:
            shutil.copy2(os.path.join(PROJECT, "tools", "githooks", hook), os.path.join(self.repo, "tools", "githooks"))
        self.git("init", "-q", "-b", "main")
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false"),
                           ("core.hooksPath", "tools/githooks")):
            self.git("config", key, value)
        self.git("add", "tools")
        first = self.git("commit", "-q", "-m", "tools")                 # a clean commit goes through the hook
        self.assertEqual(first.returncode, 0, first.stderr)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], env=self.env, capture_output=True, text=True)

    def write(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def test_pre_commit_refuses_a_cookie_export_whatever_it_is_called(self):
        self.write("x.com_cookies.txt", cookie_export())
        self.git("add", "-f", "x.com_cookies.txt")                      # .gitignore would not even let it in
        result = self.git("commit", "-q", "-m", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("x.com_cookies.txt", result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_pre_commit_refuses_a_login_pasted_into_a_source_file(self):
        self.write("helper.py", "x = 1\n" + cookie_export())
        self.git("add", "helper.py")
        self.assertNotEqual(self.git("commit", "-q", "-m", "oops").returncode, 0)

    def test_pre_commit_accepts_an_ordinary_change(self):
        self.write("README.md", "hello\n")
        self.git("add", "README.md")
        result = self.git("commit", "-q", "-m", "docs")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pre_push_is_the_second_gate(self):
        remote = os.path.join(self.base, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", remote], env=self.env, check=True)
        self.git("remote", "add", "origin", remote)
        first = self.git("push", "-q", "origin", "main")                # a clean history may leave the machine
        self.assertEqual(first.returncode, 0, first.stderr)
        self.write("my-cookies.json", f'[{{"name": "auth_token", "value": "{TOKEN}"}}]\n')
        self.git("add", "-f", "my-cookies.json")
        skipped = self.git("commit", "-q", "--no-verify", "-m", "hook skipped")
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        result = self.git("push", "origin", "main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("my-cookies.json", result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        on_remote = subprocess.run(["git", "-C", remote, "rev-parse", "main"], env=self.env,
                                   capture_output=True, text=True).stdout.strip()
        self.assertEqual(on_remote, self.git("rev-parse", "main~1").stdout.strip())     # only the clean push got there


    def test_a_link_that_became_a_file_is_scanned_by_both_gates(self):
        # A type change (T: a tracked link replaced by a file) was left out by --diff-filter=ACMR: nothing checked it.
        remote = os.path.join(self.base, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", remote], env=self.env, check=True)
        self.git("remote", "add", "origin", remote)
        os.symlink("README.md", os.path.join(self.repo, "notes.txt"))
        self.git("add", "notes.txt")
        self.assertEqual(self.git("commit", "-q", "-m", "link").returncode, 0)
        self.assertEqual(self.git("push", "-q", "origin", "main").returncode, 0)
        os.remove(os.path.join(self.repo, "notes.txt"))
        self.write("notes.txt", cookie_export())
        self.git("add", "notes.txt")
        refused = self.git("commit", "-q", "-m", "oops")                                  # gate 1: pre-commit
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("notes.txt", refused.stderr)
        self.assertEqual(self.git("commit", "-q", "--no-verify", "-m", "hook skipped").returncode, 0)
        pushed = self.git("push", "origin", "main")                                        # gate 2: pre-push
        self.assertNotEqual(pushed.returncode, 0)
        self.assertIn("notes.txt", pushed.stderr)


class TestHookFilesAreExecutable(unittest.TestCase):
    def test_git_would_skip_a_hook_that_is_not_executable(self):
        # git then only prints a hint and carries on, so the guard would be off without anyone noticing.
        if git_ignores("README.md") is None:
            self.skipTest("not a git checkout")
        listing = subprocess.run(["git", "-C", PROJECT, "ls-files", "-s", "tools/githooks"],
                                 capture_output=True, text=True, check=True).stdout
        modes = {fields[3]: fields[0] for fields in (line.split() for line in listing.splitlines())}
        self.assertEqual(modes, {f"tools/githooks/{hook}": "100755" for hook in HOOKS})


class TestGitignoreIsAnAllowList(unittest.TestCase):
    """The old deny-list let `git add .` pick up every file nobody had thought of (x.com_cookies.txt, .env, token.txt,
    a screenshot ...), so a browser's cookie export that kept its default name could be published."""

    # What a browser extension, a user or a tool is likely to leave in the folder, and files of unknown kinds
    # dropped into the folders that are allowed.
    UNKNOWN = (
        "x.com_cookies.txt", "www.x.com_cookies.txt", "my-cookies.txt", "cookies.json", "cookies.sqlite",
        ".env", "token.txt", "session.json", "notes.txt", "clip.mov", "screenshot.png", ".claude/settings.local.json",
        "tests/dump.json", "tests/x.com_cookies.txt", "xdownloader/session.json", "tools/leak.txt",
        "bundled-runtime/cookies.txt", "docs/notes.txt", "docs/screenshot.png", "docs/images/cookies.json",
        "docs/images/clip.mov", "docs/images/sub/x.png",
    )
    # New files of the kinds the project already has must keep working without touching .gitignore.
    ALLOWED = (
        "tests/test_new.py", "xdownloader/new_module.py", "tools/new-tool.sh", "tools/githooks/pre-commit",
        "bundled-runtime/linux-riscv64.tar.gz", "LICENSE", "CONTRIBUTING.md", "docs/images/new-shot.png",
    )

    def setUp(self):
        if git_ignores("README.md") is None:
            self.skipTest("not a git checkout")

    def test_a_file_nobody_listed_is_not_picked_up(self):
        self.assertEqual([p for p in self.UNKNOWN if not git_ignores(p)], [])

    def test_the_kinds_of_file_the_project_has_are_not_ignored(self):
        self.assertEqual([p for p in self.ALLOWED if git_ignores(p)], [])

    def test_every_tracked_file_is_covered(self):
        # A tracked file the patterns would ignore is silently dropped when the project is moved to a new
        # repository (`git add -A` there): add a "!" line to .gitignore for the new kind of file.
        out = subprocess.run(["git", "-C", PROJECT, "ls-files", "-ci", "--exclude-standard", "-z"],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual([p for p in out.split("\0") if p], [])


if __name__ == "__main__":
    unittest.main()
