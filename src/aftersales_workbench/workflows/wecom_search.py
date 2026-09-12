"""识别企微左上方搜索框的蓝色焦点边框，不依赖整窗变化量。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image


def search_field_focused(image: Image) -> bool:
    rgb = image.convert("RGB")
    if rgb.width < 800 or rgb.height < 550:
        return False
    w, h = rgb.size
    pixels = rgb.load()
    x0, x1 = int(w * .035), int(w * .30)
    y0, y1 = int(h * .01), int(h * .13)
    blue = set()
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b = pixels[x, y]
            if r <= 150 and 70 <= g <= 210 and b >= 135 and b - r >= 55 and b - g >= 20:
                blue.add((x, y))

    # 两条长水平边加左右竖边才是焦点框；蓝色文字、列表高亮不算。
    edges = []
    for y in range(y0, y1):
        xs = [x for x in range(x0, x1) if (x, y) in blue]
        if not xs:
            continue
        left, right = xs[0], xs[-1]
        if w * .08 <= right - left <= w * .26 and len(xs) >= (right - left) * .85:
            edges.append((left, right, y))
    for left, right, top in edges:
        for other_left, other_right, bottom in edges:
            if not h * .025 <= bottom - top <= h * .08:
                continue
            tolerance = max(4, round(w / 350))
            if abs(left - other_left) > tolerance or abs(right - other_right) > tolerance:
                continue
            # 圆角使竖边比水平边的端点向外伸出少量像素。
            margin = max(5, round((bottom - top) * .25))
            radius = max(14, round(w * .01))
            sides = [sum(any((x, y) in blue for x in range(edge - radius, edge + radius + 1))
                         for y in range(top + margin, bottom - margin))
                     for edge in (left, right)]
            if min(sides) >= (bottom - top - 2 * margin) * .8:
                return True
    return False
