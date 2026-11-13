#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成鸿蒙风格 App 图标（纯几何绘制，不依赖中文字体）。

产出 assets/icon.svg / icon-192.png / icon-512.png / icon-maskable-512.png
用法：python3 scripts/make_icons.py
"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets"
OUT.mkdir(exist_ok=True)

S = 4                      # 超采样倍数
BASE = 512
W = BASE * S

# 鸿蒙蓝渐变
TOP = (10, 89, 247)
BOTTOM = (55, 132, 252)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded(draw, box, r, fill):
    draw.rounded_rectangle(box, radius=r, fill=fill)


def make(maskable: bool) -> Image.Image:
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 背景：maskable 铺满整块（不留透明边），any 版留圆角
    if maskable:
        for y in range(W):
            d.line([(0, y), (W, y)], fill=lerp(TOP, BOTTOM, y / W) + (255,))
    else:
        for y in range(W):
            d.line([(0, y), (W, y)], fill=lerp(TOP, BOTTOM, y / W) + (255,))
        # 圆角遮罩
        mask = Image.new("L", (W, W), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, W - 1),
                                               radius=int(W * 0.22), fill=255)
        img.putalpha(mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    soft = (255, 255, 255, 90)

    # 图形安全区：maskable 缩到中心 62%，any 版 72%
    k = 0.62 if maskable else 0.72
    cx, cy = W / 2, W / 2
    span = W * k
    left, right = cx - span / 2, cx + span / 2
    top, bottom = cy - span / 2, cy + span / 2

    # 基线
    bw = max(2, int(W * 0.011))
    d.line([(left, bottom), (right, bottom)], fill=soft, width=bw)

    # 四根柱状（递增），圆角顶
    n = 4
    gap = span * 0.10
    cw = (span - gap * (n - 1)) / n
    heights = [0.30, 0.46, 0.38, 0.66]
    for i, h in enumerate(heights):
        x0 = left + i * (cw + gap)
        x1 = x0 + cw
        y0 = bottom - span * h
        rounded(d, (x0, y0, x1, bottom), int(cw * 0.28), (255, 255, 255, 55))

    # 上升折线 + 端点圆
    pts = [
        (left + cw * 0.5, bottom - span * 0.30),
        (left + cw * 1.5 + gap * 0.5, bottom - span * 0.47),
        (left + cw * 2.5 + gap, bottom - span * 0.40),
        (right - cw * 0.5, bottom - span * 0.76),
    ]
    lw = max(3, int(W * 0.026))
    d.line(pts, fill=white, width=lw, joint="curve")
    r = W * 0.026
    for x, y in pts:
        d.ellipse((x - r, y - r, x + r, y + r), fill=white)
    # 末端高亮点
    ex, ey = pts[-1]
    rr = W * 0.048
    d.ellipse((ex - rr, ey - rr, ex + rr, ey + rr), outline=(255, 255, 255, 120), width=bw)

    return img.resize((BASE, BASE), Image.LANCZOS)


SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#0a59f7"/><stop offset="1" stop-color="#3784fc"/></linearGradient></defs>
<rect x="0" y="0" width="512" height="512" rx="112" fill="url(#g)"/>
<g fill="#ffffff" opacity=".22">
<rect x="118" y="266" width="58" height="128" rx="16"/>
<rect x="196" y="212" width="58" height="182" rx="16"/>
<rect x="274" y="240" width="58" height="154" rx="16"/>
<rect x="352" y="150" width="58" height="244" rx="16"/></g>
<polyline points="147,266 225,212 303,240 381,150" fill="none" stroke="#fff"
 stroke-width="14" stroke-linecap="round" stroke-linejoin="round"/>
<g fill="#fff"><circle cx="147" cy="266" r="12"/><circle cx="225" cy="212" r="12"/>
<circle cx="303" cy="240" r="12"/><circle cx="381" cy="150" r="14"/></g>
</svg>
'''

(OUT / "icon.svg").write_text(SVG, "utf-8")

a = make(False)
a.resize((192, 192), Image.LANCZOS).save(OUT / "icon-192.png")
a.save(OUT / "icon-512.png")
make(True).save(OUT / "icon-maskable-512.png")

for p in sorted(OUT.glob("*")):
    print(f"  {p.name:26s} {p.stat().st_size:>7} B")
print("图标生成完成（纯几何，无外部字体依赖）")
