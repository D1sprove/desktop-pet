"""Task 8：数据小面板。

右键宠物 / 托盘 -> 打开。展示：
今日活跃时长、应用占比（QPainter 饼图）、token 用量、最近记忆、今日课表、番茄钟状态，
并提供 CSV / JSON 导出。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QTabWidget,
                               QTextEdit, QVBoxLayout, QWidget)

from app.collector import app_top, category_stats, export_records, today_stats
from app.memory import MemoryStore
from app.utils import fmt_duration, today_token_usage

CARD_CSS = """
QWidget#card{background:#ffffff;border:1px solid #e3ebf6;border-radius:12px;}
QLabel#title{color:#5b6b82;font-size:12px;}
QLabel#value{color:#1e2733;font-size:22px;font-weight:600;}
QLabel#sub{color:#8a97ab;font-size:11px;}
"""

CAT_COLOR = {
    "browser": "#7FB2FF", "ide": "#B08CFF", "game": "#FF8FA3",
    "office": "#FFC46B", "im": "#6FD39B", "other": "#C3CEDD",
}
CAT_NAME = {
    "browser": "浏览器", "ide": "开发工具", "game": "游戏",
    "office": "办公", "im": "聊天", "other": "其他",
}


class PieChart(QWidget):
    """极简饼图（QPainter 手绘）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = []
        self.setMinimumHeight(180)

    def set_data(self, data):
        self.data = data or []
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        p.setPen(Qt.NoPen)
        size = min(rect.width(), rect.height()) - 20
        cx, cy = rect.center().x(), rect.center().y()
        r = size / 2
        pie_rect = QRectF(cx - r, cy - r, r * 2, r * 2)

        if not self.data:
            p.setPen(QColor("#8a97ab"))
            p.drawText(rect, Qt.AlignCenter, "暂无数据")
            p.end()
            return

        start = 90 * 16
        for d in self.data:
            angle = int(round(d["ratio"] * 360 * 16))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(CAT_COLOR.get(d["category"], "#C3CEDD")))
            p.drawPie(pie_rect, start, -angle)
            start -= angle

        # 中心留白做环形
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(pie_rect.adjusted(r * 0.52, r * 0.52, -r * 0.52, -r * 0.52))
        p.setPen(QColor("#1e2733"))
        f = QFont("Microsoft YaHei", 11, QFont.Bold)
        p.setFont(f)
        top = max(self.data, key=lambda x: x["ratio"])
        p.drawText(pie_rect, Qt.AlignCenter,
                   f"{CAT_NAME.get(top['category'], top['category'])}\n"
                   f"{top['ratio']*100:.0f}%")
        p.end()


class DataPanel(QWidget):
    """数据小面板主窗口。"""

    def __init__(self, cfg, pomodoro, schedule, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.pomodoro = pomodoro
        self.schedule = schedule
        self.store = MemoryStore()

        self.setWindowTitle("桌宠 · 数据面板")
        self.resize(520, 620)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setStyleSheet(
            "QWidget{background:#f4f7fc;color:#1e2733;font-family:'Microsoft YaHei';}"
            "QTabWidget::pane{border:none;background:#f4f7fc;}"
            "QTabBar::tab{padding:8px 18px;background:#e7eefb;border-radius:8px;"
            "margin-right:6px;color:#43536b;}"
            "QTabBar::tab:selected{background:#ffffff;color:#1e2733;font-weight:600;}"
            "QTableWidget{background:#ffffff;border:1px solid #e3ebf6;border-radius:10px;"
            "gridline-color:#eef3fa;}"
            "QHeaderView::section{background:#f0f4fb;padding:6px;border:none;"
            "color:#5b6b82;}"
            "QTextEdit{background:#ffffff;border:1px solid #e3ebf6;border-radius:10px;"
            "padding:8px;}"
            + CARD_CSS)

        tabs = QTabWidget(self)
        tabs.addTab(self._build_overview(), "概览")
        tabs.addTab(self._build_memory(), "记忆")
        tabs.addTab(self._build_courses(), "课表")
        tabs.addTab(self._build_pomodoro(), "番茄钟/提醒")
        tabs.addTab(self._build_export(), "导出")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.addWidget(tabs)

        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    # ---------------------------------------------------------- 组件
    def _card(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        w.setObjectName("card")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 12, 14, 12)
        t = QLabel(title)
        t.setObjectName("title")
        lay.addWidget(t)
        return w, lay

    def _build_overview(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 10, 6, 6)
        lay.setSpacing(12)

        row = QHBoxLayout()
        c1, l1 = self._card("今日活跃时长")
        self.lbl_active = QLabel("--")
        self.lbl_active.setObjectName("value")
        self.lbl_active_sub = QLabel("--")
        self.lbl_active_sub.setObjectName("sub")
        l1.addWidget(self.lbl_active)
        l1.addWidget(self.lbl_active_sub)

        c2, l2 = self._card("今日 Token 用量")
        self.lbl_token = QLabel("--")
        self.lbl_token.setObjectName("value")
        self.lbl_token_sub = QLabel("--")
        self.lbl_token_sub.setObjectName("sub")
        l2.addWidget(self.lbl_token)
        l2.addWidget(self.lbl_token_sub)

        row.addWidget(c1)
        row.addWidget(c2)
        lay.addLayout(row)

        c3, l3 = self._card("应用占比（今日活跃）")
        body = QHBoxLayout()
        self.pie = PieChart()
        self.pie_legend = QLabel("--")
        self.pie_legend.setWordWrap(True)
        self.pie_legend.setStyleSheet("color:#5b6b82;font-size:12px;")
        body.addWidget(self.pie, 3)
        body.addWidget(self.pie_legend, 2)
        l3.addLayout(body)
        lay.addWidget(c3)

        c4, l4 = self._card("当前状态")
        self.lbl_status = QLabel("--")
        self.lbl_status.setStyleSheet("color:#5b6b82;font-size:12px;")
        l4.addWidget(self.lbl_status)
        btn_set = QPushButton("⚙️ API 设置（Key / 模型）")
        btn_set.setStyleSheet(
            "QPushButton{background:#ffffff;border:1px solid #c9d8ee;border-radius:10px;"
            "padding:8px 14px;color:#1e2733;}"
            "QPushButton:hover{background:#eef4ff;}")
        btn_set.clicked.connect(lambda: self.settings_cb and self.settings_cb())
        l4.addWidget(btn_set)
        lay.addWidget(c4)
        self.settings_cb = None

        lay.addStretch(1)
        return w

    def _build_memory(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 10, 6, 6)
        self.mem_text = QTextEdit()
        self.mem_text.setReadOnly(True)
        lay.addWidget(self.mem_text)

        row = QHBoxLayout()
        b1 = QPushButton("立即抽取偏好")
        b1.clicked.connect(self.on_extract)
        b2 = QPushButton("刷新")
        b2.clicked.connect(self.refresh)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        lay.addLayout(row)
        self.extract_cb = None
        return w

    def _build_courses(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 10, 6, 6)
        self.course_text = QTextEdit()
        self.course_text.setReadOnly(True)
        lay.addWidget(self.course_text)
        row = QHBoxLayout()
        b1 = QPushButton("从 courses.json 重新导入")
        b1.clicked.connect(self.on_reload_courses)
        row.addWidget(b1)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def _build_pomodoro(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 10, 6, 6)
        c, l = self._card("番茄钟")
        self.lbl_pomo = QLabel("--")
        self.lbl_pomo.setObjectName("value")
        self.lbl_pomo_sub = QLabel("--")
        self.lbl_pomo_sub.setObjectName("sub")
        l.addWidget(self.lbl_pomo)
        l.addWidget(self.lbl_pomo_sub)
        lay.addWidget(c)

        btns = QHBoxLayout()
        for txt, fn in (("开始专注", lambda: self.pomodoro.start("work")),
                        ("暂停/继续", self.pomodoro.toggle),
                        ("跳过", self.pomodoro.skip)):
            b = QPushButton(txt)
            b.setStyleSheet(
                "QPushButton{background:#ffffff;border:1px solid #c9d8ee;border-radius:10px;"
                "padding:8px 14px;color:#1e2733;}"
                "QPushButton:hover{background:#eef4ff;}")
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)

        self.pomo_log = QTableWidget(0, 4)
        self.pomo_log.setHorizontalHeaderLabels(["阶段", "开始", "时长", "完成"])
        self.pomo_log.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.pomo_log.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.pomo_log)

        self.rem_text = QTextEdit()
        self.rem_text.setReadOnly(True)
        self.rem_text.setPlaceholderText("暂无提醒")
        lay.addWidget(self.rem_text)
        return w

    def _build_export(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 10, 6, 6)
        tip = QLabel(
            "导出今日使用明细（usage_samples）。\n"
            "CSV 含时间戳、进程、分类、CPU/内存、活跃标记；JSON 为原始结构化数据。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#5b6b82;font-size:12px;")
        lay.addWidget(tip)

        for txt, fn in (("导出 CSV", self.export_csv), ("导出 JSON", self.export_json),
                        ("导出记忆 JSON", self.export_memory)):
            b = QPushButton(txt)
            b.setStyleSheet(
                "QPushButton{background:#ffffff;border:1px solid #c9d8ee;border-radius:10px;"
                "padding:10px 16px;color:#1e2733;}"
                "QPushButton:hover{background:#eef4ff;}")
            b.clicked.connect(fn)
            lay.addWidget(b)
        lay.addStretch(1)
        return w

    # ---------------------------------------------------------- 刷新
    def refresh(self):
        try:
            st = today_stats()
            self.lbl_active.setText(fmt_duration(st["active_seconds"]))
            self.lbl_active_sub.setText(
                f"开机采样 {fmt_duration(st['total_seconds'])} · "
                f"活跃占比 {(st['active_seconds']/st['total_seconds']*100 if st['total_seconds'] else 0):.0f}%")
        except Exception:
            pass
        try:
            tu = today_token_usage()
            self.lbl_token.setText(f"{tu['total']:,}")
            self.lbl_token_sub.setText(
                f"prompt {tu['prompt']:,} / completion {tu['completion']:,}")
        except Exception:
            pass
        try:
            cats = category_stats()
            self.pie.set_data(cats)
            tops = app_top(limit=5)
            lines = [f"● {CAT_NAME.get(c['category'], c['category'])} "
                     f"{c['ratio']*100:.0f}%" for c in cats]
            if tops:
                lines.append("")
                lines.append("Top 应用：")
                lines += [f"  {t['process']} {t['ratio']*100:.0f}%" for t in tops]
            self.pie_legend.setText("\n".join(lines))
        except Exception:
            pass
        try:
            from app.settings import env_status_text
            cs = category_stats()
            self.lbl_status.setText(
                f"人格：{self.cfg['personality']['presets'][self.cfg['personality']['active']]['name']}"
                f" ｜ 模型：{self.cfg['api']['flash_model']}"
                f" ｜ API：{env_status_text()}"
                f"\n采样间隔：{self.cfg['collector']['interval_seconds']}s"
                f" ｜ 已采集分类 {len(cs)} 个")
        except Exception:
            pass
        try:
            items = self.store.recent(20)
            tag = {"preference": "偏好", "fact": "事实", "taboo": "禁忌"}
            self.mem_text.setPlainText(
                "\n".join(f"[{tag.get(i['kind'], i['kind'])}] {i['content']}"
                          for i in items) or "还没有记忆，多聊几句试试～")
        except Exception:
            pass
        try:
            self.course_text.setPlainText(self.schedule.today_text())
        except Exception:
            pass
        try:
            self.lbl_pomo.setText(self.pomodoro.status_text())
            s = self.pomodoro.today_summary()
            self.lbl_pomo_sub.setText(f"今日完成 {s['count']} 个番茄 · 专注 {fmt_duration(s['seconds'])}")
            logs = self.pomodoro.recent_logs(10)
            self.pomo_log.setRowCount(len(logs))
            label = {"work": "专注", "break": "短休", "long_break": "长休"}
            for i, lg in enumerate(logs):
                self.pomo_log.setItem(i, 0, QTableWidgetItem(label.get(lg["kind"], lg["kind"])))
                self.pomo_log.setItem(i, 1, QTableWidgetItem(
                    datetime.fromtimestamp(lg["started_at"]).strftime("%H:%M")))
                self.pomo_log.setItem(i, 2, QTableWidgetItem(fmt_duration(lg["actual_seconds"])))
                self.pomo_log.setItem(i, 3, QTableWidgetItem("是" if lg["completed"] else "否"))
        except Exception:
            pass
        try:
            rs = self.schedule.list_reminders()
            self.rem_text.setPlainText(
                "\n".join(f"{'每天' if r['repeat']=='daily' else '一次'} "
                          f"{r['hhmm'] or datetime.fromtimestamp(r['trigger_at']).strftime('%m-%d %H:%M')}"
                          f"  {r['title']}" for r in rs) or "暂无提醒")
        except Exception:
            pass

    # ---------------------------------------------------------- 动作
    def on_extract(self):
        if self.extract_cb:
            self.extract_cb()

    def on_reload_courses(self):
        n = self.schedule.store.load_from_json()
        QMessageBox.information(self, "课表导入", f"已导入 {n} 门课程")
        self.refresh()

    def _save_path(self, default_name: str, ext: str) -> str:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出", f"{default_name}_{datetime.now():%Y%m%d}.{ext}",
            f"{ext.upper()} 文件 (*.{ext})")
        return path

    def export_csv(self):
        path = self._save_path("usage", "csv")
        if not path:
            return
        rows = export_records()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "day", "process", "category",
                                              "window_title", "cpu_percent",
                                              "mem_percent", "active"])
            w.writeheader()
            for r in rows:
                r = dict(r)
                r["ts"] = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
                w.writerow(r)
        QMessageBox.information(self, "导出成功", f"{len(rows)} 条 -> {path}")

    def export_json(self):
        path = self._save_path("usage", "json")
        if not path:
            return
        rows = export_records()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, "导出成功", f"{len(rows)} 条 -> {path}")

    def export_memory(self):
        path = self._save_path("memories", "json")
        if not path:
            return
        data = self.store.all()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, "导出成功", f"{len(data)} 条 -> {path}")

    # ---------------------------------------------------------- 生命周期
    def showEvent(self, e):
        self.refresh()
        self._timer.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)
