"""把当前指令模板渲染成一张简短的帮助图片。"""

from io import BytesIO
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont


_WIDTH = 920
_MARGIN = 24
_PADDING = 44
_RADIUS = 30

_FONT_CANDIDATES = {
    "regular": (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ),
    "bold": (
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ),
}


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES["bold" if bold else "regular"]:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> List[str]:
    lines: List[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textlength(candidate, font=font) > width:
            lines.append(current.rstrip())
            current = char.lstrip()
        else:
            current = candidate
    if current:
        lines.append(current.rstrip())
    return lines or [""]


def _rounded(draw: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], radius: int, fill: str) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def render_help_image(
    title: str,
    sections: Sequence[Tuple[str, Sequence[Tuple[str, Sequence[str]]]]],
    footer: str,
    note: str,
) -> bytes:
    """渲染一张浅色卡片风格的帮助图；内容只使用当前配置的模板。"""
    title_font = _font(46, bold=True)
    section_font = _font(27, bold=True)
    label_font = _font(23, bold=True)
    body_font = _font(22)
    note_font = _font(20)
    padding = _PADDING
    content_width = _WIDTH - _MARGIN * 2 - padding * 2

    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    wrapped: List[Tuple[str, List[Tuple[str, List[List[str]]]]]] = []
    height = padding + 64
    for section_title, rows in sections:
        section_rows: List[Tuple[str, List[List[str]]]] = []
        height += 46
        for label, templates in rows:
            template_lines = [
                _wrap(measure, template, body_font, content_width - 18)
                for template in templates
            ]
            section_rows.append((label, template_lines))
            height += 34
            for lines in template_lines:
                height += len(lines) * 30 + 4
            height += 8
        wrapped.append((section_title, section_rows))
        height += 14
    height += 38
    footer_lines = _wrap(measure, footer, note_font, content_width)
    note_lines = _wrap(measure, note, note_font, content_width)
    height += len(footer_lines) * 27 + 18 + len(note_lines) * 25 + padding
    height = max(420, height)

    image = Image.new("RGB", (_WIDTH, height), "#f7eef3")
    draw = ImageDraw.Draw(image)
    _rounded(draw, (_MARGIN, _MARGIN, _WIDTH - _MARGIN, height - _MARGIN), _RADIUS, "#fffafd")
    # 顶部装饰色块，避免整张图过于单调。
    draw.rounded_rectangle((_MARGIN, _MARGIN, _WIDTH - _MARGIN, _MARGIN + 112), radius=_RADIUS, fill="#f3dfea")
    draw.rectangle((_MARGIN, _MARGIN + 72, _WIDTH - _MARGIN, _MARGIN + 112), fill="#f3dfea")

    x = _MARGIN + padding
    y = _MARGIN + 34
    draw.text((x, y), title, font=title_font, fill="#4f3542")
    y += 70

    for section_title, rows in wrapped:
        draw.text((x, y), section_title, font=section_font, fill="#b15d83")
        y += 40
        for label, template_lines in rows:
            draw.rounded_rectangle((x, y + 2, x + 8, y + 28), radius=4, fill="#e8a8c2")
            draw.text((x + 18, y), label, font=label_font, fill="#674c59")
            y += 32
            for lines in template_lines:
                for line in lines:
                    draw.text((x + 22, y), "· " + line, font=body_font, fill="#3e4a4f")
                    y += 30
                y += 4
            y += 8
        y += 12

    y += 8
    for line in footer_lines:
        draw.text((x, y), line, font=note_font, fill="#8a6b79")
        y += 27
    y += 12
    for line in note_lines:
        draw.text((x, y), line, font=note_font, fill="#a07b8b")
        y += 25

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
