"""
海报标题/副标题文字的渲染叠加。

为什么不让 Seedream 直接把文字画进图里：AI 生图模型画中文字符经常变形/写错字，
这是模型本身的短板，不是靠改 prompt 能稳定解决的。所以海报分两步：Seedream 只出一张
"不含任何文字"的背景图(见 seedream.py 的 generate_poster_background，prompt 里会
明确要求"不要出现文字")，标题/副标题用 Pillow 真实渲染叠上去——字体、位置、颜色都是
代码控制的，不会乱码。
"""

from __future__ import annotations

import platform
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 常见系统级中文字体路径，按平台猜测。用户没在设置页填 posterFontPath 的话，
# 按顺序试第一个存在的文件。这些路径是各操作系统"开箱自带、大概率存在"的中文字体，
# 不是每台机器都一样(比如精简版 Windows/服务器版 Linux 可能一个都没有)，找不到就
# 报清晰的错误，不猜第二次、也不用毫无中文字形的默认字体硬画(那样会变成一串方块/问号)。
_CANDIDATE_FONT_PATHS = {
    "Darwin": [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ],
    "Windows": [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ],
    "Linux": [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ],
}


class PosterComposeError(RuntimeError):
    pass


def resolve_poster_font(explicit_path: str | None = None) -> Path:
    """按优先级找一个能用的中文字体文件：设置页手动填的 posterFontPath > 当前操作系统
    的常见系统字体。都找不到就抛出 PosterComposeError，报错信息里直接说清楚去哪填。
    """
    if explicit_path and explicit_path.strip():
        p = Path(explicit_path.strip()).expanduser()
        if not p.is_file():
            raise PosterComposeError(f"设置页填的海报字体文件不存在: {p}")
        return p

    for candidate in _CANDIDATE_FONT_PATHS.get(platform.system(), []):
        p = Path(candidate)
        if p.is_file():
            return p

    raise PosterComposeError(
        "找不到可用的中文字体文件，海报标题文字没法渲染。"
        "去「设置」页的「海报字体」里手动填一个支持中文的字体文件路径"
        "（.ttf/.ttc/.otf 都行，比如 Windows 上的 C:\\Windows\\Fonts\\msyh.ttc）。"
    )


def _fit_font_size(
    draw: ImageDraw.ImageDraw, text: str, font_path: Path, max_width: int, start_size: int, min_size: int
) -> ImageFont.FreeTypeFont:
    """标题长短不一，字号固定的话短标题好看、长标题会溢出画面——从 start_size 往下试，
    直到这行字的渲染宽度能塞进 max_width，最小不小于 min_size(再小也不给缩了，
    交给用户自己换一个短一点的标题)。
    """
    size = start_size
    while size > min_size:
        font = ImageFont.truetype(str(font_path), size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 2
    return ImageFont.truetype(str(font_path), min_size)


def _wrap_by_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按逐字符宽度换行——中文没有天然的"单词"分界，按空格断词的英文换行算法在这里
    不适用，逐字符量宽度、超了就换行简单可靠，中文场景够用(知识卡片/价格表条目
    本来也不长，不追求断词优雅)。"""
    lines: list[str] = []
    current = ""
    for ch in text:
        trial = current + ch
        if draw.textlength(trial, font=font) > max_width and current:
            lines.append(current)
            current = ch
        else:
            current = trial
    lines.append(current)
    return lines


def _split_infographic_sections(body_lines: list[str]) -> list[tuple[str, list[str]]]:
    """信息图正文支持用 "# 小标题" 分区；不写分区时自动归到"重点信息"。
    普通用户直接一行一条写也能用，想做攻略类复杂海报时再手动加几个分区标题。
    """
    sections: list[tuple[str, list[str]]] = []
    current_title = "重点信息"
    current_items: list[str] = []
    for raw in body_lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if current_items:
                sections.append((current_title, current_items))
            current_title = line.lstrip("#").strip() or "重点信息"
            current_items = []
        else:
            current_items.append(line)
    if current_items:
        sections.append((current_title, current_items))
    return sections or [("重点信息", [])]


def _draw_round_box(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    *,
    fill: tuple[int, int, int, int],
    outline: tuple[int, int, int, int],
    radius: int,
    width: int = 2,
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    font: ImageFont.FreeTypeFont,
    *,
    max_width: int,
    fill: tuple[int, int, int, int],
    line_gap: int,
    max_lines: int | None = None,
) -> int:
    lines = _wrap_by_width(draw, text, font, max_width)
    if max_lines is not None:
        lines = lines[:max_lines]
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), line or "国", font=font)
        y += bbox[3] - bbox[1] + line_gap
    return y


def _compose_infographic_poster(
    *,
    background_path: str,
    title: str,
    subtitle: str | None,
    dest_path: str,
    font_path: str | None,
    body_lines: list[str],
) -> None:
    """复杂攻略/科普信息图排版：顶部大标题 + 多分区卡片网格 + 底部提示。
    参考用户给的首尔攻略/出租车科普/出行方式对比图，核心是"多模块、卡片化、编号、
    强主次"，而不是只在底部压一条标题。
    """
    resolved_font = resolve_poster_font(font_path)
    with Image.open(background_path) as bg:
        img = bg.convert("RGBA")
    w, h = img.size
    draw = ImageDraw.Draw(img)

    # 用浅色纸张层盖住 AI 背景，保留一点氛围，但保证大量中文可读。
    paper = Image.new("RGBA", (w, h), (255, 250, 240, 224))
    img = Image.alpha_composite(img, paper)
    draw = ImageDraw.Draw(img)

    margin = int(min(w, h) * 0.035)
    gap = int(min(w, h) * 0.014)
    radius = int(min(w, h) * 0.014)
    title_font = _fit_font_size(draw, title, resolved_font, w - margin * 2, int(h * 0.07), 34)
    subtitle_font = ImageFont.truetype(str(resolved_font), max(22, int(h * 0.022)))
    section_font = ImageFont.truetype(str(resolved_font), max(24, int(h * 0.026)))
    item_title_font = ImageFont.truetype(str(resolved_font), max(18, int(h * 0.019)))
    item_font = ImageFont.truetype(str(resolved_font), max(15, int(h * 0.016)))
    small_font = ImageFont.truetype(str(resolved_font), max(13, int(h * 0.014)))

    palette = [
        ((37, 99, 235, 255), (239, 246, 255, 236), (191, 219, 254, 255)),
        ((22, 163, 74, 255), (240, 253, 244, 236), (187, 247, 208, 255)),
        ((234, 88, 12, 255), (255, 247, 237, 236), (254, 215, 170, 255)),
        ((219, 39, 119, 255), (253, 242, 248, 236), (251, 207, 232, 255)),
        ((126, 34, 206, 255), (250, 245, 255, 236), (221, 214, 254, 255)),
    ]

    # 顶部主标题区。
    y = margin
    draw.text((margin, y), title, font=title_font, fill=(10, 15, 25, 255))
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    y += title_bbox[3] - title_bbox[1] + int(gap * 0.6)
    if subtitle and subtitle.strip():
        _draw_round_box(
            draw,
            (margin, y, w - margin, y + int(h * 0.042)),
            fill=(255, 255, 255, 210),
            outline=(245, 158, 11, 180),
            radius=radius,
            width=2,
        )
        draw.text((margin + gap, y + int(gap * 0.4)), subtitle.strip(), font=subtitle_font, fill=(194, 65, 12, 255))
        y += int(h * 0.05)

    sections = _split_infographic_sections(body_lines)
    content_top = y + gap
    footer_h = int(h * 0.055)
    content_bottom = h - margin - footer_h
    columns = 3 if w >= h else 2
    card_w = (w - margin * 2 - gap * (columns - 1)) // columns

    # 每个 section 变成一张大卡，大卡内部前几条以编号条目呈现；内容多时自动多卡分布。
    cards: list[tuple[str, list[str], int]] = []
    for idx, (section_title, items) in enumerate(sections):
        chunk_size = 5 if columns == 2 else 4
        chunks = [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)] or [[]]
        for chunk_idx, chunk in enumerate(chunks):
            title_text = section_title if chunk_idx == 0 else f"{section_title} {chunk_idx + 1}"
            cards.append((title_text, chunk, idx))

    rows = max(1, (len(cards) + columns - 1) // columns)
    card_h = max(int(h * 0.18), (content_bottom - content_top - gap * (rows - 1)) // rows)
    card_h = min(card_h, int(h * 0.28))

    for idx, (section_title, items, section_idx) in enumerate(cards[: columns * rows]):
        col = idx % columns
        row = idx // columns
        x1 = margin + col * (card_w + gap)
        y1 = content_top + row * (card_h + gap)
        x2 = x1 + card_w
        y2 = min(y1 + card_h, content_bottom)
        accent, fill, outline = palette[section_idx % len(palette)]
        _draw_round_box(draw, (x1, y1, x2, y2), fill=fill, outline=outline, radius=radius, width=2)

        badge = int(min(card_w, card_h) * 0.16)
        draw.ellipse((x1 + gap, y1 + gap, x1 + gap + badge, y1 + gap + badge), fill=accent)
        badge_text = str(section_idx + 1)
        bb = draw.textbbox((0, 0), badge_text, font=section_font)
        draw.text(
            (x1 + gap + (badge - (bb[2] - bb[0])) / 2, y1 + gap + (badge - (bb[3] - bb[1])) / 2 - 2),
            badge_text,
            font=section_font,
            fill=(255, 255, 255, 255),
        )
        title_x = x1 + gap + badge + int(gap * 0.7)
        title_y = y1 + gap + int(badge * 0.12)
        _draw_wrapped_text(
            draw,
            section_title,
            (title_x, title_y),
            section_font,
            max_width=x2 - title_x - gap,
            fill=(17, 24, 39, 255),
            line_gap=2,
            max_lines=1,
        )

        item_y = y1 + gap + badge + int(gap * 0.65)
        for item_idx, item in enumerate(items[:6]):
            if item_y > y2 - int(gap * 1.5):
                break
            if "|" in item:
                left, _, right = item.partition("|")
                title_text = left.strip()
                body_text = right.strip()
            elif "：" in item:
                title_text, _, body_text = item.partition("：")
            elif ":" in item:
                title_text, _, body_text = item.partition(":")
            else:
                title_text, body_text = item, ""

            dot_r = max(7, int(item_font.size * 0.45))
            draw.ellipse((x1 + gap, item_y + 3, x1 + gap + dot_r, item_y + 3 + dot_r), fill=accent)
            text_x = x1 + gap + dot_r + 8
            draw.text((text_x, item_y), title_text.strip(), font=item_title_font, fill=(17, 24, 39, 255))
            item_y += item_title_font.size + 2
            if body_text.strip():
                item_y = _draw_wrapped_text(
                    draw,
                    body_text.strip(),
                    (text_x, item_y),
                    item_font,
                    max_width=x2 - text_x - gap,
                    fill=(55, 65, 81, 255),
                    line_gap=2,
                    max_lines=2,
                )
            item_y += int(gap * 0.5)

    # 底部收口条，模拟攻略海报常见的 slogan/提示栏。
    footer_y = h - margin - footer_h
    _draw_round_box(
        draw,
        (margin, footer_y, w - margin, h - margin),
        fill=(37, 99, 235, 235),
        outline=(191, 219, 254, 255),
        radius=radius,
        width=2,
    )
    footer_text = subtitle.strip() if subtitle and subtitle.strip() else "信息仅供参考，请以实际情况为准"
    footer_font = _fit_font_size(draw, footer_text, resolved_font, w - margin * 3, int(footer_h * 0.42), 16)
    bb = draw.textbbox((0, 0), footer_text, font=footer_font)
    draw.text(
        (margin + (w - margin * 2 - (bb[2] - bb[0])) / 2, footer_y + (footer_h - (bb[3] - bb[1])) / 2 - 2),
        footer_text,
        font=footer_font,
        fill=(255, 255, 255, 255),
    )

    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(dest_path, quality=94)


def compose_poster(
    *,
    background_path: str,
    title: str,
    subtitle: str | None,
    dest_path: str,
    font_path: str | None = None,
    body_lines: list[str] | None = None,
    layout_mode: str = "title",
) -> None:
    """把标题/副标题(+可选的多行正文 body_lines)叠到背景图底部，加一条从透明到半透明
    黑的渐变遮罩band垫在文字下面——海报设计的常见手法，不然文字直接盖在复杂背景图上
    经常看不清楚，不管背景是亮是暗，黑色渐变遮罩 + 白色文字这个组合的可读性都过得去，
    不用针对每张背景做对比度分析。

    body_lines 是价格表/知识卡片这类场景用的：每一行既可以是一段科普知识点(自动按宽度
    换行)，也可以是"项目名|价格"这种用竖线分隔的两栏格式(价格右对齐)，方便一行内同时
    放名称和数字对齐美观。普通宣传海报不传这个参数，行为跟以前完全一样。
    """
    if layout_mode == "infographic":
        _compose_infographic_poster(
            background_path=background_path,
            title=title,
            subtitle=subtitle,
            dest_path=dest_path,
            font_path=font_path,
            body_lines=body_lines or [],
        )
        return

    resolved_font = resolve_poster_font(font_path)

    with Image.open(background_path) as bg:
        img = bg.convert("RGBA")
    w, h = img.size

    draw = ImageDraw.Draw(img)
    max_text_width = int(w * 0.86)
    margin_x = int(w * 0.07)

    title_font = _fit_font_size(draw, title, resolved_font, max_text_width, start_size=int(h * 0.075), min_size=24)
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    title_h = title_bbox[3] - title_bbox[1]

    subtitle_font = None
    subtitle_h = 0
    if subtitle and subtitle.strip():
        subtitle_font = _fit_font_size(
            draw, subtitle, resolved_font, max_text_width, start_size=int(h * 0.035), min_size=16
        )
        subtitle_bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
        subtitle_h = subtitle_bbox[3] - subtitle_bbox[1]

    gap = int(h * 0.02)
    bottom_margin = int(h * 0.06)

    # blocks: 每个元素是 ("text", text, font) 或 ("row", left, right, font)，
    # 按从上到下的顺序排列，最后统一算总高度再决定遮罩从哪里开始、每块画在哪个 y。
    blocks: list[tuple] = [("text", title, title_font)]
    if subtitle_font:
        blocks.append(("text", subtitle or "", subtitle_font))

    if body_lines:
        body_font_size = max(16, int(h * 0.03))
        body_font = ImageFont.truetype(str(resolved_font), body_font_size)
        body_line_gap = int(body_font_size * 0.45)
        for raw_line in body_lines:
            line = raw_line.strip()
            if not line:
                continue
            if "|" in line:
                left, _, right = line.partition("|")
                blocks.append(("row", left.strip(), right.strip(), body_font))
            else:
                for wrapped in _wrap_by_width(draw, line, body_font, max_text_width):
                    blocks.append(("text", wrapped, body_font))

    # 算每个块的高度，块间距：标题/副标题之间用大间距(gap)，正文行之间用更紧凑的
    # body_line_gap，读起来像一份列表而不是松散的几行字。
    heights = []
    for i, block in enumerate(blocks):
        font = block[-1]
        bbox = draw.textbbox((0, 0), "国" if block[0] == "row" else (block[1] or "国"), font=font)
        heights.append(bbox[3] - bbox[1])

    total_h = sum(heights)
    for i in range(len(blocks) - 1):
        is_body_to_body = blocks[i][0] in ("text", "row") and i >= (2 if subtitle_font else 1) and blocks[i + 1][0] in ("text", "row") and (i + 1) >= (2 if subtitle_font else 1)
        total_h += (body_line_gap if body_lines and is_body_to_body else gap) if body_lines else gap

    content_top = h - bottom_margin - total_h
    # 遮罩默认从 60% 高度开始(短标题的老样子)；正文行一多，内容块会更高，遮罩跟着
    # 往上延伸盖住整个内容区，不然长内容会有一截露在遮罩外面看不清。
    band_start = min(int(h * 0.6), max(int(h * 0.1), content_top - int(h * 0.04)))

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for y in range(band_start, h):
        progress = (y - band_start) / max(1, h - band_start)
        alpha = int(190 * progress)
        overlay_draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    y_cursor = content_top
    for i, block in enumerate(blocks):
        kind = block[0]
        font = block[-1]
        color = (255, 255, 255, 255) if i == 0 else (230, 230, 230, 255)
        if kind == "text":
            _draw_text_with_outline(draw, (margin_x, y_cursor), block[1], font, fill=color)
        else:
            _, left, right, row_font = block
            _draw_text_with_outline(draw, (margin_x, y_cursor), left, row_font, fill=color)
            right_w = draw.textlength(right, font=row_font)
            _draw_text_with_outline(draw, (w - margin_x - right_w, y_cursor), right, row_font, fill=color)
        y_cursor += heights[i]
        if i < len(blocks) - 1:
            is_body_to_body = i >= (2 if subtitle_font else 1)
            y_cursor += (body_line_gap if body_lines and is_body_to_body else gap) if body_lines else gap

    final = img.convert("RGB")
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    final.save(dest_path, quality=92)


def _draw_text_with_outline(draw: ImageDraw.ImageDraw, pos, text: str, font, fill) -> None:
    x, y = pos
    outline_width = max(1, font.size // 24)
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 160))
    draw.text((x, y), text, font=font, fill=fill)
