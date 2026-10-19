import base64
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import FontLibrary
from tmp_fonts import is_bundle_header, normalize_font, render_text, style_weight
from PIL import Image


def modern_tree():
    return {
        "m_Name": "Test SDF", "m_FaceInfo": {"m_FamilyName": "Test", "m_PointSize": 10, "m_AscentLine": 8, "m_DescentLine": -2},
        "m_AtlasPadding": 0, "m_AtlasRenderMode": 4165,
        "m_GlyphTable": [{"m_Index": 7, "m_GlyphRect": {"m_X": 1, "m_Y": 1, "m_Width": 2, "m_Height": 2},
                          "m_Metrics": {"m_Width": 2, "m_Height": 2, "m_HorizontalBearingX": 0, "m_HorizontalBearingY": 8, "m_HorizontalAdvance": 10},
                          "m_Scale": 1, "m_AtlasIndex": 0}],
        "m_CharacterTable": [{"m_Unicode": ord("あ"), "m_GlyphIndex": 7, "m_Scale": 1},
                             {"m_Unicode": 0x20000, "m_GlyphIndex": 7, "m_Scale": .5}],
    }


class TmpRenderingTests(unittest.TestCase):
    def test_weight_matches_baked_font_style(self):
        self.assertEqual(style_weight("Bold"), 700)
        self.assertEqual(style_weight("Semi Bold"), 600)
        self.assertEqual(style_weight("ExtraLight"), 200)
        self.assertEqual(style_weight("Regular"), 400)

    def test_modern_character_mapping_includes_non_bmp_and_scale(self):
        font = normalize_font(modern_tree())
        self.assertEqual(font["family"], "Test")
        self.assertFalse(font["legacy"])
        self.assertEqual(font["glyphs"][str(ord("あ"))]["advance"], 10)
        self.assertEqual(font["glyphs"][str(0x20000)]["scale"], .5)

    def test_legacy_coordinates_and_metrics_are_preserved(self):
        tree = {"m_fontInfo": {"Name": "Old TMP", "PointSize": 10, "Ascender": 8, "Padding": 3},
                "m_glyphInfoList": [{"id": 65, "x": 2, "y": 3, "width": 4.4, "height": 5.6,
                                     "xOffset": 1, "yOffset": 8, "xAdvance": 6, "scale": 1}]}
        font = normalize_font(tree)
        self.assertTrue(font["legacy"])
        self.assertEqual(font["glyphs"]["65"]["h"], 6)
        self.assertEqual(font["glyphs"]["65"]["y"], 3)

    def test_real_atlas_pixels_are_drawn_and_missing_characters_reported(self):
        font = normalize_font(modern_tree())
        atlas = Image.new("L", (8, 8), 0)
        # Modern Unity coordinates count from the bottom; Pillow counts from the top.
        atlas.paste(255, (1, 5, 3, 7))
        result = render_text(font, lambda index: atlas, "あ🧪🧪", 10, 0, 100, 2)
        image = Image.open(io.BytesIO(base64.b64decode(result["image"].split(",", 1)[1])))
        self.assertEqual(image.size, (200, result["pixel_height"]))
        self.assertEqual(result["missing"], ["🧪"])
        self.assertGreater(image.getchannel("A").getpixel((7, 8)), 200)

    def test_newlines_and_wrap_increase_preview_height(self):
        font = normalize_font(modern_tree())
        atlas = Image.new("L", (8, 8), 255)
        single = render_text(font, lambda index: atlas, "あ", 10, 0, 80, 1)
        newline = render_text(font, lambda index: atlas, "あ\nあ", 10, 0, 80, 1)
        wrapped = render_text(font, lambda index: atlas, "あ" * 12, 10, 0, 80, 1)
        self.assertGreater(newline["height"], single["height"])
        self.assertGreater(wrapped["height"], single["height"])
        self.assertEqual(wrapped["missing"], [])

    def test_empty_font_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_font({"m_fontInfo": {}, "m_glyphInfoList": []})


class TmpDiscoveryTests(unittest.TestCase):
    def test_bundle_detection_is_based_on_content(self):
        self.assertTrue(is_bundle_header(b"UnityFS\0more bytes"))
        self.assertTrue(is_bundle_header(b"UnityRaw\0more bytes"))
        self.assertFalse(is_bundle_header(b"Unity is only a word"))

    def test_extensionless_bundle_appears_and_bad_bundle_does_not_break_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "any-new-name").write_bytes(b"UnityFS\0" + b"\0" * 80)
            (root / "ordinary-file").write_bytes(b"not a bundle")
            library = FontLibrary(root)
            try:
                snapshot = library.snapshot()
                self.assertEqual(snapshot["count"], 1)
                self.assertEqual(snapshot["fonts"][0]["kind"], "tmp")
                for _ in range(100):
                    snapshot = library.snapshot()
                    if snapshot["fonts"][0]["tmp_status"] != "pending":
                        break
                    time.sleep(.01)
                self.assertEqual(snapshot["fonts"][0]["tmp_status"], "error")
                self.assertTrue(snapshot["fonts"][0]["error"])
                (root / "any-new-name").unlink()
                self.assertEqual(library.snapshot()["count"], 0)
            finally:
                library.close()


if __name__ == "__main__":
    unittest.main()
