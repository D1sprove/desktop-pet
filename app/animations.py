"""Task 6：动画状态机。

状态：idle / thinking / speaking / happy / remind / working / break / sleep
- 加载 assets/animations/<state>.gif（QMovie）
- 支持临时状态（带自动过期）+ 基础状态（番茄钟 / 瞌睡 / 空闲）
- 空闲自动降帧（idle_fps），活跃时恢复（active_fps）
- 无操作超过 sleep_after_minutes 切打瞌睡动画
"""
from __future__ import annotations

import time
from typing import Dict

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QMovie, QPainter, QPixmap
from PySide6.QtWidgets import QLabel

from app.utils import ANIM_DIR

STATES = ("idle", "thinking", "speaking", "happy", "remind", "working", "break", "sleep")

# 状态显示名
STATE_LABEL = {
    "idle": "发呆中", "thinking": "思考中", "speaking": "说话中", "happy": "开心",
    "remind": "提醒", "working": "专注中", "break": "休息中", "sleep": "打瞌睡",
}

_FALLBACK_COLOR = {
    "idle": "#7FB2FF", "thinking": "#B08CFF", "speaking": "#6FD39B",
    "happy": "#FFB36F", "remind": "#FF7B7B", "working": "#4FC3F7",
    "break": "#8BE9C0", "sleep": "#8FA0B8",
}


def _fallback_pixmap(state: str, size: int = 160) -> QPixmap:
    """素材缺失时的兜底画面（纯色圆 + 状态名）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(_FALLBACK_COLOR.get(state, "#7FB2FF")))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, size - 8, size - 8)
    p.setPen(QColor("#20304a"))
    f = QFont("Microsoft YaHei", 11)
    p.setFont(f)
    p.drawText(QRect(0, size - 34, size, 24), Qt.AlignCenter, STATE_LABEL.get(state, state))
    p.end()
    return pm


class AnimationEngine(QObject):
    """管理状态 -> 动画的映射与帧率。UI 层只需 set_state / set_base_state。"""

    state_changed = Signal(str)          # 状态名（供外部同步，比如面板显示）
    frame_ready = Signal(QPixmap)        # 当前帧（也可直接拿 label）

    def __init__(self, cfg: dict, label: QLabel, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.label = label
        self._movies: Dict[str, QMovie] = {}
        self._native_fps: Dict[str, float] = {}
        self._state = ""            # 置空，保证首次 apply_state 会真正挂载动画
        self._base_state = "idle"
        self._override: str | None = None
        self._override_until = 0.0
        self._last_input_ts = time.time()
        self._low_fps = False

        self._load_movies()

        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        self.apply_state("idle")

    # ---------------------------------------------------------- 素材
    def _load_movies(self):
        for s in STATES:
            path = ANIM_DIR / f"{s}.gif"
            if not path.exists():
                continue
            mv = QMovie(str(path))
            if not mv.isValid():
                continue
            mv.setCacheMode(QMovie.CacheAll)
            delay = mv.nextFrameDelay() or 100
            self._native_fps[s] = 1000.0 / max(30, delay)
            self._movies[s] = mv
        if not self._movies:
            # 没有任何素材 -> 用定时器驱动兜底重绘
            self._fallback_timer = QTimer(self)
            self._fallback_timer.timeout.connect(self._draw_fallback)
            self._fallback_timer.start(600)

    def _draw_fallback(self):
        self.label.setPixmap(_fallback_pixmap(self._state))

    @property
    def has_assets(self) -> bool:
        return bool(self._movies)

    # ---------------------------------------------------------- 帧率
    def _fps(self) -> int:
        """只在临时状态（说话/思考/提醒等真实互动）用高帧率，
        待机（idle/working/break/sleep）一律用 idle_fps，避免平时太活跃。"""
        pc = self.cfg.get("pet", {})
        return int(pc.get("active_fps", 24) if self._override
                   else pc.get("idle_fps", 6))

    def _apply_speed(self):
        mv = self._movies.get(self._state)
        if not mv:
            return
        native = self._native_fps.get(self._state, 10.0)
        speed = max(10, int(100 * self._fps() / native))
        if abs(mv.speed() - speed) > 5:
            mv.setSpeed(speed)

    def set_low_fps(self, low: bool):
        """兼容旧接口：空闲降帧已并入 _fps() 逻辑，此方法仅保留。"""
        self._apply_speed()

    # ---------------------------------------------------------- 状态
    @property
    def state(self) -> str:
        return self._state

    def notify_input(self):
        """用户有操作（键鼠/对话），重置瞌睡计时（不改变动画速度）。"""
        self._last_input_ts = time.time()

    def set_base_state(self, state: str):
        """基础状态：idle / working / break（由番茄钟等长期因素决定）。"""
        if state not in STATES:
            return
        self._base_state = state

    def set_state(self, state: str, duration_ms: int = 0):
        """临时状态，duration_ms>0 时到期自动回到基础状态。"""
        if state not in STATES:
            return
        if duration_ms > 0:
            self._override = state
            self._override_until = time.time() + duration_ms / 1000.0
        else:
            self._override = None
            self._override_until = 0.0
            self._base_state = state
        self.apply_state(state)
        self._apply_speed()

    # 兼容命名
    def show_state(self, state: str, duration_ms: int = 0):
        self.set_state(state, duration_ms)

    def apply_state(self, state: str, force: bool = False):
        """挂载并播放对应动画。force=True 时即使状态相同也重新挂载。"""
        if not force and state == self._state and self._movies.get(state) is not None:
            return
        self._state = state
        mv = self._movies.get(state)
        if mv is None:
            self._draw_fallback()
        else:
            for other in self._movies.values():
                if other is not mv:
                    other.stop()
            self.label.setMovie(mv)
            self._apply_speed()
            mv.start()
        self.state_changed.emit(state)

    def _on_tick(self):
        # 1) 临时状态到期
        if self._override and time.time() > self._override_until:
            self._override = None
            self._apply_speed()
        # 2) 瞌睡判定
        sleep_after = float(self.cfg.get("pet", {}).get("sleep_after_minutes", 30)) * 60
        idle_sec = time.time() - self._last_input_ts
        sleepy = idle_sec > sleep_after

        target = self._override or self._base_state
        if sleepy and self._override is None:
            target = "sleep"
        if target != self._state:
            self.apply_state(target)

    def stop(self):
        for mv in self._movies.values():
            mv.stop()
        self._tick.stop()
