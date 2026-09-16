"""生成占位动画素材（GIF）与托盘图标。

运行：python tools/gen_assets.py
会在 assets/animations/ 下生成各状态的 GIF，assets/icons/ 下生成 tray.png。
素材是纯程序绘制的占位资源，可随时替换为自己的 GIF/APNG。
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ANIM_DIR = ROOT / "assets" / "animations"
ICON_DIR = ROOT / "assets" / "icons"
ANIM_DIR.mkdir(parents=True, exist_ok=True)
ICON_DIR.mkdir(parents=True, exist_ok=True)

W, H = 180, 200
FPS_ACTIVE = 12

# 状态 -> (主色, 副色, 帧数, 每帧毫秒, 动作)
STATES = {
    "idle":     ((127, 178, 255), (206, 229, 255), 8, 140, "bob"),
    "thinking": ((176, 140, 255), (226, 214, 255), 8, 120, "tilt"),
    "speaking": ((111, 211, 155), (200, 245, 220), 8, 110, "talk"),
    "happy":    ((255, 179, 111), (255, 229, 194), 8, 90,  "jump"),
    "remind":   ((255, 123, 123), (255, 209, 209), 8, 130, "shake"),
    "working":  ((79, 195, 247), (200, 238, 255), 8, 130, "bob"),
    "break":    ((139, 233, 192), (216, 250, 236), 8, 160, "bob"),
    "sleep":    ((143, 160, 184), (216, 224, 235), 6, 320, "sleep"),
}


def draw_pet(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float,
             main, light, eye: str, mouth: str, tilt: float = 0.0):
    # 身体
    x0, y0, x1, y1 = cx - r, cy - r * 0.92, cx + r, cy + r * 1.05
    d.rounded_rectangle([x0, y0, x1, y1], radius=int(r * 0.9), fill=main,
                        outline=(60, 70, 95, 255), width=3)
    # 高光
    d.ellipse([cx - r * 0.55, y0 + r * 0.18, cx - r * 0.05, y0 + r * 0.62],
              fill=light)
    # 耳朵
    for sx in (-1, 1):
        ex = cx + sx * r * 0.62
        d.polygon([(ex - r * 0.22, y0 + r * 0.05), (ex + r * 0.22, y0 + r * 0.05),
                   (ex + sx * r * 0.05, y0 - r * 0.55)], fill=main,
                  outline=(60, 70, 95, 255))

    ey = cy - r * 0.15
    ex_off = r * 0.34
    if eye == "open":
        for sx in (-1, 1):
            d.ellipse([cx + sx * ex_off - r * 0.11, ey - r * 0.15,
                       cx + sx * ex_off + r * 0.11, ey + r * 0.15], fill=(45, 52, 70, 255))
            d.ellipse([cx + sx * ex_off - r * 0.03, ey - r * 0.09,
                       cx + sx * ex_off + r * 0.04, ey - r * 0.02], fill=(255, 255, 255, 255))
    elif eye == "closed":
        for sx in (-1, 1):
            d.line([cx + sx * ex_off - r * 0.12, ey, cx + sx * ex_off + r * 0.12, ey],
                   fill=(45, 52, 70, 255), width=3)
    elif eye == "happy":
        for sx in (-1, 1):
            d.arc([cx + sx * ex_off - r * 0.13, ey - r * 0.16,
                   cx + sx * ex_off + r * 0.13, ey + r * 0.14],
                  start=200, end=340, fill=(45, 52, 70, 255), width=3)
    elif eye == "zzz":
        for sx in (-1, 1):
            d.line([cx + sx * ex_off - r * 0.12, ey, cx + sx * ex_off + r * 0.12, ey],
                   fill=(45, 52, 70, 255), width=3)

    my = cy + r * 0.42
    if mouth == "smile":
        d.arc([cx - r * 0.2, my - r * 0.12, cx + r * 0.2, my + r * 0.22],
              start=15, end=165, fill=(45, 52, 70, 255), width=3)
    elif mouth == "open":
        d.ellipse([cx - r * 0.16, my - r * 0.05, cx + r * 0.16, my + r * 0.3],
                  fill=(45, 52, 70, 255))
    elif mouth == "flat":
        d.line([cx - r * 0.16, my, cx + r * 0.16, my], fill=(45, 52, 70, 255), width=3)
    elif mouth == "o":
        d.ellipse([cx - r * 0.1, my, cx + r * 0.1, my + r * 0.2], fill=(45, 52, 70, 255))


def build(state: str):
    main, light, n, dur, motion = STATES[state]
    frames = []
    for i in range(n):
        t = i / n
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        r = W * 0.30
        cx = W / 2
        base_cy = H * 0.58

        if motion == "bob":
            cy = base_cy + math.sin(t * 2 * math.pi) * 6
            eye = "closed" if i == 0 else "open"
            mouth = "smile"
        elif motion == "tilt":
            cy = base_cy + math.sin(t * 2 * math.pi) * 2
            eye = "closed" if i % 4 == 3 else "open"
            mouth = "flat"
        elif motion == "talk":
            cy = base_cy + math.sin(t * 4 * math.pi) * 2
            eye = "open"
            mouth = "open" if i % 2 == 0 else "smile"
        elif motion == "jump":
            cy = base_cy - abs(math.sin(t * 2 * math.pi)) * 20
            eye = "happy"
            mouth = "open"
        elif motion == "shake":
            cy = base_cy
            eye = "open"
            mouth = "o"
        elif motion == "sleep":
            cy = base_cy + math.sin(t * 2 * math.pi) * 3
            eye = "zzz"
            mouth = "flat"
        else:
            cy, eye, mouth = base_cy, "open", "smile"

        if motion == "shake":
            cx += math.sin(t * 4 * math.pi) * 10

        draw_pet(d, cx, cy, r, main + (255,), light + (255,), eye, mouth)

        # 思考气泡 / 提醒叹号 / Zzz 装饰
        if state == "thinking":
            for k in range(3):
                rr = 4 + k * 2
                ox = W / 2 + 44 + k * 9
                oy = H * 0.30 - k * 10 - (i % 3) * 2
                d.ellipse([ox - rr, oy - rr, ox + rr, oy + rr], fill=(176, 140, 255, 220))
        if state == "remind":
            d.rounded_rectangle([W / 2 - 8, H * 0.16, W / 2 + 8, H * 0.16 + 40],
                                radius=8, fill=(255, 90, 90, 235))
            d.ellipse([W / 2 - 4, H * 0.16 + 46, W / 2 + 4, H * 0.16 + 54],
                      fill=(255, 90, 90, 235))
        if state == "sleep":
            d.text((W / 2 + 32, H * 0.28 - (i % 3) * 6), "Z", fill=(120, 140, 170, 230))

        frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=64))

    out = ANIM_DIR / f"{state}.gif"
    frames[0].save(out, save_all=True, append_images=frames[1:], loop=0,
                   duration=dur, disposal=2, transparency=0, optimize=True)
    return out


def build_icon():
    img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    draw_pet(d, 64, 70, 46, (127, 178, 255, 255), (206, 229, 255, 255), "open", "smile")
    out = ICON_DIR / "tray.png"
    img.save(out)
    return out


if __name__ == "__main__":
    for s in STATES:
        p = build(s)
        print("生成:", p.relative_to(ROOT), f"{p.stat().st_size/1024:.1f} KB")
    print("生成:", build_icon().relative_to(ROOT))
