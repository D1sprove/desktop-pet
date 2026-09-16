"""Task 9：课表导入与课前提醒（+ 通用提醒接口）。

- 从 data/courses.json 导入课表 -> SQLite
- 每分钟检查下节课，提前 remind_before_minutes 触发提醒（气泡 + 系统通知）
- 通用提醒：一次性 / 每日两种，持久化到 reminders 表
- APScheduler 在后台线程跑，触发后通过 Qt 信号回到主线程（线程安全）
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Dict, List

from PySide6.QtCore import QObject, Signal

from app.utils import COURSES_PATH, db_tx, get_db

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------- 工具
def _hhmm_to_min(s: str) -> int:
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return -1


def _parse_weeks(spec: str) -> set[int]:
    """'1-16' / '1,3,5' / '' -> 周次集合（空表示不限制）。"""
    spec = (spec or "").strip()
    if not spec:
        return set()
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                out.update(range(int(a), int(b) + 1))
            except Exception:
                pass
        elif part.isdigit():
            out.add(int(part))
    return out


# ---------------------------------------------------------------- 课表
class CourseStore:
    def __init__(self):
        self.term_start: str | None = None

    def load_from_json(self, path=COURSES_PATH) -> int:
        if not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        items = data.get("courses", data if isinstance(data, list) else [])
        self.term_start = data.get("term_start") if isinstance(data, dict) else None
        n = 0
        with db_tx() as db:
            db.execute("DELETE FROM courses")
            for c in items:
                db.execute(
                    "INSERT INTO courses(name, teacher, location, weekday, start_time,"
                    " end_time, weeks) VALUES(?,?,?,?,?,?,?)",
                    (c.get("name", ""), c.get("teacher", ""), c.get("location", ""),
                     int(c.get("weekday", 1)), c.get("start_time", "08:00"),
                     c.get("end_time", "09:40"), str(c.get("weeks", ""))))
                n += 1
        return n

    def sync_to_db(self, courses: List[Dict]) -> int:
        n = 0
        with db_tx() as db:
            db.execute("DELETE FROM courses")
            for c in courses:
                db.execute(
                    "INSERT INTO courses(name, teacher, location, weekday, start_time,"
                    " end_time, weeks) VALUES(?,?,?,?,?,?,?)",
                    (c.get("name", ""), c.get("teacher", ""), c.get("location", ""),
                     int(c.get("weekday", 1)), c.get("start_time", "08:00"),
                     c.get("end_time", "09:40"), str(c.get("weeks", ""))))
                n += 1
        return n

    def all(self) -> List[Dict]:
        return [dict(r) for r in get_db().execute(
            "SELECT * FROM courses ORDER BY weekday, start_time").fetchall()]

    def current_week(self) -> int | None:
        if not self.term_start:
            return None
        try:
            d0 = datetime.strptime(self.term_start, "%Y-%m-%d")
            return (datetime.now() - d0).days // 7 + 1
        except Exception:
            return None

    def courses_of(self, weekday: int) -> List[Dict]:
        week = self.current_week()
        out = []
        for c in self.all():
            if int(c["weekday"]) != weekday:
                continue
            ws = _parse_weeks(c["weeks"])
            if ws and week and week not in ws:
                continue
            out.append(c)
        return sorted(out, key=lambda x: _hhmm_to_min(x["start_time"]))

    def today_courses(self) -> List[Dict]:
        # Python Monday=0 -> 1..7
        return self.courses_of(datetime.now().weekday() + 1)

    def next_course(self, now: datetime | None = None) -> Dict | None:
        now = now or datetime.now()
        cur = now.hour * 60 + now.minute
        for c in self.today_courses():
            if _hhmm_to_min(c["start_time"]) >= cur:
                return c
        return None

    def current_course(self, now: datetime | None = None) -> Dict | None:
        now = now or datetime.now()
        cur = now.hour * 60 + now.minute
        for c in self.today_courses():
            if _hhmm_to_min(c["start_time"]) <= cur <= _hhmm_to_min(c["end_time"]):
                return c
        return None


# ---------------------------------------------------------------- 调度
class ScheduleManager(QObject):
    """课表检查 + 通用提醒。"""

    notify = Signal(str, str, str)        # title, body, 动画状态
    courses_reloaded = Signal(int)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.store = CourseStore()
        self._scheduler = None
        self._fired: set[str] = set()     # 当天已提醒过的课程
        self._last_day = ""

    # ---------------------------------------------------------- 启动
    def start(self):
        n = self.store.load_from_json()
        if n:
            self.courses_reloaded.emit(n)
        if not self.cfg.get("schedule", {}).get("enabled", True):
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except Exception:
            return
        self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self._scheduler.add_job(
            self._check_courses, "interval",
            seconds=max(20, int(self.cfg.get("schedule", {})
                                .get("check_interval_seconds", 60))),
            id="course_check", replace_existing=True, max_instances=1)
        self._scheduler.add_job(self._check_reminders, "interval", seconds=30,
                                id="reminder_check", replace_existing=True,
                                max_instances=1)
        self._scheduler.start()

    def shutdown(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass

    # ---------------------------------------------------------- 课表检查
    def _check_courses(self):
        try:
            now = datetime.now()
            day = now.strftime("%Y-%m-%d")
            if day != self._last_day:
                self._last_day = day
                self._fired.clear()

            before = int(self.cfg.get("schedule", {}).get("remind_before_minutes", 15))
            nxt = self.store.next_course(now)
            if not nxt:
                return
            start = _hhmm_to_min(nxt["start_time"])
            cur = now.hour * 60 + now.minute
            delta = start - cur
            key = f"{day}-{nxt['name']}-{nxt['start_time']}"
            if 0 < delta <= before and key not in self._fired:
                self._fired.add(key)
                left = "马上" if delta <= 1 else f"{delta} 分钟后"
                loc = nxt.get("location") or "地点待定"
                self.notify.emit(
                    "上课提醒",
                    f"{left}「{nxt['name']}」在 {loc} 开始啦，别迟到～",
                    "remind")
        except Exception:
            pass

    # ---------------------------------------------------------- 通用提醒
    def add_daily(self, hhmm: str, title: str, body: str = "") -> int:
        with db_tx() as db:
            cur = db.execute(
                "INSERT INTO reminders(title, body, repeat, hhmm, enabled)"
                " VALUES(?,?,'daily',?,1)", (title, body, hhmm))
            return cur.lastrowid

    def add_once(self, dt: datetime, title: str, body: str = "") -> int:
        with db_tx() as db:
            cur = db.execute(
                "INSERT INTO reminders(title, body, trigger_at, repeat, enabled)"
                " VALUES(?,?,?,'once',1)", (title, body, dt.timestamp()))
            return cur.lastrowid

    def add_in(self, minutes: float, title: str, body: str = "") -> int:
        return self.add_once(datetime.now() + timedelta(minutes=minutes), title, body)

    def list_reminders(self) -> List[Dict]:
        return [dict(r) for r in get_db().execute(
            "SELECT * FROM reminders WHERE enabled=1 ORDER BY id").fetchall()]

    def delete_reminder(self, rid: int):
        with db_tx() as db:
            db.execute("DELETE FROM reminders WHERE id=?", (rid,))

    def _check_reminders(self):
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            cur_min = now.hour * 60 + now.minute
            for r in self.list_reminders():
                if r["repeat"] == "daily" and r["hhmm"]:
                    target = _hhmm_to_min(r["hhmm"])
                    if abs(cur_min - target) <= 1 and r["last_fired"] != today:
                        self._mark(r["id"], today)
                        self.notify.emit(r["title"] or "提醒", r["body"] or "", "remind")
                elif r["repeat"] == "once" and r["trigger_at"]:
                    if now.timestamp() >= float(r["trigger_at"]):
                        self._mark(r["id"], today)
                        self.notify.emit(r["title"] or "提醒", r["body"] or "", "remind")
                        with db_tx() as db:
                            db.execute("UPDATE reminders SET enabled=0 WHERE id=?", (r["id"],))
        except Exception:
            pass

    @staticmethod
    def _mark(rid: int, day: str):
        with db_tx() as db:
            db.execute("UPDATE reminders SET last_fired=? WHERE id=?", (day, rid))

    # ---------------------------------------------------------- 展示
    def today_text(self) -> str:
        cs = self.store.today_courses()
        if not cs:
            return "今天没有课，自由安排～"
        now_min = datetime.now().hour * 60 + datetime.now().minute
        lines = []
        for c in cs:
            st, et = c["start_time"], c["end_time"]
            flag = "▶" if _hhmm_to_min(st) <= now_min <= _hhmm_to_min(et) else "·"
            loc = c.get("location") or ""
            lines.append(f"{flag} {st}-{et} {c['name']} {loc}".strip())
        wk = self.store.current_week()
        head = f"第{wk}周 " if wk else ""
        return head + "\n".join(lines)
