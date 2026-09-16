"""API 接入设置窗口（API Key / Base URL / 模型 / 人格）。

入口：宠物右键菜单「⚙️ 设置」或托盘菜单。
保存规则：Key 写入项目根目录 .env（DEEPSEEK_API_KEY=...），
其余写入 config.yaml；config.yaml 里的 api_key 留空时自动用 .env。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QProgressBar,
                               QPushButton, QVBoxLayout)

from app.chat import DeepSeekClient
from app.utils import ROOT_DIR, load_config, save_config

ENV_FILE = ROOT_DIR / ".env"


class _TestWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg

    def run(self):
        try:
            client = DeepSeekClient(self.cfg)
            text = client.chat_sync(
                [{"role": "user", "content": "只回复两个字：收到"}],
                model_role="flash", temperature=0.1, max_tokens=16, stream=False)
            self.done.emit(True, (text or "").strip()[:40] or "连接成功")
        except Exception as e:
            self.done.emit(False, str(e)[:200])


class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("桌宠设置 · API 接入")
        self.setMinimumWidth(460)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setStyleSheet(
            "QDialog{background:#f4f7fc;}"
            "QLabel{color:#1e2733;}"
            "QLineEdit,QComboBox{padding:8px;border:1px solid #c9d8ee;"
            "border-radius:8px;background:#ffffff;color:#1e2733;}"
            "QPushButton{padding:8px 16px;border-radius:8px;"
            "border:1px solid #c9d8ee;background:#ffffff;color:#1e2733;}"
            "QPushButton:hover{background:#eef4ff;}"
            "QPushButton#primary{background:#7FB2FF;color:#ffffff;border:none;}")

        api = cfg["api"]
        self.edit_key = QLineEdit(api.get("api_key", ""))
        self.edit_key.setEchoMode(QLineEdit.Password)
        self.edit_key.setPlaceholderText("sk-xxxxxxxxxxxxxxxx（填入后自动写入 .env）")

        self.edit_url = QLineEdit(api.get("base_url", "https://api.deepseek.com"))
        self.edit_flash = QLineEdit(api.get("flash_model", "deepseek-chat"))
        self.edit_pro = QLineEdit(api.get("pro_model", "deepseek-chat"))

        self.combo_persona = QComboBox()
        for k, v in cfg["personality"]["presets"].items():
            self.combo_persona.addItem(v.get("name", k), k)
        self.combo_persona.setCurrentIndex(
            max(0, self.combo_persona.findData(cfg["personality"]["active"])))

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("API Key", self.edit_key)
        form.addRow("接口地址", self.edit_url)
        form.addRow("闲聊模型（flash）", self.edit_flash)
        form.addRow("总结模型（pro）", self.edit_pro)
        form.addRow("人格", self.combo_persona)

        self.btn_test = QPushButton("测试连接")
        self.btn_test.clicked.connect(self.test_connection)
        self.btn_save = QPushButton("保存并应用")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self.save)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.tip = QLabel("Key 保存在项目根目录 .env，聊天记录等数据都在本地 data/pet.db。")
        self.tip.setWordWrap(True)
        self.tip.setStyleSheet("color:#8a97ab;font-size:11px;")

        row = QHBoxLayout()
        row.addWidget(self.btn_test)
        row.addStretch(1)
        row.addWidget(btn_cancel)
        row.addWidget(self.btn_save)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        lay.addLayout(form)
        lay.addWidget(self.progress)
        lay.addWidget(self.tip)
        lay.addLayout(row)

        self._worker = None

    # ---------------------------------------------------------- 测试
    def test_connection(self):
        key = self.edit_key.text().strip()
        if not key:
            QMessageBox.warning(self, "缺少 Key", "请先填入 API Key 再测试。")
            return
        cfg = self._build_cfg()
        self.btn_test.setEnabled(False)
        self.progress.show()
        self._worker = _TestWorker(cfg, self)
        self._worker.done.connect(self._on_tested)
        self._worker.start()

    def _on_tested(self, ok: bool, msg: str):
        self.btn_test.setEnabled(True)
        self.progress.hide()
        if ok:
            QMessageBox.information(self, "连接成功",
                                    f"DeepSeek 返回：{msg}\n\n点击「保存并应用」生效。")
        else:
            QMessageBox.critical(self, "连接失败", msg)

    # ---------------------------------------------------------- 保存
    def _build_cfg(self) -> dict:
        import copy
        cfg = copy.deepcopy(self.cfg)
        cfg["api"]["api_key"] = self.edit_key.text().strip()
        cfg["api"]["base_url"] = self.edit_url.text().strip() or "https://api.deepseek.com"
        cfg["api"]["flash_model"] = self.edit_flash.text().strip() or "deepseek-chat"
        cfg["api"]["pro_model"] = self.edit_pro.text().strip() or "deepseek-chat"
        cfg["personality"]["active"] = self.combo_persona.currentData()
        return cfg

    def save(self):
        cfg = self._build_cfg()
        key = cfg["api"]["api_key"]

        # 1) Key 写 .env
        try:
            write_env_key(key)
        except Exception as e:
            QMessageBox.warning(self, "写入 .env 失败", str(e))

        # 2) 其余写 config.yaml（api_key 留空，优先读 .env）
        self.cfg["api"]["api_key"] = ""
        self.cfg["api"]["base_url"] = cfg["api"]["base_url"]
        self.cfg["api"]["flash_model"] = cfg["api"]["flash_model"]
        self.cfg["api"]["pro_model"] = cfg["api"]["pro_model"]
        self.cfg["personality"]["active"] = cfg["personality"]["active"]
        save_config(self.cfg)

        # 3) 重新加载环境变量，让当前进程立刻生效
        try:
            from dotenv import load_dotenv
            load_dotenv(ENV_FILE, override=True)
            import os
            if key:
                os.environ["DEEPSEEK_API_KEY"] = key
        except Exception:
            pass
        fresh = load_config()
        if not fresh["api"]["api_key"] and key:
            fresh["api"]["api_key"] = key
        self.saved_cfg = fresh
        self.accept()

    @staticmethod
    def has_key() -> bool:
        return bool(load_config()["api"].get("api_key"))


def env_status_text() -> str:
    """给面板/气泡用的 Key 状态说明。"""
    cfg = load_config()
    if cfg["api"].get("api_key"):
        return f"已配置 Key（{cfg['api']['flash_model']}）"
    return "未配置 Key：右键宠物 → ⚙️ 设置"


def write_env_key(key: str) -> Path:
    """把 Key 写入 .env（保留其他变量），并刷新当前进程环境变量。"""
    import os
    lines = []
    if ENV_FILE.exists():
        lines = [l for l in ENV_FILE.read_text(encoding="utf-8").splitlines()
                 if not l.startswith("DEEPSEEK_API_KEY")]
    if key:
        lines.append(f"DEEPSEEK_API_KEY={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["DEEPSEEK_API_KEY"] = key
    return ENV_FILE
