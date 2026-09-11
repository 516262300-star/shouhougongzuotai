"""本地 OCR 核验企微浅色聊天界面；不能识别时不猜测成功。"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image


def message_key(text: str) -> str:
    # OCR 常将中文标点识别为同类西文标点；正文和运单字符必须完整一致。
    return "".join(c for c in unicodedata.normalize("NFKC", text) if c.isalnum())


def group_key(text: str) -> str:
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    return re.sub(r"[·.。‐‑–—]", "-", text)


class WindowsLocalOcr:
    def __init__(self) -> None:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        self.engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
        if self.engine is None:
            raise RuntimeError("本机没有可用的 Windows 简体中文 OCR")
        self.max_dimension = OcrEngine.max_image_dimension

    def read(self, image: Image) -> str:
        from PIL import ImageOps
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter

        gray = ImageOps.autocontrast(image.convert("L"))
        factor = min(3.0, self.max_dimension / max(gray.size))
        rgba = gray.resize((int(gray.width * factor), int(gray.height * factor))).convert("RGBA")
        with DataWriter() as writer:
            writer.write_bytes(rgba.tobytes())
            with SoftwareBitmap.create_copy_from_buffer(
                writer.detach_buffer(), BitmapPixelFormat.RGBA8, rgba.width, rgba.height,
            ) as bitmap:
                async def recognize() -> str:
                    result = await self.engine.recognize_async(bitmap)
                    return result.text

                return asyncio.run(recognize())


class LocalReceiptOcr:
    def __init__(self) -> None:
        import onnxruntime
        from rapidocr_onnxruntime import RapidOCR

        onnxruntime.disable_telemetry_events()
        self.title_reader = WindowsLocalOcr()
        self.engine = RapidOCR(
            intra_op_num_threads=2, inter_op_num_threads=1,
            det_limit_type="max", det_limit_side_len=960,
        )

    def read_title(self, image: Image) -> str:
        return self.title_reader.read(image)

    def read(self, image: Image) -> str:
        import numpy as np

        rgb = image.convert("RGB").resize((image.width * 2, image.height * 2))
        rows, _ = self.engine(np.asarray(rgb), use_cls=False)
        if not rows or any(row[2] < .95 for row in rows):
            return ""
        return "\n".join(row[1] for row in rows)


@lru_cache(maxsize=1)
def _local_ocr() -> LocalReceiptOcr:
    # 后台在发送锁内串行使用；避免每个空队列周期反复加载识别模型。
    return LocalReceiptOcr()


@dataclass(frozen=True)
class ReceiptObservation:
    group_matches: bool
    input_empty: bool
    draft_matches: bool
    matching_bubbles: int
    status_clear: bool

    @property
    def sent_visible(self) -> bool:
        return (
            self.group_matches and self.input_empty
            and self.matching_bubbles == 1 and self.status_clear
        )


def _runs(values: list[int], max_gap: int = 0) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for value in values:
        if runs and value - runs[-1][1] <= max_gap:
            runs[-1] = (runs[-1][0], value + 1)
        else:
            runs.append((value, value + 1))
    return runs


class WeComReceiptReader:
    def __init__(self, ocr=None) -> None:
        self.ocr = ocr or _local_ocr()

    def inspect(self, image: Image, group: str, message: str) -> ReceiptObservation:
        from PIL import ImageChops

        rgb = image.convert("RGB")
        w, h = rgb.size
        if w < 800 or h < 550:
            raise ValueError("企微窗口过小，无法核验消息")
        # 按同一逻辑像素宽度处理，避免系统200%缩放时把字形过度放大。
        if w != 1400:
            rgb = rgb.resize((1400, round(h * 1400 / w)))
            w, h = rgb.size
        # 从底部输入面板定位聊天左右边界，兼容成员栏展开/收起。
        pixels = rgb.load()
        white_x = [x for x in range(int(w * .2), int(w * .99))
                   if min(pixels[x, int(h * .90)]) > 250]
        panels = [(a, b) for a, b in _runs(white_x)
                  if b - a > w * .4 and a < w * .4]
        if len(panels) != 1:
            raise ValueError("无法唯一定位企微输入面板")
        left, right = panels[0]
        # 沿面板两侧留白找顶部；不能按整行白色比例判断，否则长草稿会
        # 把面板顶部误判到文字下面，继而把仍有草稿误认为空框。
        inset = max(3, int(w * .003))
        rows = [y for y in range(int(h * .55), int(h * .90))
                if min(pixels[left + inset, y]) > 250
                and min(pixels[right - inset, y]) > 250]
        runs = [r for r in _runs(rows) if r[1] >= int(h * .89)]
        if not runs:
            raise ValueError("无法定位企微输入框顶部")
        panel_top = runs[-1][0]
        if not .60 * h < panel_top < .82 * h:
            raise ValueError("企微聊天布局不受支持")
        draft_box = (left + int(w * .007), panel_top + int(h * .05),
                     right - int(w * .007), int(h * .93))
        draft = rgb.crop(draft_box)
        dark = draft.convert("L").point(lambda v: 255 if v < 180 else 0)
        bbox = dark.getbbox()
        # 空框可能有一条闪烁插入光标；任何更宽的深色内容都不是空框。
        empty = bbox is None or bbox[2] - bbox[0] <= max(2, int(w / 700))
        draft_matches = not empty and message_key(self.ocr.read(draft)) == message_key(message)
        title = rgb.crop((left, int(h * .023), right - int(w * .05), int(h * .071)))
        read_title = getattr(self.ocr, "read_title", self.ocr.read)
        title_matches = group_key(read_title(title)) == group_key(group)

        # 蓝色、自右侧对齐的完整气泡才算本账号消息；左侧复述/机器人回复不算。
        red, green, blue = rgb.split()
        mask = ImageChops.multiply(red.point(lambda v: 255 if 160 <= v <= 220 else 0),
                                   green.point(lambda v: 255 if 205 <= v <= 245 else 0))
        mask = ImageChops.multiply(mask, blue.point(lambda v: 255 if v >= 235 else 0))
        mask = ImageChops.multiply(mask, ImageChops.subtract(blue, red).point(
            lambda v: 255 if v >= 25 else 0))
        data = mask.load()
        blue_rows = [y for y in range(int(h * .12), panel_top - 3)
                     if sum(bool(data[x, y]) for x in range(left, right, 3)) > w * .035]
        matches = 0
        clear = True
        # 长文字行会遮住大部分蓝色背景，不能把一个气泡按字行截成几块。
        for top, bottom in _runs(blue_rows, max_gap=int(h * .025)):
            if bottom - top < h * .035:
                continue
            box = mask.crop((left, top, right, bottom)).getbbox()
            if box is None:
                continue
            x0, _, x1, _ = box
            x0 += left
            x1 += left
            if x0 < left + (right - left) * .05 or abs(right - x1) > w * .015:
                continue
            bubble = rgb.crop((x0, top, x1, bottom))
            if message_key(self.ocr.read(bubble)) != message_key(message):
                continue
            matches += 1
            # 红色叹号和灰色旋转等待图标位于气泡左侧；出现任何深色标记则不确认。
            status = rgb.crop((max(left, x0 - int(w * .03)), top, x0 - 2, bottom))
            status_dark = status.convert("L").point(lambda v: 255 if v < 205 else 0)
            if status_dark.getbbox() is not None:
                clear = False
        return ReceiptObservation(title_matches, empty, draft_matches, matches, clear)
