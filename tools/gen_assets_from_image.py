"""从用户提供的角色立绘生成桌宠动画素材。

用法：python tools/gen_assets_from_image.py [图片路径]
默认使用 assets/source_character.png（首次会把剪贴板图复制过来）。

处理流程：
1. 洪水填充去除纯白背景（从四角出发）
2. 连通域分析只保留最大的角色主体（去掉气泡文字 / 水印等杂块）
3. 自动裁剪 -> assets/pet_base.png
4. 按状态机生成 8 个状态的 GIF（idle/thinking/speaking/happy/remind/working/break/sleep）
5. 用角色头像生成托盘图标
"""
from __future__ import annotations

import math
import shutil
import sys
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ANIM_DIR = ROOT / "assets" / "animations"
ICON_DIR = ROOT / "assets" / "icons"
SRC_DEFAULT = ROOT / "assets" / "source_character.png"
ANIM_DIR.mkdir(parents=True, exist_ok=True)
ICON_DIR.mkdir(parents=True, exist_ok=True)

W, H = 180, 200          # 与 config.yaml pet.width/height 一致
MAGIC = (255, 0, 255)    # 背景标记色

# 状态 -> (帧数, 每帧ms, 动作)   幅度已调低：待机时安静浮动，不乱跳
STATES = {
    "idle":     (8, 220, "bob"),
    "thinking": (8, 200, "tilt"),
    "speaking": (8, 130, "talk"),
    "happy":    (8, 110, "jump"),
    "remind":   (8, 180, "shake"),
    "working":  (8, 240, "bob"),
    "break":    (8, 300, "bob"),
    "sleep":    (6, 400, "sleep"),
}


# ---------------------------------------------------------------- 1. 去背景
def remove_background(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    rgb = img.convert("RGB")
    w, h = img.size

    # 从四个角 + 四条边中点洪水填充近似白色的背景
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
             (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    for s in seeds:
        try:
            ImageDraw.floodfill(rgb, s, MAGIC, thresh=48)
        except Exception:
            pass

    px_rgb = rgb.load()
    px_a = img.load()
    for y in range(h):
        for x in range(w):
            if px_rgb[x, y] == MAGIC:
                r, g, b = px_rgb[x, y]
                px_a[x, y] = (r, g, b, 0)

    # 连通域：只保留最大主体，去掉气泡文字 / 水印等孤立小块
    keep = largest_component(img)
    for y in range(h):
        for x in range(w):
            if not keep[y * w + x]:
                r, g, b, _ = px_a[x, y]
                px_a[x, y] = (r, g, b, 0)
    return img


def largest_component(img: Image.Image) -> list[bool]:
    """返回 mask（行优先），只有最大连通域为 True。"""
    w, h = img.size
    alpha = img.getchannel("A")
    mask = [v > 8 for v in alpha.getdata()]
    seen = [False] * (w * h)
    best, best_size = None, 0

    for start in range(w * h):
        if not mask[start] or seen[start]:
            continue
        comp = []
        q = deque([start])
        seen[start] = True
        while q:
            i = q.popleft()
            comp.append(i)
            x, y = i % w, i // w
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    j = ny * w + nx
                    if mask[j] and not seen[j]:
                        seen[j] = True
                        q.append(j)
        if len(comp) > best_size:
            best, best_size = comp, len(comp)

    out = [False] * (w * h)
    if best:
        for i in best:
            out[i] = True
    return out


def remove_bubble(img: Image.Image) -> Image.Image:
    """去除立绘右上角的对话气泡（斜线边界：上右为气泡，下左为角色）。"""
    img = img.crop(img.getchannel("A").getbbox())
    px = img.load()
    w, h = img.size
    X0 = min(232, w - 1)
    for y in range(0, min(165, h)):
        for x in range(X0, w):
            if y < 112 + (x - 232) * 0.55:
                r, g, b, a = px[x, y]
                px[x, y] = (r, g, b, 0)
    # 顶部残留的气泡边线
    for y in range(0, min(42, h)):
        for x in range(240, w):
            r, g, b, a = px[x, y]
            px[x, y] = (r, g, b, 0)
    img = img.crop(img.getchannel("A").getbbox())

    # 左侧残留的气泡弧线（白/灰/黑，保留蓝色头发）
    px = img.load()
    w, h = img.size
    for y in range(0, min(76, h)):
        for x in range(205, min(237, w)):
            r, g, b, a = px[x, y]
            if a != 0 and not (b - r > 25):
                px[x, y] = (r, g, b, 0)
    return img.crop(img.getchannel("A").getbbox())


# ---------------------------------------------------------------- 2. 生成动画
def fit_character(base: Image.Image) -> Image.Image:
    """缩放并贴到统一画布，脚底留边。"""
    max_w, max_h = W - 12, H - 12
    scale = min(max_w / base.width, max_h / base.height)
    im = base.resize((max(1, int(base.width * scale)),
                      max(1, int(base.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.alpha_composite(im, ((W - im.width) // 2, H - im.height - 4))
    return canvas


def frame(base: Image.Image, dy: float, dx: float = 0.0,
          tilt: float = 0.0, squash: float = 1.0,
          brightness: float = 1.0) -> Image.Image:
    im = base
    if abs(tilt) > 0.05:
        im = im.rotate(tilt, resample=Image.BICUBIC, expand=False)
    if abs(squash - 1.0) > 0.005:
        nw, nh = im.width, max(1, int(im.height * squash))
        im = im.resize((nw, nh), Image.BICUBIC)
    if abs(brightness - 1.0) > 0.01:
        im = ImageEnhance.Brightness(im).enhance(brightness)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.alpha_composite(im, (int(dx), int(dy)))
    return out


KEY = (255, 0, 255)      # 透明区域填充色（角色中不存在品红，便于做透明索引）


def _keyed(rgba: Image.Image) -> Image.Image:
    """把透明像素替换为纯品红，供量化后指定透明索引。"""
    a = rgba.getchannel("A")
    bg = Image.new("RGBA", rgba.size, KEY + (255,))
    return Image.composite(rgba, bg, a).convert("RGB")


def save_gif(frames, path: Path, durations):
    """共享调色板 + 正确的透明索引（修复黑框问题）。"""
    keyed = [_keyed(f) for f in frames]
    pal = keyed[0].quantize(colors=255)
    pframes = [k.quantize(palette=pal, dither=Image.Dither.NONE) for k in keyed]

    # 量化可能把品红映射到"近似品红"的相邻索引（如 253,0,253），
    # 这里把所有接近品红的索引强制归并到一个确切透明索引。
    pal_list = pframes[0].getpalette() or []
    if len(pal_list) < 768:
        pal_list = pal_list + [0] * (768 - len(pal_list))

    def _d2(i):
        r, g, b = pal_list[i * 3], pal_list[i * 3 + 1], pal_list[i * 3 + 2]
        return (r - KEY[0]) ** 2 + (g - KEY[1]) ** 2 + (b - KEY[2]) ** 2

    best = min(range(256), key=_d2)
    pal_list[best * 3: best * 3 + 3] = [KEY[0], KEY[1], KEY[2]]
    near = {i for i in range(256) if i != best and _d2(i) < 3000}
    for pf in pframes:
        pf.putpalette(pal_list)
        if near:
            pf.putdata([best if i in near else i for i in pf.getdata()])

    pframes[0].save(path, save_all=True, append_images=pframes[1:], loop=0,
                    duration=durations, disposal=2, transparency=best,
                    optimize=False)


def build_state(base: Image.Image, state: str, n: int, dur: int, motion: str):
    frames = []
    for i in range(n):
        t = i / n
        if motion == "bob":
            f = frame(base, dy=math.sin(t * 2 * math.pi) * 2)
        elif motion == "tilt":
            f = frame(base, dy=math.sin(t * 2 * math.pi) * 1.2,
                      tilt=math.sin(t * 2 * math.pi) * 1.2)
        elif motion == "talk":
            f = frame(base, dy=math.sin(t * 4 * math.pi) * 1.2,
                      squash=1 + 0.015 * math.sin(t * 4 * math.pi))
        elif motion == "jump":
            f = frame(base, dy=-abs(math.sin(t * 2 * math.pi)) * 12)
        elif motion == "shake":
            f = frame(base, dy=0, dx=math.sin(t * 4 * math.pi) * 6)
        elif motion == "sleep":
            f = frame(base, dy=math.sin(t * 2 * math.pi) * 1.5, tilt=2.0,
                      brightness=0.75)
        else:
            f = frame(base, dy=0)
        frames.append(f)

    out = ANIM_DIR / f"{state}.gif"
    save_gif(frames, out, [dur] * n)
    return out


def build_tray_icon(base: Image.Image):
    im = base.copy()
    im.thumbnail((120, 120), Image.LANCZOS)
    canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    canvas.alpha_composite(im, ((128 - im.width) // 2, (128 - im.height) // 2))
    out = ICON_DIR / "tray.png"
    canvas.save(out)
    return out


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC_DEFAULT
    if not src.exists():
        print(f"找不到图片：{src}")
        return 1
    if src.resolve() != SRC_DEFAULT.resolve():
        shutil.copy(src, SRC_DEFAULT)

    print("1) 去背景 + 去气泡 + 保留最大主体 …")
    cut = remove_background(Image.open(src))
    bbox = cut.getchannel("A").getbbox()
    if not bbox:
        print("   失败：没有识别到主体")
        return 1
    cut = remove_bubble(cut)
    cut.save(ROOT / "assets" / "pet_base.png")
    print(f"   主体尺寸 {cut.width}x{cut.height} -> assets/pet_base.png")

    print("2) 生成各状态 GIF …")
    base = fit_character(cut)
    for state, (n, dur, motion) in STATES.items():
        p = build_state(base, state, n, dur, motion)
        print(f"   {state}.gif  {p.stat().st_size/1024:.0f} KB")

    print("3) 托盘图标 …")
    print("  ", build_tray_icon(cut))
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
