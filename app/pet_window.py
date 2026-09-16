"""Task 2：透明置顶宠物窗口 + 气泡 + 托盘。

- 无边框 / 背景透明 / 置顶 / 不进任务栏（Qt.Tool）
- setMask(Alpha) 实现"空白区域鼠标穿透"，宠物本体可拖拽
- 气泡为独立置顶窗口，跟随宠物移动，支持流式追加文本
- 系统托盘图标与右键菜单
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QBitmap, QColor, QFont, QIcon, QImage,
                           QPainter, QPainterPath, QPixmap, QRegion)
from PySide6.QtWidgets import (QApplication, QLabel, QLineEdit, QMenu,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from app.animations import STATE_LABEL, AnimationEngine
from app.utils import DATA_DIR, ICON_DIR

_STATE_FILE = DATA_DIR / "ui_state.json"

# ---------------------------------------------------------------- 穿透开关
IS_WIN = sys.platform.startswith("win")


def set_click_through(widget: QWidget, enable: bool) -> None:
    """完全鼠标穿透（Windows: WS_EX_TRANSPARENT；其他: Qt 属性）。"""
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            hwnd = int(widget.winId())
            get = ctypes.windll.user32.GetWindowLongW
            get.restype = wintypes.LONG
            get.argtypes = [wintypes.HWND, ctypes.c_int]
            style = get(hwnd, GWL_EXSTYLE)
            if enable:
                style |= WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            return
        except Exception:
            pass
    widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, enable)


# ---------------------------------------------------------------- 气泡
class BubbleWindow(QWidget):
    """跟随宠物的气泡窗口，支持流式追加。"""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool |
            Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)
        self.label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        fam = cfg.get("ui", {}).get("font_family", "Microsoft YaHei")
        size = int(cfg.get("ui", {}).get("font_size", 11))
        self.label.setFont(QFont(fam, size))
        self.label.setStyleSheet(
            "QLabel{background:transparent;color:#1e2733;padding:10px 14px;}")
        self.label.setMinimumWidth(120)
        self.label.setMaximumWidth(340)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 16, 18)
        lay.addWidget(self.label)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    # -------------------------------------------------- 内容
    def set_text(self, text: str, auto_hide_ms: int | None = None):
        self.label.setText(text)
        self.label.adjustSize()
        self.adjustSize()
        self._reposition()
        self.show()
        if auto_hide_ms is None:
            auto_hide_ms = int(self.cfg.get("ui", {}).get("bubble_duration_ms", 9000))
        if auto_hide_ms > 0:
            self._hide_timer.start(auto_hide_ms)

    def append_text(self, delta: str):
        self.set_text(self.label.text() + delta, auto_hide_ms=0)

    def clear(self):
        self.label.clear()

    def _reposition(self):
        owner = self.parent()
        if not isinstance(owner, PetWindow):
            return
        g = owner.geometry()
        x = g.center().x() - self.width() // 2
        y = g.top() - self.height() - 6
        # 越界保护
        scr = QApplication.primaryScreen().availableGeometry()
        x = max(scr.left() + 4, min(x, scr.right() - self.width() - 4))
        if y < scr.top() + 4:
            y = g.bottom() + 6
        self.move(x, y)
        self.raise_()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(4, 4, -14, -16)
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        # 小尾巴
        tail_y = r.bottom()
        path.moveTo(r.center().x() - 8, tail_y)
        path.lineTo(r.center().x() + 2, tail_y + 14)
        path.lineTo(r.center().x() + 10, tail_y)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 246))
        p.drawPath(path.simplified())
        p.end()


# ---------------------------------------------------------------- 聊天输入框
class ChatInputWindow(QWidget):
    """极简聊天输入框（回车发送，Esc 关闭）。"""

    submitted = Signal(str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("和桌宠说说话")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool)
        self.resize(420, 92)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("说点什么吧…（Enter 发送 / Esc 关闭）")
        fam = cfg.get("ui", {}).get("font_family", "Microsoft YaHei")
        self.edit.setFont(QFont(fam, 11))
        self.edit.setStyleSheet(
            "QLineEdit{padding:10px 12px;border:1px solid #c9d6ea;border-radius:14px;"
            "background:#ffffff;color:#1e2733;}"
            "QLineEdit:focus{border:1px solid #7FB2FF;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.addWidget(self.edit)
        self.edit.returnPressed.connect(self._send)

    def _send(self):
        text = self.edit.text().strip()
        if not text:
            return
        self.edit.clear()
        self.submitted.emit(text)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)

    def popup(self, near: QPoint):
        self.move(near.x() - self.width() // 2, near.y() - self.height() - 16)
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()


# ---------------------------------------------------------------- 宠物窗口
class PetWindow(QWidget):
    chat_submitted = Signal(str)
    request_panel = Signal()
    request_quit = Signal()
    request_settings = Signal()
    personality_changed = Signal(str)
    pomodoro_toggled = Signal()
    double_clicked = Signal()

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        pc = cfg.get("pet", {})
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool |
            Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("DesktopPet")
        self.setFixedSize(int(pc.get("width", 180)), int(pc.get("height", 200)))
        self.setWindowOpacity(float(pc.get("opacity", 1.0)))
        if not pc.get("always_on_top", True):
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowStaysOnTopHint)

        self.pet_label = QLabel(self)
        self.pet_label.setGeometry(0, 0, self.width(), self.height())
        self.pet_label.setAlignment(Qt.AlignCenter)
        self.pet_label.setStyleSheet("background:transparent;")

        self.engine = AnimationEngine(cfg, self.pet_label, self)
        self.bubble = BubbleWindow(cfg, self)

        self._drag_offset: QPoint | None = None
        self._mask_state = None

        self._restore_position()
        self.engine.state_changed.connect(self._on_state_changed)
        # 初始就挂载动画 + 生成穿透遮罩（force 避免"状态相同被跳过"）
        self.engine.apply_state("idle", force=True)

    # ---------------------------------------------------------- 位置
    def _restore_position(self):
        x = int(self.cfg.get("pet", {}).get("x", -1))
        y = int(self.cfg.get("pet", {}).get("y", -1))
        saved = {}
        if _STATE_FILE.exists():
            try:
                saved = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                saved = {}
        if x < 0:
            x = saved.get("x", -1)
        if y < 0:
            y = saved.get("y", -1)
        if x < 0 or y < 0:
            scr = QApplication.primaryScreen().availableGeometry()
            x = scr.right() - self.width() - 40
            y = scr.bottom() - self.height() - 60
        self.move(x, y)

    def save_position(self):
        try:
            data = {}
            if _STATE_FILE.exists():
                data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            data.update({"x": self.x(), "y": self.y()})
            _STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ---------------------------------------------------------- 遮罩（穿透）
    def _on_state_changed(self, state: str):
        """按当前帧 Alpha 重建穿透遮罩；拿不到有效帧时退化为椭圆遮罩，
        绝不把整窗裁空（否则宠物会整体不可见）。"""
        if not self.cfg.get("pet", {}).get("mouse_transparent", True):
            return
        mv = self.engine._movies.get(state)
        img = mv.currentImage() if mv is not None else QImage()
        if img.isNull() or not self._has_opaque_body(img):
            self._apply_ellipse_mask()
            return
        try:
            alpha = img.createAlphaMask(Qt.ImageConversionFlag.MonoOnly)
            self.setMask(QRegion(QBitmap.fromImage(alpha)))
        except Exception:
            self._apply_ellipse_mask()

    @staticmethod
    def _has_opaque_body(img: QImage) -> bool:
        """粗采样：不透明像素占比过低说明帧还没解码好，不能用。"""
        w, h = img.width(), img.height()
        if w == 0 or h == 0:
            return False
        img = img.convertToFormat(QImage.Format_ARGB32)
        opaque = 0
        total = 0
        for y in range(0, h, 8):
            for x in range(0, w, 8):
                total += 1
                if img.pixel(x, y) & 0xFF000000:
                    opaque += 1
        return total > 0 and opaque / total > 0.10

    def _apply_ellipse_mask(self):
        r = self.rect()
        self.setMask(QRegion(r.adjusted(
            int(r.width() * 0.08), int(r.height() * 0.05),
            -int(r.width() * 0.08), -int(r.height() * 0.08)), QRegion.Ellipse))

    def set_mouse_transparent(self, enable: bool):
        self.cfg.setdefault("pet", {})["mouse_transparent"] = enable
        if enable:
            self._on_state_changed(self.engine.state)
        else:
            self.clearMask()

    # ---------------------------------------------------------- 交互
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.engine.notify_input()
            e.accept()
        elif e.button() == Qt.RightButton:
            self._show_menu(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            self.bubble._reposition()
            e.accept()

    def mouseReleaseEvent(self, e):
        if self._drag_offset is not None:
            self._drag_offset = None
            self.save_position()
        e.accept()

    def mouseDoubleClickEvent(self, e):
        self.double_clicked.emit()
        self.request_panel.emit()
        e.accept()

    def enterEvent(self, e):
        # 鼠标划过不再触发开心动画（避免看起来"瞎跳"），只算一次交互
        self.engine.notify_input()
        super().enterEvent(e)

    # ---------------------------------------------------------- 菜单
    def _show_menu(self, pos: QPoint):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#ffffff;border:1px solid #d8e2f0;border-radius:8px;"
            "padding:6px;color:#1e2733;}"
            "QMenu::item{padding:7px 26px 7px 20px;border-radius:6px;}"
            "QMenu::item:selected{background:#eaf2ff;}")

        menu.addAction("📊 数据面板").triggered.connect(self.request_panel.emit)
        menu.addAction("💬 和我聊天").triggered.connect(
            lambda: self._open_chat())
        menu.addAction("⚙️ 设置（API Key）").triggered.connect(self.request_settings.emit)
        menu.addSeparator()

        sub = menu.addMenu("🎭 切换人格")
        cur = self.cfg["personality"]["active"]
        group = []
        for key, val in self.cfg["personality"]["presets"].items():
            act = QAction(f"{val.get('name', key)}{'  ✓' if key == cur else ''}", self)
            act.setCheckable(True)
            act.setChecked(key == cur)
            act.triggered.connect(lambda _=False, k=key: self.personality_changed.emit(k))
            sub.addAction(act)
            group.append(act)

        menu.addAction("🍅 番茄钟 开始/暂停").triggered.connect(self.pomodoro_toggled.emit)
        passthrough = QAction("🖱️ 鼠标穿透（空白区域）", self)
        passthrough.setCheckable(True)
        passthrough.setChecked(bool(self.cfg["pet"].get("mouse_transparent", True)))
        passthrough.triggered.connect(lambda c: self.set_mouse_transparent(c))
        menu.addAction(passthrough)
        menu.addSeparator()
        menu.addAction("❌ 退出").triggered.connect(self.request_quit.emit)
        menu.exec(pos)

    # ---------------------------------------------------------- 聊天框
    def _open_chat(self):
        if not hasattr(self, "_chat") or self._chat is None:
            self._chat = ChatInputWindow(self.cfg, None)
            self._chat.submitted.connect(self.chat_submitted.emit)
        self._chat.popup(self.geometry().topLeft())

    def open_chat(self):
        self._open_chat()

    # ---------------------------------------------------------- 对外
    def say(self, text: str, auto_hide_ms: int | None = None):
        self.bubble.set_text(text, auto_hide_ms)

    def stream_begin(self):
        self.bubble.clear()
        self.engine.set_state("speaking")

    def stream_delta(self, delta: str):
        self.bubble.append_text(delta)

    def stream_end(self):
        self.engine.set_base_state("idle")
        self.bubble._hide_timer.start(
            int(self.cfg.get("ui", {}).get("bubble_duration_ms", 9000)))

    def notify(self, title: str, body: str, state: str = "remind", ms: int = 6000):
        self.engine.set_state(state, ms)
        self.say(body, ms)

    def status_text(self) -> str:
        return STATE_LABEL.get(self.engine.state, self.engine.state)


# ---------------------------------------------------------------- 托盘
def build_tray(pet: PetWindow, on_quit) -> QSystemTrayIcon:
    icon_path = ICON_DIR / "tray.png"
    icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
    tray = QSystemTrayIcon(icon, pet)
    tray.setToolTip("桌面桌宠 Agent")

    menu = QMenu()
    menu.setStyleSheet(
        "QMenu{background:#ffffff;border:1px solid #d8e2f0;border-radius:8px;padding:6px;}"
        "QMenu::item{padding:7px 24px;border-radius:6px;color:#1e2733;}"
        "QMenu::item:selected{background:#eaf2ff;}")
    menu.addAction("📊 数据面板").triggered.connect(pet.request_panel.emit)
    menu.addAction("💬 聊天").triggered.connect(pet.open_chat)
    menu.addAction("⚙️ 设置（API Key）").triggered.connect(pet.request_settings.emit)
    menu.addAction("🍅 番茄钟").triggered.connect(pet.pomodoro_toggled.emit)
    menu.addSeparator()
    menu.addAction("❌ 退出").triggered.connect(on_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: pet.open_chat() if reason == QSystemTrayIcon.Trigger else None)
    return tray
