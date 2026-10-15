"""Ensure the complete translation implementation is self-contained in the .pyw.

Run without any ASR inference or real network calls. This test intentionally
launches a separate interpreter from a directory without translation/.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "live-caption-ja.pyw"
TRANSLATION_MODULES = ("text.py", "history.py", "api.py", "engine.py", "gui.py", "__init__.py")


class SingleFileBundleTests(unittest.TestCase):
    def test_all_translation_sources_embedded_verbatim(self):
        source = MAIN.read_text(encoding="utf-8-sig")
        for filename in TRANSLATION_MODULES:
            with self.subTest(filename=filename):
                begin = f"    # <<< INLINE translation/{filename} BEGIN >>>\n"
                end = f"    # <<< INLINE translation/{filename} END >>>"
                self.assertEqual(1, source.count(begin))
                self.assertEqual(1, source.count(end))
                embedded = textwrap.dedent(source.split(begin, 1)[1].split(end, 1)[0]).strip()
                original = (ROOT / "translation" / filename).read_text(
                    encoding="utf-8-sig").strip()
                self.assertEqual(original, embedded)

    def test_pyW_import_and_cli_without_translation_directory(self):
        with tempfile.TemporaryDirectory() as isolated:
            standalone = Path(isolated) / MAIN.name
            shutil.copyfile(MAIN, standalone)
            self.assertFalse((Path(isolated) / "translation").exists())
            env = dict(os.environ)
            # Prevent launcher bootstrap into a nonexisting temporary .venv.
            env["_LIVE_CAPTION_PYW_BOOTSTRAPPED"] = "1"
            env.pop("PYTHONPATH", None)
            # This tests the actual file's __main__ execution path, but asks
            # argparse for --help so audio capture and GPU inference never run.
            result = subprocess.run(
                [sys.executable, "-B", str(standalone), "--help"],
                cwd=isolated, env=env, capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("--llama-server", result.stdout)

            # runpy imports all bundled modules, including optional settings UI.
            probe = (
                "import runpy,sys,pathlib;"
                "x=runpy.run_path(sys.argv[1],run_name='standalone_probe');"
                "from translation import GoogleTranslateSink,TranslationAPI;"
                "from translation.gui import open_settings,open_history;"
                "assert x['GoogleTranslateSink'] is GoogleTranslateSink;"
                "assert not pathlib.Path(sys.modules['translation'].__file__).exists();"
                "print('STANDALONE_INLINE_MODULES_OK')"
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", probe, str(standalone)],
                cwd=isolated, env=env, capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("STANDALONE_INLINE_MODULES_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
