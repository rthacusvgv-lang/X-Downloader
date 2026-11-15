"""i18n guard rails: every UI string and log message is translated, and a new language needs data only."""
import ast
import glob
import os
import re
import unittest
from unittest import mock

from common import PROJECT, Config, sites
from xdownloader import i18n

LOG_LEVELS = {"debug", "info", "warning", "error", "critical", "exception"}
RAW_OUTPUT = {"  yt-dlp: %s", "   %s"}      # yt-dlp's own output / a line whose argument is translated separately


def package_calls():
    """Every ast.Call in xdownloader/*.py."""
    for path in sorted(glob.glob(os.path.join(PROJECT, "xdownloader", "*.py"))):
        with open(path, encoding="utf-8") as f:
            yield from (n for n in ast.walk(ast.parse(f.read())) if isinstance(n, ast.Call))


def literal_arg(call):
    a = call.args[0] if call.args else None
    return a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None


def logged_messages():
    """Every literal log message template in the package (separator lines such as '----' excluded)."""
    found = set()
    for call in package_calls():
        f, msg = call.func, literal_arg(call)
        if (msg and isinstance(f, ast.Attribute) and f.attr in LOG_LEVELS
                and "log" in ast.unparse(f.value).lower() and set(msg) - set("-= ")):
            found.add(msg)
    return found - RAW_OUTPUT


def string_constants():
    consts = set()
    for path in glob.glob(os.path.join(PROJECT, "xdownloader", "*.py")):
        with open(path, encoding="utf-8") as f:
            consts.update(n.value for n in ast.walk(ast.parse(f.read()))
                          if isinstance(n, ast.Constant) and isinstance(n.value, str))
    return consts


FOREIGN_LANGUAGES = [lang for lang in Config.SUPPORTED_LANGUAGES if lang != "en"]
CONVERSION = re.compile(r"%[-+ #0]*\d*(?:\.\d+)?[sdrf%]")


def conversions(template: str) -> list[str]:
    """The %s / %d ... of a log template, in order (a literal %% is not an argument)."""
    return [c for c in CONVERSION.findall(template) if c != "%%"]


class TestTranslationTables(unittest.TestCase):
    def test_ui_tables_have_the_same_keys(self):
        english = set(i18n.TRANSLATIONS["en"])
        for lang in Config.SUPPORTED_LANGUAGES:
            self.assertEqual(set(i18n.TRANSLATIONS[lang]), english, lang)

    def test_every_literal_t_key_exists(self):
        keys = {literal_arg(c) for c in package_calls() if isinstance(c.func, ast.Name) and c.func.id == "t"}
        self.assertLessEqual(keys - {None}, set(i18n.TRANSLATIONS["en"]))

    def test_every_log_message_is_translated(self):
        messages = logged_messages()
        for lang in FOREIGN_LANGUAGES:
            self.assertEqual(messages - set(i18n.LOG_TRANSLATIONS[lang]), set(),
                             f"{lang}: log messages without a translation")

    def test_no_stale_log_translations(self):
        messages = logged_messages()
        for lang in FOREIGN_LANGUAGES:
            self.assertEqual(set(i18n.LOG_TRANSLATIONS[lang]) - messages, set(),
                             f"{lang}: translations of messages that are no longer logged")

    def test_translated_arguments_still_exist_in_the_source(self):
        consts = string_constants()
        for lang in FOREIGN_LANGUAGES:
            self.assertEqual(set(i18n.LOG_ARG_TRANSLATIONS[lang]) - consts, set(),
                             f"{lang}: argument translations whose source text changed")

    def test_ui_placeholders_match_the_english_text(self):
        for lang in FOREIGN_LANGUAGES:
            for key, english in i18n.TRANSLATIONS["en"].items():
                self.assertEqual(sorted(re.findall(r"\{(\w+)\}", i18n.TRANSLATIONS[lang][key])),
                                 sorted(re.findall(r"\{(\w+)\}", english)), f"{lang} / {key}")

    def test_log_arguments_match_the_english_template(self):
        # A translation with a different %s / %d sequence does not fail: it silently falls back to English.
        for lang in FOREIGN_LANGUAGES:
            for english, translated in i18n.LOG_TRANSLATIONS[lang].items():
                self.assertEqual(conversions(translated), conversions(english), f"{lang}: {english!r}")

    def test_english_never_puts_a_plural_noun_after_a_count(self):
        # "1 valid URLs" / "1 videos": write URL(s) / video(s). The Chinese text has no plural to get wrong.
        plural = re.compile(r"(%d|\{count\})[^.\n]{0,22}?\b(URLs|videos|items|tasks|seconds|files|folders)\b")
        texts = list(i18n.TRANSLATIONS["en"].values()) + sorted(logged_messages())
        self.assertEqual([t for t in texts if plural.search(t)], [])

    def test_one_word_for_a_folder_in_every_language(self):
        # The Chinese UI mixed 目錄 and 資料夾 (and English directory / folder) for the same thing.
        # 家目錄 (the home folder) is the standard Linux term and stays.
        zh = list(i18n.TRANSLATIONS["zh_TW"].values()) + list(i18n.LOG_TRANSLATIONS["zh_TW"].values())
        self.assertEqual([t for t in zh if "目錄" in t.replace("家目錄", "")], [])
        english = list(i18n.TRANSLATIONS["en"].values()) + sorted(logged_messages())
        self.assertEqual([t for t in english if re.search(r"\bdirector(y|ies)\b", t, re.I)], [])

    def test_site_login_hints_are_translated(self):
        # A new Site needs its login_hint added to LOG_ARG_TRANSLATIONS (otherwise it stays English).
        for site in sites.SITES:
            for lang in FOREIGN_LANGUAGES:
                if site.login_hint:
                    self.assertIn(site.login_hint, i18n.LOG_ARG_TRANSLATIONS[lang], f"{site.name} / {lang}")


class TestAddingALanguage(unittest.TestCase):
    def test_a_new_language_needs_data_only(self):
        table = {"Queue is empty, nothing to do.": "QUEUE EMPTY", "Loaded %d unfinished URLs from %s.": "%d <- %s"}
        args = {"list.txt": "LIST"}
        with mock.patch.dict(i18n.LOG_TRANSLATIONS, {"xx": table}), \
                mock.patch.dict(i18n.LOG_ARG_TRANSLATIONS, {"xx": args}), \
                mock.patch.object(Config, "LANGUAGE", "xx"):
            self.assertEqual(i18n.format_log_entry("Queue is empty, nothing to do.", (), None), "QUEUE EMPTY")
            self.assertEqual(i18n.format_log_entry("Loaded %d unfinished URLs from %s.", (3, "list.txt"), None),
                             "3 <- LIST")
            self.assertEqual(i18n.format_log_entry("not in the table: %s", ("a",), None), "not in the table: a")

    def test_english_logs_are_untouched(self):
        with mock.patch.object(Config, "LANGUAGE", "en"):
            self.assertEqual(i18n.format_log_entry("Queue is empty, nothing to do.", (), None),
                             "Queue is empty, nothing to do.")
            self.assertEqual(i18n.format_log_entry("x %s", ("y",), "TRACE"), "x y\nTRACE")


if __name__ == "__main__":
    unittest.main()
