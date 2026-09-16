"""离线逻辑测试：不依赖 API Key，覆盖 Task 3/4/5/9/10 的核心逻辑。

运行：python tests/test_offline.py
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.utils import get_db, init_db, load_config  # noqa: E402

_app = QCoreApplication(sys.argv)


class FakeClient:
    """模拟 DeepSeek：非流式返回固定串，流式逐字吐出。"""

    def __init__(self):
        self.calls = []

    def chat_sync(self, messages, model_role="pro", **kw):
        self.calls.append(("sync", model_role, messages))
        joined = "".join(m.get("content", "") for m in messages)
        if "偏好抽取器" in joined:
            return ('```json\n[{"kind":"preference","subject":"饮食",'
                    '"content":"爱喝奶茶"}]\n```')
        return "压缩摘要：用户在做桌宠项目，喜欢简洁回复。"

    def chat_stream(self, messages, model_role="flash", **kw):
        self.calls.append(("stream", model_role, messages))
        for ch in "你好呀，我是桌宠～":
            yield ch


# ---------------------------------------------------------------- 测试
class TestConfig(unittest.TestCase):
    def test_load(self):
        cfg = load_config()
        self.assertIn("flash_model", cfg["api"])
        self.assertIn(cfg["personality"]["active"], cfg["personality"]["presets"])
        self.assertGreaterEqual(cfg["collector"]["interval_seconds"], 5)

    def test_wal(self):
        init_db()
        self.assertEqual(get_db().execute("PRAGMA journal_mode").fetchone()[0], "wal")


class TestChat(unittest.TestCase):
    def setUp(self):
        from app.chat import ChatSession
        self.cfg = load_config()
        self.cfg["context"]["max_tokens"] = 200
        self.cfg["context"]["keep_recent"] = 4
        self.client = FakeClient()
        self.sess = ChatSession(self.cfg, self.client, session_id="test_chat")
        self.sess.clear()

    def test_build_system_contains_memory(self):
        ps = self.cfg["personality"]["presets"][self.cfg["personality"]["active"]]
        s = self.sess.build_system("- [偏好] 喜欢喝奶茶", "当前应用：Code.exe")
        self.assertIn(ps["name"] or "", s)
        self.assertIn("喜欢喝奶茶", s)
        self.assertIn("Code.exe", s)
        self.assertIn(datetime.now().strftime("%Y-%m-%d"), s)

    def test_trim_compresses(self):
        for i in range(30):
            self.sess.messages.append({"role": "user", "content": f"第{i}句话内容内容内容"})
        before = len(self.sess.messages)
        self.assertGreater(self.sess.total_tokens(), self.cfg["context"]["max_tokens"])
        ok = self.sess.trim(self.sess.build_system())
        self.assertTrue(ok)
        self.assertLess(len(self.sess.messages), before)
        self.assertIn("压缩摘要", self.sess.summary)
        # 压缩用的是 pro 模型
        self.assertTrue(any(c[0] == "sync" and c[1] == "pro" for c in self.client.calls))

    def test_stream_worker(self):
        from app.chat import StreamWorker
        w = StreamWorker(self.client, [{"role": "user", "content": "hi"}], "flash")
        got = []
        full = []
        w.delta.connect(got.append)
        w.finished.connect(full.append)
        w.start()
        deadline = time.time() + 5
        while w.isRunning() and time.time() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.02)
        QCoreApplication.processEvents()
        self.assertEqual("".join(got), "你好呀，我是桌宠～")
        self.assertEqual(full[0], "你好呀，我是桌宠～")


class TestMemory(unittest.TestCase):
    def setUp(self):
        from app.memory import MemoryManager, MemoryStore
        self.cfg = load_config()
        self.store = MemoryStore()
        self.mm = MemoryManager(self.cfg, FakeClient())

    def test_add_and_recall(self):
        self.store.add("preference", "喜欢喝冰美式", "饮食", "test")
        hits = self.store.recall("我喜欢喝什么")
        self.assertTrue(any("冰美式" in h["content"] for h in hits),
                        f"召回失败: {hits}")
        for h in self.store.recent(50):
            if h["content"] == "喜欢喝冰美式":
                self.store.delete(h["id"])

    def test_parse_extract_json(self):
        from app.memory import _ExtractWorker
        raw = '```json\n[{"kind":"preference","subject":"饮食","content":"爱喝奶茶"},' \
              '{"kind":"taboo","subject":"话题","content":"不要提前任"}]\n```'
        items = _ExtractWorker._parse(raw)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["kind"], "preference")
        self.assertEqual(items[1]["kind"], "taboo")
        self.assertEqual(_ExtractWorker._parse("没有可抽取的内容"), [])

    def test_extract_flow(self):
        got = []
        self.mm.extracted.connect(lambda n, items: got.append((n, items)))
        self.mm.extract_now("user: 我喜欢喝奶茶\nassistant: 记住啦")
        deadline = time.time() + 5
        while not got and time.time() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.02)
        self.assertTrue(got, "抽取线程未返回结果")
        n, items = got[0]
        self.assertGreater(n, 0)
        self.assertEqual(items[0]["content"], "爱喝奶茶")
        self.store.delete(self.store.recent(1)[0]["id"])

    def test_render(self):
        self.assertEqual(self.store.render([]), "")
        txt = self.store.render([{"kind": "taboo", "content": "别提前任"}])
        self.assertIn("禁忌", txt)


class TestCollector(unittest.TestCase):
    def test_categorize(self):
        from app.collector import Collector
        c = Collector(load_config())
        self.assertEqual(c.categorize("Code.exe"), "ide")
        self.assertEqual(c.categorize("chrome.exe"), "browser")
        self.assertEqual(c.categorize("WeChat.exe"), "im")
        self.assertEqual(c.categorize("someRandom.exe"), "other")

    def test_sample_shape(self):
        from app.collector import Collector
        s = Collector(load_config()).sample()
        for k in ("ts", "day", "process", "category", "cpu_percent",
                  "mem_percent", "active"):
            self.assertIn(k, s)


class TestSchedule(unittest.TestCase):
    def setUp(self):
        from app.schedule_manager import ScheduleManager
        self.cfg = load_config()
        self.sm = ScheduleManager(self.cfg)
        self.sm.store.sync_to_db([
            {"name": "数据挖掘", "location": "教三-301", "weekday": 1,
             "start_time": "08:00", "end_time": "09:40", "weeks": "1-16"},
            {"name": "机器学习", "location": "A202", "weekday": 3,
             "start_time": "10:00", "end_time": "11:40", "weeks": "1-16"},
        ])

    def test_weeks_parse(self):
        from app.schedule_manager import _parse_weeks
        self.assertEqual(len(_parse_weeks("1-16")), 16)
        self.assertEqual(_parse_weeks("1,3,5"), {1, 3, 5})

    def test_next_course(self):
        now = datetime.now().replace(hour=7, minute=30, second=0)
        wd = now.weekday() + 1
        if wd == 1:
            nxt = self.sm.store.next_course(now)
            self.assertEqual(nxt["name"], "数据挖掘")

    def test_reminders(self):
        rid = self.sm.add_daily("07:30", "早读", "背单词")
        self.assertIn(rid, [r["id"] for r in self.sm.list_reminders()])
        rid2 = self.sm.add_in(1, "喝水", "起来喝水")
        self.assertIn(rid2, [r["id"] for r in self.sm.list_reminders()])
        self.sm.delete_reminder(rid)
        self.sm.delete_reminder(rid2)
        self.assertNotIn(rid, [r["id"] for r in self.sm.list_reminders()])


class TestPomodoro(unittest.TestCase):
    def test_cycle(self):
        from app.pomodoro import Pomodoro
        cfg = load_config()
        cfg["pomodoro"]["work_minutes"] = 1
        cfg["pomodoro"]["break_minutes"] = 1
        cfg["pomodoro"]["auto_start_break"] = True
        pm = Pomodoro(cfg)
        done = []
        pm.finished.connect(lambda k, s: done.append(k))
        pm.start("work")
        self.assertEqual(pm.phase, "work")
        self.assertEqual(pm.remaining, 60)
        pm.remaining = 1
        pm._on_tick()
        self.assertEqual(done, ["work"])
        self.assertEqual(pm.phase, "break")
        pm.stop(completed=False)
        self.assertEqual(pm.phase, "idle")
        self.assertIn("番茄钟", pm.status_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
