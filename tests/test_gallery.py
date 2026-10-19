import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import FontLibrary, contained_path, make_server


def sample_font(family="测试宋体", weight=400):
    strings = bytearray()
    names = []
    for name_id, value in [(1, family), (2, "Regular"), (4, family + " Regular")]:
        encoded = value.encode("utf-16-be")
        names.append(struct.pack(">6H", 3, 1, 0x0804, name_id, len(encoded), len(strings)))
        strings.extend(encoded)
    name = struct.pack(">3H", 0, len(names), 6 + len(names) * 12) + b"".join(names) + strings
    os2 = bytearray(96)
    struct.pack_into(">H", os2, 4, weight)
    tables = [(b"name", name), (b"OS/2", os2)]
    offset = 12 + len(tables) * 16
    directory = bytearray()
    content = bytearray()
    for tag, data in tables:
        directory.extend(struct.pack(">4sIII", tag, 0, offset, len(data)))
        content.extend(data)
        offset += len(data)
    return struct.pack(">4s4H", b"\x00\x01\x00\x00", 2, 0, 0, 0) + directory + content


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = FontLibrary(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_discovers_chinese_names_nested_files_and_ignores_sidecars(self):
        nested = self.root / "中文子目录"
        nested.mkdir()
        (nested / "测试#1.otf").write_bytes(sample_font())
        (nested / "._测试#1.otf").write_bytes(b"not a font")
        snapshot = self.library.snapshot()
        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(snapshot["ignored"], 1)
        font = snapshot["fonts"][0]
        self.assertEqual(font["family"], "测试宋体")
        self.assertEqual(font["category"], "serif")
        self.assertEqual(font["weight"], 400)
        self.assertIn("%23", font["url"])
        self.assertEqual(snapshot["version"], self.library.snapshot()["version"])

    def test_add_modify_and_remove_are_reflected_without_restart(self):
        initial = self.library.snapshot()
        path = self.root / "new.ttf"
        path.write_bytes(sample_font())
        added = self.library.snapshot()
        self.assertNotEqual(initial["version"], added["version"])
        font_id = added["fonts"][0]["id"]
        path.write_bytes(sample_font("修改后的楷体", 700))
        modified = self.library.snapshot()
        self.assertEqual(modified["fonts"][0]["id"], font_id)
        self.assertEqual(modified["fonts"][0]["family"], "修改后的楷体")
        self.assertEqual(modified["fonts"][0]["category"], "handwriting")
        self.assertNotEqual(added["version"], modified["version"])
        path.unlink()
        self.assertEqual(self.library.snapshot()["count"], 0)

    def test_malformed_fonts_do_not_break_the_library(self):
        (self.root / "broken.ttf").write_bytes(b"broken")
        (self.root / "collection.ttc").write_bytes(b"ttcf")
        snapshot = self.library.snapshot()
        self.assertEqual(snapshot["count"], 1)
        self.assertIsNotNone(snapshot["fonts"][0]["error"])
        self.assertEqual(snapshot["unsupported"], 1)

    def test_paths_cannot_escape_the_font_directory(self):
        self.assertIsNone(contained_path(self.root, "../outside.ttf"))
        self.assertIsNone(contained_path(self.root, "..\\outside.ttf"))
        self.assertIsNone(contained_path(self.root, "bad\x00.ttf"))
        self.assertEqual(contained_path(self.root, "font.ttf"), self.root / "font.ttf")

    def test_missing_directory_is_reported_and_can_recover(self):
        library = FontLibrary(self.root / "not-yet-created")
        first = library.snapshot()
        self.assertFalse(first["exists"])
        library.directory.mkdir()
        (library.directory / "font.ttf").write_bytes(sample_font())
        self.assertTrue(library.snapshot()["exists"])
        self.assertEqual(library.snapshot()["count"], 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server = make_server(self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, path, data=None, headers=None):
        return urlopen(Request(self.origin + path, data=data, headers=headers or {}), timeout=5)

    def post(self, filename, data, origin=None):
        return self.request("/api/fonts?name=" + quote(filename), data, {
            "Origin": origin or self.origin, "X-Type-Room": "1", "Content-Type": "application/octet-stream",
        })

    def test_home_and_encoded_font_urls_are_served(self):
        filename = "测试字体#1.ttf"
        data = sample_font()
        (self.root / filename).write_bytes(data)
        with self.request("/") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("字间".encode("utf-8"), response.read())
        with self.request("/api/fonts") as response:
            snapshot = json.load(response)
        with self.request(snapshot["fonts"][0]["url"]) as response:
            self.assertEqual(response.headers["Content-Type"], "font/ttf")
            self.assertEqual(response.read(), data)

    def test_live_http_add_and_remove(self):
        with self.request("/api/fonts") as response:
            self.assertEqual(json.load(response)["count"], 0)
        path = self.root / "新增.ttf"
        path.write_bytes(sample_font())
        with self.request("/api/fonts") as response:
            self.assertEqual(json.load(response)["count"], 1)
        path.unlink()
        with self.request("/api/fonts") as response:
            self.assertEqual(json.load(response)["count"], 0)

    def test_import_preserves_existing_files(self):
        filename = "原字体.ttf"
        original = sample_font("原字体")
        (self.root / filename).write_bytes(original)
        with self.post(filename, sample_font("新字体")) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(json.load(response)["filename"], "原字体 (2).ttf")
        self.assertEqual((self.root / filename).read_bytes(), original)
        self.assertTrue((self.root / "原字体 (2).ttf").is_file())
        self.assertFalse(list(self.root.glob("*.upload")))

    def test_remote_origin_import_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.post("bad.ttf", sample_font(), origin="https://example.com")
        self.assertEqual(error.exception.code, 403)
        self.assertFalse((self.root / "bad.ttf").exists())

    def test_extensionless_bundle_import_is_accepted_by_its_header(self):
        data = b"UnityFS\0" + struct.pack(">I", 6) + b"5.x.x\0" + b"2021.3.33f1\0" + b"\0" * 80
        with self.post("new-tmp-font", data) as response:
            self.assertEqual(response.status, 201)
        self.assertEqual((self.root / "new-tmp-font").read_bytes(), data)
        with self.request("/api/fonts") as response:
            snapshot = json.load(response)
        self.assertEqual(snapshot["tmp_count"], 1)
        self.assertEqual(snapshot["fonts"][0]["kind"], "tmp")

    def test_render_parameters_are_limited_before_drawing(self):
        for query in ["size=NaN", "width=90000", "dpr=100", "text=" + "x" * 301]:
            with self.assertRaises(HTTPError) as error:
                self.request("/api/tmp/render?font=unknown&" + query)
            self.assertEqual(error.exception.code, 400)

    def test_invalid_files_and_traversal_uploads_are_rejected(self):
        for filename, data in [("../outside.ttf", sample_font()), ("broken.ttf", b"not a font"), ("CON.ttf", sample_font())]:
            with self.assertRaises(HTTPError) as error:
                self.post(filename, data)
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_encoded_path_traversal_is_rejected(self):
        for path in ["/fonts/%2e%2e/settings.json", "/%2e%2e/settings.json", "/fonts/..%5Csettings.json"]:
            with self.assertRaises(HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
