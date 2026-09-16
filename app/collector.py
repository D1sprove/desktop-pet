"""Task 7：电脑使用时间采集（psutil + pynput）。

- 每 5~15 秒采样一次：前台进程 / 分类 / CPU / 内存 / 是否活跃
- pynput 监听键鼠，判定真实活跃状态（无操作超过阈值视为空闲）
- 采样先进内存缓冲，攒够 batch_size 再一次性写库（WAL）
"""
from __future__ import annotations

import platform
import sys
import time
from typing import Dict, List

from PySide6.QtCore import QObject, QThread, Signal

from app.utils import db_tx, get_db, today_str

IS_WIN = sys.platform.startswith("win")


# ---------------------------------------------------------------- 前台窗口
def _foreground_info() -> tuple[str, str]:
    """返回 (进程名, 窗口标题)。Windows 用 ctypes，其他平台尽力而为。"""
    if not IS_WIN:
        return "", ""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        if pid.value:
            import psutil
            try:
                name = psutil.Process(pid.value).name()
            except Exception:
                name = ""
        return name, title
    except Exception:
        return "", ""


class Collector(QObject):
    """采集器（运行在子线程，通过信号回传 UI）。"""

    sample_ready = Signal(dict)          # 每次采样的快照
    flushed = Signal(int)                # 批量写库条数

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        cc = cfg.get("collector", {})
        self.interval = max(5, int(cc.get("interval_seconds", 10)))
        self.batch_size = max(1, int(cc.get("batch_size", 12)))
        self.idle_threshold = int(cc.get("idle_threshold_seconds", 120))
        self.categories = {k: [p.lower() for p in v]
                           for k, v in (cc.get("categories", {}) or {}).items()}

        self._buffer: List[Dict] = []
        self._last_input_ts = time.time()
        self._running = False
        self._thread: QThread | None = None
        self._listeners = []
        self._cpu_warm = False

        import psutil
        self.psutil = psutil
        psutil.cpu_percent(interval=None)   # 预热

    # ---------------------------------------------------------- 键鼠活跃度
    def _start_input_listeners(self):
        try:
            from pynput import keyboard, mouse
        except Exception:
            return

        def touch(*_a, **_kw):
            self._last_input_ts = time.time()

        try:
            ml = mouse.Listener(on_move=touch, on_click=touch, on_scroll=touch)
            ml.daemon = True
            ml.start()
            self._listeners.append(ml)
        except Exception:
            pass
        try:
            kl = keyboard.Listener(on_press=touch)
            kl.daemon = True
            kl.start()
            self._listeners.append(kl)
        except Exception:
            pass

    # ---------------------------------------------------------- 分类
    def categorize(self, process: str) -> str:
        p = (process or "").lower()
        if not p:
            return "other"
        for cat, names in self.categories.items():
            if p in names:
                return cat
        return "other"

    # ---------------------------------------------------------- 采样
    def sample(self) -> Dict:
        import psutil
        proc, title = _foreground_info()
        now = time.time()
        idle_sec = now - self._last_input_ts
        active = idle_sec < self.idle_threshold
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
        except Exception:
            cpu, mem = 0.0, 0.0
        return {
            "ts": now,
            "day": today_str(now),
            "process": proc,
            "category": self.categorize(proc),
            "window_title": title[:120],
            "cpu_percent": round(cpu, 1),
            "mem_percent": round(mem, 1),
            "active": 1 if active else 0,
            "idle_seconds": round(idle_sec, 1),
        }

    # ---------------------------------------------------------- 线程
    def start(self):
        if self._running or not self.cfg.get("collector", {}).get("enabled", True):
            return
        self._running = True
        self._start_input_listeners()

        self._thread = QThread()
        self.moveToThread(self._thread)
        self._thread.run = self._loop          # 直接在子线程跑循环
        self._thread.finished.connect(self._flush)
        self._thread.start()

    def _loop(self):
        while self._running:
            try:
                s = self.sample()
                self._buffer.append(s)
                self.sample_ready.emit(s)
                if len(self._buffer) >= self.batch_size:
                    self._flush()
            except Exception:
                pass
            # 分段 sleep，保证 stop() 能及时响应
            for _ in range(self.interval * 4):
                if not self._running:
                    break
                time.sleep(0.25)

    def stop(self):
        self._running = False
        self._flush()
        for l in self._listeners:
            try:
                l.stop()
            except Exception:
                pass
        self._listeners.clear()
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)

    # ---------------------------------------------------------- 写库
    def _flush(self):
        if not self._buffer:
            return
        rows = self._buffer
        self._buffer = []
        try:
            with db_tx() as db:
                db.executemany(
                    "INSERT INTO usage_samples(ts, day, process, category, window_title,"
                    " cpu_percent, mem_percent, active) "
                    "VALUES(:ts,:day,:process,:category,:window_title,"
                    ":cpu_percent,:mem_percent,:active)", rows)
                # 汇总到 daily_usage
                span = self.interval
                for r in rows:
                    db.execute(
                        "INSERT INTO daily_usage(day, active_seconds, total_seconds)"
                        " VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET "
                        "active_seconds=active_seconds+excluded.active_seconds,"
                        "total_seconds=total_seconds+excluded.total_seconds",
                        (r["day"], span if r["active"] else 0, span))
            self.flushed.emit(len(rows))
        except Exception:
            self._buffer.extend(rows)   # 失败放回缓冲，下轮重试

    # ---------------------------------------------------------- 查询（主线程/面板用）
    @property
    def idle_seconds(self) -> float:
        return time.time() - self._last_input_ts

    @property
    def last_sample(self) -> Dict:
        return getattr(self, "_last", {})


# ---------------------------------------------------------------- 统计查询
def today_stats() -> Dict:
    db = get_db()
    day = today_str()
    row = db.execute(
        "SELECT COALESCE(active_seconds,0) a, COALESCE(total_seconds,0) t"
        " FROM daily_usage WHERE day=?", (day,)).fetchone()
    return {"day": day, "active_seconds": row["a"] if row else 0.0,
            "total_seconds": row["t"] if row else 0.0}


def category_stats(day: str | None = None, limit_hours: float = 24) -> List[Dict]:
    """按应用分类统计（仅统计活跃样本）。"""
    db = get_db()
    day = day or today_str()
    rows = db.execute(
        "SELECT category, COUNT(*) c FROM usage_samples "
        "WHERE day=? AND active=1 GROUP BY category", (day,)).fetchall()
    total = sum(r["c"] for r in rows) or 1
    return [{"category": r["category"], "samples": r["c"],
             "ratio": r["c"] / total} for r in rows]


def app_top(day: str | None = None, limit: int = 8) -> List[Dict]:
    db = get_db()
    day = day or today_str()
    rows = db.execute(
        "SELECT process, COUNT(*) c FROM usage_samples WHERE day=? AND active=1"
        " AND process<>'' GROUP BY process ORDER BY c DESC LIMIT ?",
        (day, limit)).fetchall()
    total = sum(r["c"] for r in rows) or 1
    return [{"process": r["process"], "samples": r["c"], "ratio": r["c"] / total}
            for r in rows]


def active_seconds_today() -> float:
    return today_stats()["active_seconds"]


def history_days(days: int = 7) -> List[Dict]:
    db = get_db()
    rows = db.execute(
        "SELECT day, active_seconds, total_seconds FROM daily_usage"
        " ORDER BY day DESC LIMIT ?", (days,)).fetchall()
    return [dict(r) for r in rows][::-1]


def export_records(day: str | None = None) -> List[Dict]:
    db = get_db()
    day = day or today_str()
    rows = db.execute(
        "SELECT ts, day, process, category, window_title, cpu_percent,"
        " mem_percent, active FROM usage_samples WHERE day=? ORDER BY ts",
        (day,)).fetchall()
    return [dict(r) for r in rows]
