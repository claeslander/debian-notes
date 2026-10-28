import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from debian_notes.app import BUNDLED_DICT_DIR, installed_dictionaries, link_bundled_dictionaries, spelling_languages  # noqa: E402


class SpellingTest(unittest.TestCase):
    def test_finds_dictionaries_with_aff_and_dic(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ["nl.aff", "nl.dic", "en_US.aff", "en_US.dic", "de_DE.dic", "hyph_nl.dic"]:
                open(os.path.join(d, name), "w").close()
            self.assertEqual(installed_dictionaries([d, "/nonexistent"]), {"nl", "en_US"})

    def test_bundles_dutch_and_english(self):
        self.assertEqual(installed_dictionaries([BUNDLED_DICT_DIR]), {"nl", "en_US"})

    def test_links_bundled_dictionaries_missing_from_system(self):
        with tempfile.TemporaryDirectory() as d:
            src, dest, system = (os.path.join(d, n) for n in ("src", "dest", "system"))
            for folder, names in ((src, ["nl.aff", "nl.dic", "en_US.aff", "en_US.dic"]),
                                  (system, ["en_US.aff", "en_US.dic"])):
                os.makedirs(folder)
                for name in names:
                    open(os.path.join(folder, name), "w").close()
            os.makedirs(dest)
            os.symlink("/old/checkout/nl.dic", os.path.join(dest, "nl.dic"))
            link_bundled_dictionaries(src, dest, [system])
            link_bundled_dictionaries(src, dest, [system])  # again: nothing changes
            self.assertEqual(sorted(os.listdir(dest)), ["nl.aff", "nl.dic"])
            self.assertEqual(os.readlink(os.path.join(dest, "nl.dic")), os.path.join(src, "nl.dic"))

    def test_english_locale_also_checks_dutch(self):
        langs = spelling_languages(["en_US", "en", "C"], {"en_US", "nl", "nl_BE", "nl_NL"})
        self.assertEqual(langs, ["en_US", "nl"])

    def test_locale_language_comes_first(self):
        self.assertEqual(spelling_languages(["nl_BE", "nl", "C"], {"en_US", "nl"}), ["nl", "en_US"])

    def test_without_known_dictionaries_uses_locale(self):
        self.assertEqual(spelling_languages(["en_US", "en", "C"], set()), ["en_US"])
        self.assertEqual(spelling_languages(["C"], set()), ["en_US"])


if __name__ == "__main__":
    unittest.main()
