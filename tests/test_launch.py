import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launch import prepare_settings


class FirstRunSettingsTests(unittest.TestCase):
    def test_fresh_checkout_gets_a_usable_default_folder_and_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = {"font_directory": "./fonts", "port": 8765}
            (root / "settings.example.json").write_text(json.dumps(template), encoding="utf-8")
            result = prepare_settings(root)
            self.assertEqual(json.loads(result.read_text(encoding="utf-8")), template)
            self.assertTrue((root / "fonts").is_dir())

    def test_existing_font_directory_setting_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = '{"font_directory":"F:/我的字体","port":9000}\n'
            (root / "settings.json").write_text(existing, encoding="utf-8")
            prepare_settings(root)
            self.assertEqual((root / "settings.json").read_text(encoding="utf-8"), existing)


if __name__ == "__main__":
    unittest.main()
