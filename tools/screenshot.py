"""离屏渲染预览图：assets/preview_pet.png / preview_panel.png

用法：python tools/screenshot.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.panel import DataPanel  # noqa: E402
from app.pet_window import PetWindow  # noqa: E402
from app.pomodoro import Pomodoro  # noqa: E402
from app.schedule_manager import ScheduleManager  # noqa: E402
from app.utils import init_db, load_config  # noqa: E402

W, H = 180, 200


def main():
    cfg = load_config()
    init_db()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    pet = PetWindow(cfg)
    pet.show()
    pet.engine.set_state("idle")
    pet.say("你好呀～我是你的桌宠，右键我可以看数据面板！", auto_hide_ms=0)
    for _ in range(40):
        app.processEvents()

    # 让 GIF 前进几帧
    QTimer.singleShot(600, lambda: None)
    import time
    time.sleep(0.4)
    app.processEvents()

    out1 = ROOT / "assets" / "preview_pet.png"
    from PIL import Image
    from PySide6.QtGui import QColor, QPainter

    # 气泡用 Qt 渲染；宠物用 pet_base.png（PIL 读 GIF 不应用透明索引）
    bubble_pm = pet.bubble.grab()
    bubble_img = Image.frombytes("RGBA", (bubble_pm.width(), bubble_pm.height()),
                                 bytes(bubble_pm.toImage().constBits()))
    base = Image.open(ROOT / "assets" / "pet_base.png").convert("RGBA")
    scale = min((W - 12) / base.width, (H - 12) / base.height)
    im = base.resize((int(base.width * scale), int(base.height * scale)), Image.LANCZOS)
    pet_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pet_img.alpha_composite(im, ((W - im.width) // 2, H - im.height - 4))
    canvas = Image.new("RGBA", (360, 400), (238, 243, 251, 255))
    canvas.alpha_composite(bubble_img, (30, 30))
    canvas.alpha_composite(pet_img, (90, 170))
    canvas.convert("RGB").save(out1)
    print("保存:", out1)

    sch = ScheduleManager(cfg)
    sch.store.load_from_json()
    pomo = Pomodoro(cfg)
    pomo.start("work")
    panel = DataPanel(cfg, pomo, sch)
    panel.show()
    app.processEvents()
    out2 = ROOT / "assets" / "preview_panel.png"
    panel.grab().save(str(out2))
    print("保存:", out2)


if __name__ == "__main__":
    main()
