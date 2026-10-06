# -*- coding: utf-8 -*-
"""
生成程序图标 assets/icon.ico —— macOS 风格：
深红到黑渐变圆角底 + 白色摄像机 + 红色录制点，小尺寸自动简化细节。
（仅构建时需要 Pillow）
"""
import os

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "icon.ico")

TOP = (255, 69, 58)
BOT = (140, 20, 60)


def _vgrad(w, h, top, bot):
    col = Image.new("L", (1, h))
    px = col.load()
    for y in range(h):
        px[0, y] = int(y / max(1, h - 1) * 255)
    col = col.resize((w, h), Image.NEAREST)
    return Image.composite(Image.new("RGB", (w, h), bot),
                           Image.new("RGB", (w, h), top), col)


def _squircle_mask(size):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                       radius=size * 0.225, fill=255)
    return m


def _draw(size, ripple=True, pointer=True):
    S = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 投影
    sh = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sh).rounded_rectangle(
        [S * 0.075, S * 0.105, S * 0.935, S * 0.965], radius=S * 0.225, fill=90)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    shadow.putalpha(sh)
    img = Image.alpha_composite(img, shadow)

    # 渐变主体 + 顶部高光
    mask = _squircle_mask(S)
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    base.paste(_vgrad(S, S, TOP, BOT).convert("RGBA"), (0, 0), mask)
    img = Image.alpha_composite(img, base)

    hg = Image.new("L", (1, S))
    hp = hg.load()
    for y in range(S):
        hp[0, y] = int(max(0.0, 1.0 - y / (S * 0.52)) * 52)
    hg = ImageChops.darker(hg.resize((S, S), Image.NEAREST), mask)
    hl = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    hl.putalpha(hg)
    img = Image.alpha_composite(img, hl)

    d = ImageDraw.Draw(img)
    cx, cy = S * 0.5, S * 0.5

    # 录制点（右上角小红点 + 光晕）
    rx, ry, rr = S * 0.665, S * 0.325, S * 0.105
    glow = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    ImageDraw.Draw(glow).ellipse([rx - rr * 1.75, ry - rr * 1.75,
                                  rx + rr * 1.75, ry + rr * 1.75],
                                 fill=(255, 90, 80, 90))
    img.alpha_composite(glow.filter(__import__("PIL.ImageFilter",
                                              fromlist=["ImageFilter"])
                                    .GaussianBlur(S * 0.03)))

    # 摄像机机身 + 镜头
    d.rounded_rectangle([S * 0.175, S * 0.335, S * 0.60, S * 0.665],
                        radius=S * 0.075, fill=(255, 255, 255, 255))
    # 镜头梯形
    d.polygon([(S * 0.615, S * 0.415), (S * 0.80, S * 0.345),
               (S * 0.80, S * 0.655), (S * 0.615, S * 0.585)],
              fill=(255, 255, 255, 255))
    # 镜头内圈
    d.ellipse([S * 0.315, S * 0.405, S * 0.465, S * 0.595],
              fill=(200, 30, 40, 255))
    # 录制点
    d.ellipse([rx - rr, ry - rr, rx + rr, ry + rr], fill=(255, 255, 255, 255))
    d.ellipse([rx - rr * 0.55, ry - rr * 0.55, rx + rr * 0.55, ry + rr * 0.55],
              fill=(220, 30, 40, 255))
    return img


def build(size, detail):
    src = _draw(size * 4, ripple=detail >= 1, pointer=True)
    return src.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sizes = (256, 128, 64, 48, 40, 32, 24, 20, 16)
    frames = []
    for s in sizes:
        detail = 2 if s >= 48 else (1 if s >= 24 else 0)
        img = build(s, detail)
        img.save(os.path.join(HERE, "_icon_%d.png" % s))
        frames.append(img)
    frames[0].save(OUT, format="ICO",
                   sizes=[(s, s) for s in sizes], append_images=frames[1:])
    print("icon ->", OUT)
