"""Task 10（上）：番茄钟。

工作 / 短休 / 长休循环，状态同步给宠物动画，并记录番茄钟日志。
"""
from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from app.utils import db_tx

PHASE_LABEL = {"idle": "未开始", "work": "专注中", "break": "短休息",
               "long_break": "长休息"}
PHASE_STATE = {"work": "working", "break": "break", "long_break": "break",
               "idle": "idle"}


class Pomodoro(QObject):
    tick = Signal(int, str)                 # 剩余秒数, 阶段
    phase_changed = Signal(str, int)        # 阶段, 计划秒数
    finished = Signal(str, int)             # 完成的阶段, 实际时长秒
    started = Signal(str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.phase = "idle"
        self.remaining = 0
        self.planned = 0
        self.running = False
        self.cycle = 0                      # 已完成的专注轮数
        self._started_at = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_tick)

    # ---------------------------------------------------------- 配置
    def _minutes(self, key: str, default: int) -> int:
        return int(self.cfg.get("pomodoro", {}).get(key, default))

    def durations(self) -> dict:
        return {"work": self._minutes("work_minutes", 25),
                "break": self._minutes("break_minutes", 5),
                "long_break": self._minutes("long_break_minutes", 15)}

    # ---------------------------------------------------------- 控制
    def start(self, phase: str = "work"):
        if self.running:
            return
        mins = self.durations().get(phase, 25)
        self.phase = phase
        self.planned = mins * 60
        self.remaining = self.planned
        self.running = True
        self._started_at = time.time()
        self._timer.start()
        self.started.emit(phase)
        self.phase_changed.emit(phase, self.planned)
        self.tick.emit(self.remaining, phase)

    def pause(self):
        self.running = False
        self._timer.stop()

    def resume(self):
        if self.phase == "idle" or self.remaining <= 0:
            self.start("work")
            return
        self.running = True
        self._timer.start()

    def toggle(self):
        if self.running:
            self.pause()
        else:
            self.resume()

    def stop(self, completed: bool = True):
        if self.phase == "idle":
            return
        actual = int(self.planned - self.remaining) if completed else int(
            time.time() - self._started_at)
        self.pause()
        phase = self.phase
        self._log(phase, self.planned, max(0, actual), completed)
        self.phase = "idle"
        self.remaining = 0
        self.phase_changed.emit("idle", 0)
        return phase

    def skip(self):
        """跳过当前阶段，直接进入下一阶段。"""
        if self.phase == "idle":
            self.start("work")
            return
        self.stop(completed=False)
        self._next()

    # ---------------------------------------------------------- 内部
    def _on_tick(self):
        self.remaining -= 1
        if self.remaining > 0:
            self.tick.emit(self.remaining, self.phase)
            return
        self._complete()

    def _complete(self):
        phase = self.phase
        self.pause()
        self._log(phase, self.planned, self.planned, True)
        self.finished.emit(phase, self.planned)
        self._next()

    def _next(self):
        if self.phase == "work":
            self.cycle += 1
            n = int(self.cfg.get("pomodoro", {}).get("cycles_before_long_break", 4))
            nxt = "long_break" if self.cycle % max(1, n) == 0 else "break"
        else:
            nxt = "work"
        if self.cfg.get("pomodoro", {}).get("auto_start_break", True) or nxt == "work":
            if self.cfg.get("pomodoro", {}).get("auto_start_break", True):
                self.start(nxt)
            else:
                self.phase, self.remaining = "idle", 0
                self.phase_changed.emit("idle", 0)
        else:
            self.phase, self.remaining = "idle", 0
            self.phase_changed.emit("idle", 0)

    def _log(self, phase: str, planned: int, actual: int, completed: bool):
        try:
            with db_tx() as db:
                db.execute(
                    "INSERT INTO pomodoro_logs(started_at, ended_at, kind,"
                    " planned_seconds, actual_seconds, completed) VALUES(?,?,?,?,?,?)",
                    (self._started_at, time.time(), phase, planned, actual,
                     1 if completed else 0))
        except Exception:
            pass

    # ---------------------------------------------------------- 查询
    def status_text(self) -> str:
        if self.phase == "idle":
            return "番茄钟未开始"
        m, s = divmod(max(0, self.remaining), 60)
        flag = "▶" if self.running else "⏸"
        return f"{flag} {PHASE_LABEL.get(self.phase, self.phase)} {m:02d}:{s:02d}"

    def today_summary(self) -> dict:
        from app.utils import get_db, today_str
        import datetime as dt
        start = dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()
        db = get_db()
        row = db.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(actual_seconds),0) s FROM pomodoro_logs"
            " WHERE kind='work' AND completed=1 AND ended_at>=?", (start,)).fetchone()
        return {"count": row["c"], "seconds": row["s"]}

    def recent_logs(self, limit: int = 10) -> list:
        from app.utils import get_db
        rows = get_db().execute(
            "SELECT * FROM pomodoro_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
