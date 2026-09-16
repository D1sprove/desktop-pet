"""桌面桌宠 Agent - 程序入口。

启动：python main.py
自检：python main.py --selftest        （无 GUI，检查配置/数据库/各模块）
离屏冒烟：python main.py --smoke 8    （offscreen 运行 N 秒后自动退出）
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from app.chat import ChatManager, DeepSeekClient
from app.collector import Collector, active_seconds_today
from app.memory import MemoryManager
from app.panel import DataPanel
from app.pet_window import PetWindow, build_tray
from app.pomodoro import PHASE_STATE, Pomodoro
from app.schedule_manager import ScheduleManager
from app.utils import (LOG_PATH, fmt_duration, get_db, init_db, load_config,
                       save_config, setup_logging, today_str)

APP_NAME = "DesktopPetAgent"


class PetApp:
    """把各个模块装配起来（UI 线程只做 UI，重活在子线程）。"""

    def __init__(self, cfg: dict, app: QApplication):
        self.cfg = cfg
        self.qapp = app
        self.log = setup_logging(cfg)

        # ---- 业务模块
        self.client = DeepSeekClient(cfg)
        self.memory = MemoryManager(cfg, self.client)
        self.chat = ChatManager(cfg, self.memory)
        self.collector = Collector(cfg)
        self.pomodoro = Pomodoro(cfg)
        self.schedule = ScheduleManager(cfg)

        # ---- UI
        self.pet = PetWindow(cfg)
        self.panel = DataPanel(cfg, self.pomodoro, self.schedule)
        self.panel.extract_cb = self._extract_now
        self.panel.settings_cb = self.show_settings
        self.tray = build_tray(self.pet, self.quit)

        self._last_sample = {}
        self._wire()

    # ---------------------------------------------------------- 信号连线
    def _wire(self):
        p, chat = self.pet, self.chat

        # 对话
        p.chat_submitted.connect(self.on_chat)
        chat.reply_started.connect(self._on_reply_started)
        chat.reply_delta.connect(p.stream_delta)
        chat.reply_finished.connect(self._on_reply_finished)
        chat.reply_failed.connect(self._on_reply_failed)
        chat.compressed.connect(lambda: self.log.info("上下文已压缩（pro 摘要）"))
        p.personality_changed.connect(self.on_personality)

        # 采集
        self.collector.sample_ready.connect(self.on_sample)

        # 番茄钟
        self.pomodoro.phase_changed.connect(self.on_phase)
        self.pomodoro.finished.connect(self.on_pomodoro_done)
        p.pomodoro_toggled.connect(self.pomodoro.toggle)

        # 提醒 / 课表
        self.schedule.notify.connect(self.on_notify)
        self.schedule.courses_reloaded.connect(
            lambda n: self.log.info("导入课表 %s 门", n))

        # 面板 / 托盘 / 设置
        p.request_panel.connect(self.show_panel)
        p.request_settings.connect(self.show_settings)
        p.request_quit.connect(self.quit)
        self.tray.messageClicked.connect(self.show_panel)

        # 定时：上下文刷新 + 状态栏
        self._ctx_timer = QTimer()
        self._ctx_timer.setInterval(60_000)
        self._ctx_timer.timeout.connect(self._refresh_context)
        self._ctx_timer.start()

    # ---------------------------------------------------------- 对话
    def on_chat(self, text: str):
        self.pet.engine.notify_input()
        if not self.client.available:
            self.pet.engine.set_state("speaking")
            self.pet.say(self.chat.offline_reply(text))
            self.pet.engine.set_base_state("idle")
            return
        self.pet.stream_begin()
        self.chat.ask(text)

    def _on_reply_started(self):
        self.pet.engine.set_state("thinking")

    def _on_reply_finished(self, reply: str):
        self.pet.stream_end()
        self.pet.engine.set_state("happy", 1500)
        self.log.info("回复完成，长度 %s", len(reply))

    def _on_reply_failed(self, msg: str):
        self.pet.stream_end()
        self.pet.notify("出错了", msg[:80], "remind", 8000)
        self.log.error("对话失败: %s", msg)

    def on_personality(self, key: str):
        self.chat.set_personality(key)
        save_config(self.cfg)
        name = self.cfg["personality"]["presets"][key]["name"]
        self.pet.engine.set_state("happy", 1500)
        self.pet.say(f"人格已切换为「{name}」～", 4000)
        self.log.info("人格切换 -> %s", key)

    def _extract_now(self):
        self.memory.extract_now(self.chat.session.tail_text(24))
        self.memory.extracted.connect(
            lambda n, items: QTimer.singleShot(1500, self.panel.refresh))

    # ---------------------------------------------------------- 采集
    def on_sample(self, s: dict):
        self._last_sample = s
        self.pet.engine.notify_input() if s["active"] else None

    # ---------------------------------------------------------- 番茄钟
    def on_phase(self, phase: str, seconds: int):
        self.pet.engine.set_base_state(PHASE_STATE.get(phase, "idle"))
        if phase != "idle":
            self.log.info("番茄钟阶段: %s (%ss)", phase, seconds)

    def on_pomodoro_done(self, phase: str, seconds: int):
        if phase == "work":
            self.notify("番茄钟", "专注结束，起来活动一下吧～", "break")
        else:
            self.notify("番茄钟", "休息结束，继续专注！", "working")

    # ---------------------------------------------------------- 提醒
    def on_notify(self, title: str, body: str, state: str = "remind"):
        self.pet.engine.set_state(state, 8000)
        self.pet.say(body, 8000)
        try:
            self.tray.showMessage(title, body, QSystemTrayIcon.Information, 8000)
        except Exception:
            pass
        self.log.info("提醒: %s / %s", title, body)

    def notify(self, title: str, body: str, state: str = "remind"):
        self.on_notify(title, body, state)

    # ---------------------------------------------------------- 面板
    def show_panel(self):
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    # ---------------------------------------------------------- 设置
    def show_settings(self):
        from app.settings import SettingsDialog
        dlg = SettingsDialog(self.cfg, None)
        if not dlg.exec():
            return
        new_cfg = getattr(dlg, "saved_cfg", None) or load_config()
        # 就地更新，保证所有模块引用的同一份 dict 都生效
        self.cfg.clear()
        self.cfg.update(new_cfg)
        self.client.reload()
        self.chat.set_personality(self.cfg["personality"]["active"])
        self.pet.say("API 已配好，来聊两句吧～" if self.client.available
                     else "已保存，但仍未检测到 Key（离线模式）", 6000)
        self.log.info("设置已保存，API 可用：%s", self.client.available)

    # ---------------------------------------------------------- 上下文
    def _refresh_context(self):
        parts = []
        s = self._last_sample
        if s:
            parts.append(
                f"用户当前在前台使用：{s.get('process') or '未知'}"
                f"（分类 {s.get('category')}），CPU {s.get('cpu_percent')}%，"
                f"内存 {s.get('mem_percent')}%")
        if self.pomodoro.phase != "idle":
            parts.append(f"番茄钟状态：{self.pomodoro.status_text()}")
        parts.append(f"今日电脑活跃时长：{fmt_duration(active_seconds_today())}")
        nxt = self.schedule.store.next_course()
        if nxt:
            parts.append(f"下一节课：{nxt['name']} {nxt['start_time']} @ {nxt.get('location','')}")
        self.chat.set_context_extra("\n".join(parts))

    # ---------------------------------------------------------- 资源自测
    def enable_bench(self):
        """进程内统计 CPU / 内存（--bench 模式）。"""
        import psutil, os
        self._ps = psutil.Process(os.getpid())
        self._cpu_samples, self._mem_samples = [], []
        self._ps.cpu_percent(interval=None)
        self._bench_timer = QTimer()
        self._bench_timer.setInterval(2000)
        self._bench_timer.timeout.connect(self._bench_tick)
        self._bench_timer.start()

    def _bench_tick(self):
        self._cpu_samples.append(self._ps.cpu_percent(interval=None))
        self._mem_samples.append(self._ps.memory_info().rss / 1048576)

    def _bench_report(self):
        if not getattr(self, "_cpu_samples", None):
            return
        import psutil
        cpu = sum(self._cpu_samples) / len(self._cpu_samples)
        per_core = cpu / (psutil.cpu_count() or 1)
        peak = max(self._mem_samples)
        avg = sum(self._mem_samples) / len(self._mem_samples)
        print("\n=== 资源占用（进程内实测）===")
        print(f"采样次数  : {len(self._cpu_samples)}")
        print(f"CPU 平均  : {cpu:.2f}% (多核归一化) / {per_core:.2f}% (单核口径)")
        print(f"内存 RSS  : 平均 {avg:.1f} MB / 峰值 {peak:.1f} MB")
        print(f"验收标准  : CPU < 5% -> {'PASS' if per_core < 5 else 'FAIL'}"
              f" | 内存 < 200MB -> {'PASS' if peak < 200 else 'FAIL'}")

    # ---------------------------------------------------------- 生命周期
    def start(self):
        self.pet.show()
        self.tray.show()
        self.collector.start()
        self.schedule.start()
        self._refresh_context()

        greeting = (f"我来啦～现在是 {self.pet.status_text()}。"
                    f"右键我可以看数据面板，托盘图标也能找到我。")
        if not self.client.available:
            greeting += ("\n还没配置 API Key：右键我 →「⚙️ 设置」里填，"
                         "或命令行 python main.py --set-key sk-xxx")
        self.pet.say(greeting, 10000)
        self.log.info("=== 桌宠已启动 ===")

    def quit(self):
        self.log.info("正在退出…")
        try:
            self._bench_report()
        except Exception:
            pass
        try:
            self.pet.save_position()
        except Exception:
            pass
        try:
            self.schedule.shutdown()
        except Exception:
            pass
        try:
            self.collector.stop()
        except Exception:
            pass
        try:
            self.pet.engine.stop()
        except Exception:
            pass
        try:
            self.panel.close()
            self.pet.close()
        except Exception:
            pass
        save_config(self.cfg)
        self.qapp.quit()


# ---------------------------------------------------------------- 自检
def selftest(cfg: dict) -> int:
    log = setup_logging(cfg)
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'OK ' if cond else 'FAIL'}] {name} {extra}")

    print("\n=== 自检 ===")
    init_db()
    db = get_db()
    check("SQLite WAL", db.execute("PRAGMA journal_mode").fetchone()[0] == "wal")

    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    need = {"usage_samples", "daily_usage", "messages", "sessions", "memories",
            "token_usage", "pomodoro_logs", "reminders", "courses"}
    check("数据表齐全", need <= tables, f"缺 {need - tables}" if need - tables else "")

    # 记忆
    from app.memory import MemoryStore
    ms = MemoryStore()
    ms.add("preference", "自检：喜欢喝奶茶", "饮食", "selftest")
    hit = ms.recall("我喜欢喝什么")
    check("记忆写入+关键词召回", any("奶茶" in h["content"] for h in hit))
    ms.delete(ms.recent(1)[0]["id"])

    # 采集
    from app.collector import Collector, category_stats
    c = Collector(cfg)
    s = c.sample()
    check("采集采样", set(s) >= {"ts", "process", "category", "active"},
          f"-> {s.get('process')}/{s.get('category')}")
    check("进程分类", c.categorize("Code.exe") == "ide")

    # 课表
    from app.schedule_manager import CourseStore, ScheduleManager
    st = CourseStore()
    n = st.load_from_json()
    check("课表导入", n >= 0, f"导入 {n} 门")
    sm = ScheduleManager(cfg)
    print("  今日课表：", sm.today_text().replace("\n", " | "))

    # 番茄钟 / 提醒（不启动线程）
    from app.pomodoro import Pomodoro
    pm = Pomodoro(cfg)
    pm.start("work")
    check("番茄钟启动", pm.phase == "work" and pm.remaining == 25 * 60)
    pm.pause()

    # 动画素材
    from app.utils import ANIM_DIR
    gifs = sorted(p.name for p in ANIM_DIR.glob("*.gif"))
    check("动画素材", len(gifs) >= 7, f"{gifs}")

    # 模型 / Key
    check("配置加载", bool(cfg["api"]["flash_model"]))
    print(f"  [{'OK ' if cfg['api']['api_key'] else 'WARN'}] "
          f"API Key {'已配置' if cfg['api']['api_key'] else '未配置（离线模式）'}")
    print(f"  日志：{LOG_PATH}")
    print("=== 自检结果：", "全部通过" if ok else "存在失败项", "===\n")
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    cfg = load_config()
    init_db()

    if "--selftest" in args:
        return selftest(cfg)

    # 命令行配置 Key：python main.py --set-key sk-xxx
    if "--set-key" in args:
        from app.settings import write_env_key
        idx = args.index("--set-key")
        key = args[idx + 1] if len(args) > idx + 1 else input("DeepSeek API Key: ").strip()
        p = write_env_key(key.strip())
        print(f"Key 已写入 {p}")
        cfg = load_config()
        print("当前状态：", "已配置" if cfg["api"]["api_key"] else "仍未生效，请检查文件")
        return 0

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)

    pet_app = PetApp(cfg, app)
    pet_app.start()

    if "--bench" in args:
        idx = args.index("--bench")
        secs = float(args[idx + 1]) if len(args) > idx + 1 else 40.0
        pet_app.enable_bench()
        QTimer.singleShot(int(secs * 1000), pet_app.quit)

    if "--smoke" in args:
        try:
            secs = float(args[args.index("--smoke") + 1])
        except Exception:
            secs = 5.0
        QTimer.singleShot(int(secs * 1000), pet_app.quit)
        rc = app.exec()
        print(f"[smoke] 离屏运行 {secs}s 正常退出，rc={rc}")
        return rc

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
