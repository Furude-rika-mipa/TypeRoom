"""Read-only TextMeshPro bundle discovery and distance-field previews."""
from __future__ import annotations

import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
import gzip
import hashlib
import io
import json
import math
import re
import struct
from pathlib import Path
import threading

SCHEMA = 1
BUNDLE_HEADERS = (b"UnityFS\0", b"UnityRaw\0", b"UnityWeb\0")
BUNDLE_SUFFIXES = {"", ".bundle", ".unity3d", ".assetbundle", ".tmp"}


def is_bundle_header(header: bytes) -> bool:
    return header.startswith(BUNDLE_HEADERS)


def style_weight(style: str) -> int:
    style = re.sub(r"[\s_-]", "", style.casefold())
    for names, weight in [(("thin", "hairline"), 100), (("extralight", "ultralight"), 200),
                          (("light",), 300), (("medium",), 500), (("semibold", "demibold"), 600),
                          (("extrabold", "ultrabold"), 800), (("black", "heavy"), 900), (("bold",), 700)]:
        if any(name in style for name in names):
            return weight
    return 400


def normalize_font(tree: dict) -> dict:
    """Unify pre-2019 TMP glyph records and modern character/glyph tables."""
    glyphs = {}
    modern = "m_CharacterTable" in tree and "m_GlyphTable" in tree
    if modern:
        face = tree.get("m_FaceInfo", {})
        table = {glyph["m_Index"]: glyph for glyph in tree["m_GlyphTable"]}
        for character in tree["m_CharacterTable"]:
            glyph = table.get(character["m_GlyphIndex"])
            codepoint = int(character["m_Unicode"])
            if glyph is None or not 0 <= codepoint <= 0x10FFFF:
                continue
            rect, metrics = glyph["m_GlyphRect"], glyph["m_Metrics"]
            glyphs[str(codepoint)] = {
                "x": rect["m_X"], "y": rect["m_Y"], "w": rect["m_Width"], "h": rect["m_Height"],
                "bx": metrics["m_HorizontalBearingX"], "by": metrics["m_HorizontalBearingY"],
                "advance": metrics["m_HorizontalAdvance"], "atlas": glyph.get("m_AtlasIndex", 0),
                "scale": glyph.get("m_Scale", 1) * character.get("m_Scale", 1),
            }
        family = face.get("m_FamilyName") or tree.get("m_Name", "TMP Font")
        point = face.get("m_PointSize", 40)
        ascent = face.get("m_AscentLine", point)
        descent = face.get("m_DescentLine", -point * .2)
        padding = tree.get("m_AtlasPadding", 0)
        style = face.get("m_StyleName") or "Regular"
    else:
        face = tree.get("m_fontInfo", {})
        for glyph in tree.get("m_glyphInfoList", []):
            codepoint = int(glyph["id"])
            if not 0 <= codepoint <= 0x10FFFF:
                continue
            glyphs[str(codepoint)] = {
                "x": int(glyph["x"]), "y": int(glyph["y"]),
                "w": int(glyph["width"] + .5), "h": int(glyph["height"] + .5),
                "bx": glyph["xOffset"], "by": glyph["yOffset"],
                "advance": glyph["xAdvance"], "atlas": 0, "scale": glyph.get("scale", 1),
            }
        family = face.get("Name") or tree.get("m_Name", "TMP Font")
        point = face.get("PointSize", 40)
        ascent = face.get("Ascender", point)
        descent = face.get("Descender", -point * .2)
        padding = face.get("Padding", 0)
        style = "Regular"
    if not glyphs or not point or not math.isfinite(float(point)) or point <= 0:
        raise ValueError("TMP 字体没有有效的字符表或字号信息")
    return {
        "glyphs": glyphs, "family": family, "style": style, "point": float(point),
        "ascent": float(ascent), "descent": float(descent), "padding": max(0, min(64, int(padding))),
        "legacy": not modern, "render_mode": int(tree.get("m_AtlasRenderMode", 4134)),
    }


def render_text(font: dict, atlas_loader, text: str, size: float, spacing: float, width: int, dpr: float):
    """Rasterize actual bundle glyphs. Missing glyphs get boxes, never a fallback font."""
    from PIL import Image, ImageDraw

    glyphs = font["glyphs"]
    base_scale = size / font["point"]
    line_height = max(size * 1.65, (font["ascent"] - font["descent"]) * base_scale + 4)
    margin = 3
    baseline_offset = max(size * .85, font["ascent"] * base_scale) + margin
    x, row = float(margin), 0
    missing, placements = [], []
    max_height = min(4096, int(8_000_000 / max(1, width * dpr * dpr)))
    truncated = False
    for character in text.replace("\r\n", "\n").replace("\r", "\n"):
        if character == "\n":
            x, row = float(margin), row + 1
            if (row + 1) * line_height > max_height:
                truncated = True
                break
            continue
        if character == "\t":
            character = " "
            multiplier = 4
        else:
            multiplier = 1
        glyph = glyphs.get(str(ord(character)))
        scale = base_scale * (glyph.get("scale", 1) if glyph else 1)
        advance = ((glyph["advance"] * scale) if glyph else (size * .9)) * multiplier
        if x > margin and x + advance > width - margin:
            x, row = float(margin), row + 1
        if (row + 1) * line_height > max_height:
            truncated = True
            break
        baseline = row * line_height + baseline_offset
        if glyph is None and not character.isspace():
            if character not in missing:
                missing.append(character)
            placements.append((None, x, baseline, scale))
        elif glyph and not character.isspace() and glyph["w"] > 0 and glyph["h"] > 0:
            placements.append((glyph, x, baseline, scale))
        x += advance + spacing
    height = max(1, math.ceil((row + 1) * line_height + margin * 2))
    pixel_width, pixel_height = math.ceil(width * dpr), math.ceil(height * dpr)
    image = Image.new("RGBA", (pixel_width, pixel_height))
    draw = ImageDraw.Draw(image)
    padding = font["padding"]
    lookup_tables = {}
    # The SDF/SDFAA enum families set bit 0x20 or 0x40; smooth/raster modes do not.
    distance_field = bool(font["render_mode"] & 0x60)
    for glyph, x, baseline, scale in placements:
        if glyph is None:
            draw.rectangle((round((x + 2) * dpr), round((baseline - size * .75) * dpr),
                            round((x + size * .72) * dpr), round(baseline * dpr)),
                           outline=(182, 118, 63, 235), width=max(1, round(dpr)))
            continue
        atlas = atlas_loader(int(glyph["atlas"]))
        gx, gy, w, h = (int(glyph[key]) for key in ("x", "y", "w", "h"))
        top = gy if font["legacy"] else atlas.height - gy - h
        tile = atlas.crop((gx - padding, top - padding, gx + w + padding, top + h + padding))
        pixel_scale = max(.001, scale * dpr)
        tile = tile.resize((max(1, round(tile.width * pixel_scale)), max(1, round(tile.height * pixel_scale))), Image.Resampling.BILINEAR)
        if distance_field:
            key = round(pixel_scale, 6)
            if key not in lookup_tables:
                band = 255 / (4 * (padding + 1) * pixel_scale)
                lookup_tables[key] = [round(max(0, min(255, (v - 127.5 + band) * 255 / (2 * band)))) for v in range(256)]
            tile = tile.point(lookup_tables[key])
        ink = Image.new("RGBA", tile.size, (55, 63, 47))
        ink.putalpha(tile)
        image.alpha_composite(ink, (round((x + (glyph["bx"] - padding) * scale) * dpr),
                                   round((baseline - (glyph["by"] + padding) * scale) * dpr)))
    output = io.BytesIO()
    image.save(output, "PNG", compress_level=3)
    return {
        "image": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"),
        "width": width, "height": height, "pixel_width": pixel_width, "pixel_height": pixel_height,
        "missing": missing, "truncated": truncated,
    }


class TmpStore:
    def __init__(self, cache_directory: Path):
        self.directory = cache_directory
        self.entries = {}
        self.fonts_by_id = {}
        self.lock = threading.RLock()
        self.render_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tmp-font")
        self.atlases = OrderedDict()
        self.glyph_data = OrderedDict()
        self.renders = OrderedDict()
        self.closed = False

    def describe(self, path: Path, signature: tuple) -> list[dict]:
        source = str(path.resolve())
        with self.lock:
            entry = self.entries.get(source)
            if not entry or entry["signature"] != signature:
                key = hashlib.sha256(f"{SCHEMA}:{source}:{signature}".encode("utf-8")).hexdigest()[:32]
                entry = {"signature": signature, "key": key, "status": "pending", "fonts": []}
                self.entries[source] = entry
                try:
                    index = json.loads((self.directory / key / "index.json").read_text(encoding="utf-8"))
                    if index["schema"] != SCHEMA:
                        raise ValueError("old cache")
                    entry.update(status="ready", fonts=index["fonts"])
                except (OSError, ValueError, KeyError):
                    if not self.closed:
                        entry["future"] = self.executor.submit(self._extract_metadata, path, entry)
            if entry["status"] == "ready":
                return [{**font, "weight": style_weight(font["style"]), "tmp_status": "ready", "cache_key": entry["key"]} for font in entry["fonts"]]
            return [{
                "kind": "tmp", "format": "TMP", "family": path.stem, "name": path.name,
                "style": "TMP", "weight": 400, "variable": False,
                "tmp_status": entry["status"], "cache_key": entry["key"],
                "asset_id": "", "error": entry.get("error"),
            }]

    def wait_for_metadata(self, timeout=20):
        """Wait for already discovered bundles without rescanning the font folder."""
        with self.lock:
            futures = [entry["future"] for entry in self.entries.values()
                       if entry["status"] == "pending" and "future" in entry]
        if futures:
            wait(futures, timeout=timeout)
        with self.lock:
            return sum(entry["status"] == "pending" for entry in self.entries.values())

    def _extract_metadata(self, path: Path, entry: dict):
        try:
            try:
                import UnityPy
                from UnityPy.classes import PPtr
            except ImportError as error:
                raise ValueError("TMP 解析依赖未安装，请先运行 setup.cmd") from error
            with path.open("rb") as stream:
                header = stream.read(160)
            signature_end = header.index(b"\0") + 1
            if len(header) < signature_end + 8 or not 1 <= struct.unpack_from(">I", header, signature_end)[0] <= 20:
                raise ValueError("Unity 资源包头不完整或已损坏")
            # Loading bytes prevents UnityPy from retaining a lock on the user's file.
            env = UnityPy.load(path.read_bytes())
            version_strings = header[signature_end + 4:].split(b"\0", 2)
            version = version_strings[1].decode("ascii", errors="replace") if len(version_strings) > 1 else "Unknown"
            directory = self.directory / entry["key"]
            directory.mkdir(parents=True, exist_ok=True)
            fonts = []
            for obj in env.objects:
                if obj.type.name != "MonoBehaviour":
                    continue
                try:
                    tree = obj.read_typetree()
                except Exception:
                    continue
                if not (("m_GlyphTable" in tree and "m_CharacterTable" in tree) or "m_glyphInfoList" in tree):
                    continue
                normalized = normalize_font(tree)
                references = tree.get("m_AtlasTextures") or [tree.get("atlas", {})]
                atlases = []
                for reference in references:
                    pointer = PPtr(m_FileID=reference.get("m_FileID", 0), m_PathID=reference.get("m_PathID", 0), assetsfile=obj.assets_file)
                    reader = pointer.deref()
                    if reader is None or reader.type.name != "Texture2D":
                        raise ValueError("TMP 字体引用的图集缺失")
                    texture = reader.read()
                    atlases.append({"path_id": str(reader.path_id), "asset_file": reader.assets_file.name,
                                    "width": texture.m_Width, "height": texture.m_Height})
                asset_id = hashlib.sha256(f"{obj.assets_file.name}:{obj.path_id}".encode()).hexdigest()[:20]
                normalized["atlases"] = atlases
                with gzip.open(directory / f"{asset_id}.json.gz", "wt", encoding="utf-8") as output:
                    json.dump(normalized, output, ensure_ascii=True, separators=(",", ":"))
                fonts.append({
                    "kind": "tmp", "format": "TMP", "family": normalized["family"],
                    "name": tree.get("m_Name") or normalized["family"], "style": normalized["style"],
                    "weight": style_weight(normalized["style"]), "variable": False, "error": None, "asset_id": asset_id,
                    "engine_version": version, "character_count": len(normalized["glyphs"]),
                    "atlas_count": len(atlases), "atlas_width": atlases[0]["width"],
                    "atlas_height": atlases[0]["height"], "legacy": normalized["legacy"],
                })
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != entry["signature"]:
                raise ValueError("资源包仍在写入，等待下次扫描")
            temporary_index = directory / "index.json.tmp"
            temporary_index.write_text(json.dumps({"schema": SCHEMA, "fonts": fonts}, ensure_ascii=False), encoding="utf-8")
            temporary_index.replace(directory / "index.json")
            with self.lock:
                entry.update(status="ready", fonts=fonts)
        except Exception as error:
            with self.lock:
                entry.update(status="error", error=str(error)[:220] or "TMP 资源包解析失败")

    def register(self, font_id: str, path: Path, font: dict):
        with self.lock:
            self.fonts_by_id[font_id] = (path, font)

    def retain(self, font_ids: set, sources: set):
        with self.lock:
            self.fonts_by_id = {key: value for key, value in self.fonts_by_id.items() if key in font_ids}
            self.entries = {key: value for key, value in self.entries.items() if key in sources}

    @staticmethod
    def _remember(cache, key, value, limit):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            _, expired = cache.popitem(last=False)
            if hasattr(expired, "close"):
                expired.close()
        return value

    def _font_data(self, key, asset_id):
        identity = (key, asset_id)
        if identity in self.glyph_data:
            self.glyph_data.move_to_end(identity)
            return self.glyph_data[identity]
        with gzip.open(self.directory / key / f"{asset_id}.json.gz", "rt", encoding="utf-8") as stream:
            return self._remember(self.glyph_data, identity, json.load(stream), 3)

    def _atlas(self, source, key, descriptor):
        from PIL import Image
        identity = (key, descriptor["asset_file"], descriptor["path_id"])
        if identity in self.atlases:
            self.atlases.move_to_end(identity)
            return self.atlases[identity]
        filename = hashlib.sha256(repr(identity).encode()).hexdigest()[:20] + ".png"
        destination = self.directory / key / filename
        try:
            atlas = Image.open(destination)
            atlas.load()
        except (OSError, ValueError):
            import UnityPy
            env = UnityPy.load(source.read_bytes())
            reader = next((obj for obj in env.objects if str(obj.path_id) == descriptor["path_id"]
                           and obj.assets_file.name == descriptor["asset_file"]), None)
            if reader is None:
                raise ValueError("字体图集无法读取")
            image = reader.read().image
            atlas = image.getchannel("A") if "A" in image.getbands() else image.convert("L")
            temporary = destination.with_suffix(".tmp")
            atlas.save(temporary, "PNG", compress_level=1)
            temporary.replace(destination)
        return self._remember(self.atlases, identity, atlas, 2)

    def render(self, font_id, text, size, spacing, width, dpr):
        with self.lock:
            source, font = self.fonts_by_id.get(font_id, (None, None))
        if not font or font.get("tmp_status") != "ready":
            raise ValueError("TMP 字体尚未就绪，请稍后重试")
        stat = source.stat()
        if f"{stat.st_mtime_ns}-{stat.st_size}" != font["source_revision"]:
            raise ValueError("字体文件已更新，请点击“刷新字体”")
        cache_id = (font["cache_key"], font["asset_id"], text, size, spacing, width, dpr)
        with self.render_lock:
            if cache_id in self.renders:
                self.renders.move_to_end(cache_id)
                return self.renders[cache_id]
            data = self._font_data(font["cache_key"], font["asset_id"])
            def load_atlas(index):
                if not 0 <= index < len(data["atlases"]):
                    raise ValueError("字符引用的图集不存在")
                return self._atlas(source, font["cache_key"], data["atlases"][index])
            result = render_text(data, load_atlas, text, size, spacing, width, dpr)
            return self._remember(self.renders, cache_id, result, 64)

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=True)
        for atlas in self.atlases.values():
            atlas.close()
