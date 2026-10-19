"""Local font gallery with native fonts and optional TextMeshPro support."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import math
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid
import webbrowser
import zlib

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
if (BASE / ".deps").is_dir():
    sys.path.insert(0, str(BASE / ".deps"))
from tmp_fonts import TmpStore, BUNDLE_SUFFIXES, is_bundle_header

STATIC = BASE / "dist"
FORMATS = {".ttf", ".otf", ".woff", ".woff2"}
MAGIC = {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1", b"wOFF", b"wOF2"}
MAX_UPLOAD = 100 * 1024 * 1024
MAX_TABLE = 4 * 1024 * 1024


def font_tables(path: Path) -> dict[str, bytes]:
    """Read only metadata tables; do not load large CJK outline tables."""
    wanted = {b"name", b"OS/2", b"post", b"fvar"}
    tables = {}
    with path.open("rb") as stream:
        header = stream.read(48)
        if header[:4] not in MAGIC:
            raise ValueError("无法识别的字体文件")
        if header[:4] == b"wOF2":
            # WOFF2 metadata needs Brotli. Filename is a dependency-free fallback.
            return tables
        woff = header[:4] == b"wOFF"
        count = struct.unpack_from(">H", header, 12 if woff else 4)[0]
        if count > 512:
            raise ValueError("字体表数量异常")
        stream.seek(44 if woff else 12)
        records = stream.read(count * (20 if woff else 16))
        file_size = path.stat().st_size
        for index in range(count):
            offset = index * (20 if woff else 16)
            if woff:
                tag, start, length, original, _ = struct.unpack_from(">4sIIII", records, offset)
            else:
                tag, _, start, length = struct.unpack_from(">4sIII", records, offset)
                original = length
            if tag not in wanted:
                continue
            if length > MAX_TABLE or original > MAX_TABLE or start + length > file_size:
                raise ValueError("字体元数据不完整")
            stream.seek(start)
            data = stream.read(length)
            if woff and length < original:
                decompressor = zlib.decompressobj()
                data = decompressor.decompress(data, MAX_TABLE + 1)
                if len(data) != original:
                    raise ValueError("字体元数据解压失败")
            tables[tag.decode("ascii")] = data
    return tables


def font_names(data: bytes) -> dict[int, str]:
    if len(data) < 6:
        return {}
    _, count, strings = struct.unpack_from(">HHH", data)
    result = {}
    priorities = {}
    for index in range(min(count, 4096)):
        if 6 + (index + 1) * 12 > len(data):
            break
        platform, encoding, language, name_id, length, offset = struct.unpack_from(
            ">HHHHHH", data, 6 + index * 12)
        if name_id not in {1, 2, 4, 6, 16, 17}:
            continue
        raw = data[strings + offset:strings + offset + length]
        if len(raw) != length:
            continue
        if platform in {0, 3}:
            codec = "utf-16-be"
        elif platform == 1:
            codec = {0: "mac_roman", 1: "shift_jis", 2: "big5", 25: "gb18030"}.get(encoding)
            if not codec:
                continue
        else:
            continue
        try:
            value = raw.decode(codec).replace("\x00", "").strip()
        except (UnicodeError, LookupError):
            continue
        if not value or "\ufffd" in value or not value.replace("_", "").strip():
            continue
        score = (40 if platform == 3 else 10) + {
            0x0804: 50, 0x0404: 40, 0x0C04: 35, 0x0409: 20, 0x0411: 10,
        }.get(language, 0)
        if score > priorities.get(name_id, -1):
            result[name_id] = value
            priorities[name_id] = score
    return result


def classify_font(text: str, os2: bytes, post: bytes) -> str:
    if (len(post) >= 16 and struct.unpack_from(">I", post, 12)[0]) or re.search(
        r"mono|code|等宽", text, re.I
    ):
        return "mono"
    if re.search(r"fangsong|[宋明]|serif|song|ming", text, re.I):
        return "serif"
    if re.search(r"[楷隶篆行]|zhengkai|chunfeng|script|handwrit|cursive", text, re.I):
        return "handwriting"
    if re.search(r"sans|arial|gothic|ゴシック|[黑圆圓]|round", text, re.I):
        return "sans"
    if len(os2) >= 34:
        family_class = struct.unpack_from(">H", os2, 30)[0] >> 8
        if family_class == 10:
            return "handwriting"
        if len(os2) >= 42 and os2[32] == 2:
            if os2[35] == 9:
                return "mono"
            if 2 <= os2[33] <= 10:
                return "serif"
            if 11 <= os2[33] <= 15:
                return "sans"
        if 1 <= family_class <= 7:
            return "serif"
        if family_class == 8:
            return "sans"
    return "other"


def metadata(path: Path) -> dict:
    try:
        tables = font_tables(path)
        names = font_names(tables.get("name", b""))
        family = names.get(16) or names.get(1) or path.stem
        style = names.get(17) or names.get(2) or "Regular"
        full_name = names.get(4) or f"{family} {style}"
        if full_name.startswith("_"):
            full_name = f"{family} {style}"
        os2 = tables.get("OS/2", b"")
        weight = struct.unpack_from(">H", os2, 4)[0] if len(os2) >= 6 else 400
        category = classify_font(f"{family} {full_name} {path.stem}", os2, tables.get("post", b""))
        return {
            "family": family, "name": full_name, "style": style,
            "weight": max(1, min(1000, weight)), "category": category,
            "variable": "fvar" in tables, "error": None,
        }
    except (OSError, ValueError, struct.error, zlib.error) as error:
        return {
            "family": path.stem, "name": path.stem, "style": "Unknown",
            "weight": 400, "category": "other", "variable": False,
            "error": str(error) if isinstance(error, ValueError) else "读取字体信息失败",
        }


class FontLibrary:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.cache = {}
        self.detected = {}
        self.lock = threading.Lock()
        self.tmp = TmpStore(BASE / ".cache" / "tmp")

    def snapshot(self) -> dict:
        with self.lock:
            fonts = []
            ignored = 0
            unsupported = 0
            cache_keys = set()
            tmp_sources = set()
            tmp_ids = set()
            exists = self.directory.is_dir()
            if exists:
                for root, dirs, files in os.walk(self.directory, followlinks=False):
                    dirs[:] = sorted(
                        d for d in dirs if not d.startswith(".")
                        and (Path(root) / d).resolve().is_relative_to(self.directory)
                    )
                    for filename in sorted(files):
                        if filename.startswith("."):
                            ignored += 1
                            continue
                        path = Path(root) / filename
                        if path.suffix.lower() in {".ttc", ".otc"}:
                            unsupported += 1
                            continue
                        try:
                            if not path.resolve().is_relative_to(self.directory):
                                continue
                            stat = path.stat()
                            relative = path.relative_to(self.directory).as_posix()
                            signature = (stat.st_size, stat.st_mtime_ns)
                            cache_keys.add(relative)
                            detected = self.detected.get(relative)
                            if not detected or detected[0] != signature:
                                with path.open("rb") as stream:
                                    bundle = is_bundle_header(stream.read(12))
                                self.detected[relative] = (signature, bundle)
                            else:
                                bundle = detected[1]
                            if bundle:
                                tmp_sources.add(str(path.resolve()))
                                for index, tmp_font in enumerate(self.tmp.describe(path, signature)):
                                    identity = relative if index == 0 else f"{relative}#{tmp_font['asset_id']}"
                                    font_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
                                    source_revision = f"{stat.st_mtime_ns}-{stat.st_size}"
                                    font = {
                                        **tmp_font, "id": font_id, "filename": filename,
                                        "path": relative, "size": stat.st_size, "modified": stat.st_mtime,
                                        "category": classify_font(tmp_font["family"], b"", b""),
                                        "source_revision": source_revision,
                                        "revision": f"{source_revision}-{tmp_font['tmp_status']}-{tmp_font.get('asset_id', '')}",
                                    }
                                    fonts.append(font)
                                    tmp_ids.add(font_id)
                                    self.tmp.register(font_id, path, font)
                                continue
                            if path.suffix.lower() not in FORMATS:
                                continue
                            cached = self.cache.get(relative)
                            if not cached or cached[0] != signature:
                                cached = (signature, metadata(path))
                                self.cache[relative] = cached
                            font_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]
                            fonts.append({
                                **cached[1], "kind": "native", "id": font_id, "filename": filename,
                                "path": relative, "format": path.suffix[1:].upper(),
                                "size": stat.st_size, "modified": stat.st_mtime,
                                "revision": f"{stat.st_mtime_ns}-{stat.st_size}",
                                "url": f"/fonts/{quote(relative, safe='/')}?v={stat.st_mtime_ns}-{stat.st_size}",
                            })
                        except (OSError, ValueError):
                            continue
            self.cache = {key: value for key, value in self.cache.items() if key in cache_keys}
            self.detected = {key: value for key, value in self.detected.items() if key in cache_keys}
            self.tmp.retain(tmp_ids, tmp_sources)
            fonts.sort(key=lambda font: (font["family"].casefold(), font["weight"], font["path"]))
            version_data = [(font["id"], font["revision"]) for font in fonts]
            version = hashlib.sha256(json.dumps(
                [exists, version_data, ignored, unsupported], ensure_ascii=False
            ).encode("utf-8")).hexdigest()[:24]
            return {
                "version": version, "folder": str(self.directory), "exists": exists,
                "fonts": fonts, "count": len(fonts), "ignored": ignored,
                "unsupported": unsupported, "interval": 3000,
                "tmp_count": len(tmp_ids),
                "parsing": sum(font.get("tmp_status") == "pending" for font in fonts),
            }

    def close(self):
        self.tmp.close()


def contained_path(root: Path, relative: str) -> Path | None:
    try:
        if "\x00" in relative or "\\" in relative:
            return None
        candidate = (root / relative).resolve()
        return candidate if candidate.is_relative_to(root.resolve()) else None
    except (OSError, ValueError):
        return None


class GalleryHandler(BaseHTTPRequestHandler):
    server_version = "TypeRoom/1.0"

    @property
    def library(self):
        return self.server.library

    def log_message(self, format_string, *args):
        if args and isinstance(args[0], str) and "/api/fonts" in args[0]:
            return
        print(f"[{time.strftime('%H:%M:%S')}] {format_string % args}", flush=True)

    def valid_host(self) -> bool:
        try:
            host = urlsplit(f"//{self.headers.get('Host', '')}")
            return host.hostname in {"127.0.0.1", "localhost"} and host.port == self.server.server_port
        except ValueError:
            return False

    def write_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.valid_host():
            return self.write_json({"error": "无效的本机地址"}, 403)
        route = unquote(urlsplit(self.path).path)
        if route == "/api/fonts":
            return self.write_json(self.library.snapshot())
        if route == "/api/tmp/render":
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            try:
                font_id = query.get("font", [""])[0]
                text = query.get("text", [""])[0]
                size = float(query.get("size", ["36"])[0])
                spacing = float(query.get("spacing", ["0"])[0])
                width = int(query.get("width", ["400"])[0])
                dpr = float(query.get("dpr", ["1"])[0])
                if (len(text) > 300 or not all(math.isfinite(value) for value in (size, spacing, dpr))
                    or not 8 <= size <= 120 or not -1 <= spacing <= 8
                    or not 80 <= width <= 1600 or not 1 <= dpr <= 2):
                    raise ValueError("预览参数超出允许范围")
                result = self.library.tmp.render(font_id, text, size, spacing, width, dpr)
                return self.write_json(result)
            except (ValueError, KeyError, OSError) as error:
                return self.write_json({"error": str(error) or "TMP 字体预览失败"}, 400)
            except Exception:
                return self.write_json({"error": "TMP 图集无法解码，请检查资源包是否完整"}, 422)
        if route.startswith("/fonts/"):
            file = contained_path(self.library.directory, route[len("/fonts/"):])
            if not file or file.suffix.lower() not in FORMATS or file.name.startswith("."):
                return self.write_json({"error": "找不到字体"}, 404)
            content_type = {
                ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2",
            }[file.suffix.lower()]
        else:
            file = contained_path(STATIC, "index.html" if route == "/" else route.lstrip("/"))
            content_type = mimetypes.guess_type(str(file))[0] if file else None
        if not file or not file.is_file():
            return self.write_json({"error": "找不到文件"}, 404)
        try:
            with file.open("rb") as stream:
                self.send_response(200)
                self.common_headers()
                self.send_header("Content-Type", (content_type or "application/octet-stream") + (
                    "; charset=utf-8" if file.suffix in {".html", ".css", ".js", ".svg"} else ""
                ))
                self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                if self.command != "HEAD":
                    shutil.copyfileobj(stream, self.wfile, 256 * 1024)
        except ConnectionError:
            pass
        except OSError:
            self.close_connection = True

    def same_origin_request(self) -> bool:
        try:
            origin = urlsplit(self.headers.get("Origin", ""))
            return (
                self.valid_host() and self.headers.get("X-Type-Room") == "1"
                and origin.scheme == "http"
                and origin.netloc == self.headers.get("Host")
            )
        except ValueError:
            return False

    def do_POST(self):
        if not self.same_origin_request():
            self.close_connection = True
            return self.write_json({"error": "请从本机字体展厅操作"}, 403)
        parsed = urlsplit(self.path)
        if parsed.path == "/api/open-folder":
            if not self.library.directory.is_dir():
                return self.write_json({"error": "字体文件夹不存在，请检查 settings.json"}, 404)
            if sys.platform == "win32":
                try:
                    os.startfile(str(self.library.directory))
                except OSError:
                    return self.write_json({"error": "打开文件夹失败"}, 500)
                return self.write_json({"ok": True})
            return self.write_json({"error": "请手动打开字体目录", "folder": str(self.library.directory)}, 400)
        if parsed.path != "/api/fonts":
            return self.write_json({"error": "找不到接口"}, 404)
        filename = parse_qs(parsed.query).get("name", [""])[0]
        if (
            not filename or filename in {".", ".."} or filename.startswith(".")
            or any(char in filename for char in '/\\\x00<>:"|?*')
            or filename.endswith((".", " ")) or len(filename) > 200
            or Path(filename).suffix.lower() not in FORMATS | BUNDLE_SUFFIXES
            or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", filename, re.I)
        ):
            self.close_connection = True
            return self.write_json({"error": "请选择字体文件或 TMP 资源包"}, 400)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 4 <= length <= MAX_UPLOAD:
            self.close_connection = True
            return self.write_json({"error": "单个字体需小于 100 MB，且文件不能为空"}, 413)
        if not self.library.directory.is_dir():
            self.close_connection = True
            return self.write_json({"error": "字体文件夹不存在"}, 404)
        temporary = self.library.directory / f".typeroom-{uuid.uuid4().hex}.upload"
        try:
            self.connection.settimeout(60)
            first = self.rfile.read(4)
            if first == b"Unit" and length >= 12:
                first += self.rfile.read(8)
            bundle = is_bundle_header(first)
            if not bundle and (first[:4] not in MAGIC or Path(filename).suffix.lower() not in FORMATS):
                self.close_connection = True
                return self.write_json({"error": "这个文件不是有效的字体文件"}, 400)
            remaining = length - len(first)
            with temporary.open("xb") as stream:
                stream.write(first)
                while remaining:
                    chunk = self.rfile.read(min(256 * 1024, remaining))
                    if not chunk:
                        raise OSError("字体传输中断")
                    stream.write(chunk)
                    remaining -= len(chunk)
            target = self.library.directory / filename
            number = 1
            # Keep scans from observing partially copied files.
            with self.library.lock:
                while True:
                    try:
                        output = target.open("xb")
                    except FileExistsError:
                        number += 1
                        original = Path(filename)
                        target = self.library.directory / f"{original.stem} ({number}){original.suffix}"
                        continue
                    try:
                        with output, temporary.open("rb") as source:
                            shutil.copyfileobj(source, output)
                    except OSError:
                        target.unlink(missing_ok=True)
                        raise
                    break
            return self.write_json({"ok": True, "filename": target.name}, 201)
        except (OSError, TimeoutError):
            self.close_connection = True
            return self.write_json({"error": "导入失败，请检查文件夹权限和磁盘空间"}, 500)
        finally:
            temporary.unlink(missing_ok=True)


class GalleryServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        if hasattr(self, "library"):
            self.library.close()
        super().server_close()

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if not isinstance(error, ConnectionError):
            super().handle_error(request, client_address)


def make_server(directory: Path, port: int = 0):
    server = GalleryServer(("127.0.0.1", port), GalleryHandler)
    server.library = FontLibrary(directory)
    return server


def main():
    parser = argparse.ArgumentParser(description="字间 · 本地字体展厅")
    parser.add_argument("--font-dir", type=Path, help="覆盖 settings.json 中的字体目录")
    parser.add_argument("--port", type=int, help="本机端口，默认 8765")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()
    config_path = BASE / "settings.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
        directory = args.font_dir or Path(config.get("font_directory", "fonts"))
        directory = directory if directory.is_absolute() else BASE / directory
        preferred_port = args.port if args.port is not None else int(config.get("port", 8765))
        if not 0 <= preferred_port <= 65515:
            raise ValueError("端口需在 0 到 65515 之间")
    except (OSError, ValueError, TypeError) as error:
        print(f"配置读取失败：{error}")
        return 1
    server = None
    for candidate in ([0] if preferred_port == 0 else range(preferred_port, preferred_port + 20)):
        try:
            server = make_server(directory, candidate)
            break
        except OSError:
            continue
    if server is None:
        print("无法启动本地服务，请在 settings.json 中换一个端口。")
        return 1
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"\n字间 TYPE ROOM · 本地字体展厅\n字体目录：{directory.resolve()}\nLocal: {url}\n每 3 秒同步一次 · 按 Ctrl+C 退出\n", flush=True)
    if not directory.is_dir():
        print("提示：字体目录不存在，请检查 settings.json。", flush=True)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n字体展厅已关闭。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
