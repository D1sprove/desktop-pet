"""桌面桌宠 Agent - 配置加载 / 日志 / SQLite(公共部分)."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, Dict

import yaml

# ---------------------------------------------------------------- 路径常量
# app/utils.py  ->  app/  ->  项目根
APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = ROOT_DIR / "data"
ASSETS_DIR = ROOT_DIR / "assets"
ANIM_DIR = ASSETS_DIR / "animations"
ICON_DIR = ASSETS_DIR / "icons"
DB_PATH = DATA_DIR / "pet.db"
LOG_PATH = DATA_DIR / "app.log"
CONFIG_PATH = ROOT_DIR / "config.yaml"
COURSES_PATH = DATA_DIR / "courses.json"

for _d in (DATA_DIR, ASSETS_DIR, ANIM_DIR, ICON_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 默认配置
DEFAULT_CONFIG: Dict[str, Any] = {
    "api": {
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key": "",              # 留空则回落到 .env / 环境变量
        "flash_model": "deepseek-chat",       # 闲聊：快
        "pro_model": "deepseek-chat",         # 摘要/抽取：强（可改 deepseek-reasoner）
        "timeout": 60,
        "temperature": 0.85,
        "max_tokens": 1024,
    },
    "personality": {
        "active": "lively",
        "presets": {
            "lively": {
                "name": "活泼",
                "system": (
                    "你是用户桌面上住着的一只桌宠小助手，性格活泼、元气满满，说话简短有梗。"
                    "你会关心用户今天过得怎么样，偶尔撒娇，但绝不啰嗦。"
                    "回答控制在 3 句话以内，中文优先。"
                ),
            },
            "tsundere": {
                "name": "毒舌",
                "system": (
                    "你是用户桌面上住着的一只桌宠，嘴硬心软、毒舌傲娇。"
                    "你会一边吐槽用户一边把事情办好，吐槽要克制，不人身攻击。"
                    "回答控制在 3 句话以内，中文优先。"
                ),
            },
            "scholar": {
                "name": "学霸",
                "system": (
                    "你是用户桌面上住着的一只桌宠，严谨、理性、像个学霸助教。"
                    "你习惯分点作答、给出依据，必要时提醒用户休息。"
                    "回答控制在 5 句话以内，中文优先。"
                ),
            },
        },
    },
    "pet": {
        "width": 180,
        "height": 200,
        "x": -1, "y": -1,             # -1 表示自动贴右下角
        "opacity": 1.0,
        "always_on_top": True,
        "idle_fps": 3,
        "active_fps": 12,
        "sleep_after_minutes": 30,    # 无操作进入瞌睡
        "mouse_transparent": True,
    },
    "context": {
        "max_tokens": 6000,           # 上下文预算
        "keep_recent": 12,            # 压缩时至少保留的最近轮数
    },
    "memory": {
        "enabled": True,
        "summarize_every_turns": 16,  # 每 N 轮自动做一次偏好抽取
        "recall_limit": 8,
        "max_facts": 300,
    },
    "collector": {
        "enabled": True,
        "interval_seconds": 10,       # 采样间隔（5-15s）
        "batch_size": 12,             # 批量写库阈值
        "idle_threshold_seconds": 120,# 键鼠无操作判定空闲
        "categories": {
            "browser": ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "360chrome.exe"],
            "ide": ["code.exe", "pycharm64.exe", "idea64.exe", "devenv.exe", "clion64.exe",
                    "webstorm64.exe", "notepad++.exe", "cursor.exe", "sublime_text.exe"],
            "game": ["steam.exe", "wegame.exe", "leagueclient.exe", "valorant.exe",
                     "minecraft.exe", "genshin.exe", "yuanshen.exe", "epicgameslauncher.exe"],
            "office": ["winword.exe", "excel.exe", "powerpnt.exe", "wps.exe", "et.exe", "wpp.exe"],
            "im": ["wechat.exe", "weixin.exe", "qq.exe", "dingtalk.exe", "feishu.exe", "tim.exe"],
        },
    },
    "schedule": {
        "enabled": True,
        "check_interval_seconds": 60,
        "remind_before_minutes": 15,
    },
    "pomodoro": {
        "work_minutes": 25,
        "break_minutes": 5,
        "long_break_minutes": 15,
        "cycles_before_long_break": 4,
        "auto_start_break": True,
    },
    "ui": {
        "font_family": "Microsoft YaHei",
        "font_size": 11,
        "bubble_max_chars": 160,
        "bubble_duration_ms": 9000,
    },
    "logging": {
        "level": "INFO",
        "max_bytes": 1048576,
        "backup_count": 3,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并字典，override 覆盖 base。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | str = CONFIG_PATH) -> Dict[str, Any]:
    """读取 config.yaml 并与默认配置合并；同时加载 .env。"""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT_DIR / ".env", override=False)
    except Exception:  # dotenv 缺失不影响运行
        pass

    data: Dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    cfg = _deep_merge(DEFAULT_CONFIG, data)

    # API Key 优先级：config.api_key > 环境变量 > .env
    env_name = cfg["api"].get("api_key_env", "DEEPSEEK_API_KEY")
    if not cfg["api"].get("api_key"):
        cfg["api"]["api_key"] = os.environ.get(env_name, "")
    return cfg


def save_config(cfg: Dict[str, Any], path: Path | str = CONFIG_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------- 日志
_log_ready = False


def setup_logging(cfg: Dict[str, Any] | None = None) -> logging.Logger:
    global _log_ready
    cfg = cfg or {}
    lc = cfg.get("logging", {}) or {}
    level = getattr(logging, str(lc.get("level", "INFO")).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    if not _log_ready:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
        try:
            fh = logging.handlers.RotatingFileHandler(
                LOG_PATH, maxBytes=int(lc.get("max_bytes", 1048576)),
                backupCount=int(lc.get("backup_count", 3)), encoding="utf-8",
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception:
            pass
        _log_ready = True
    root.setLevel(level)
    return logging.getLogger("pet")


# ---------------------------------------------------------------- 数据库
_db_lock = threading.RLock()
_db: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,          -- unix timestamp
    day         TEXT NOT NULL,          -- YYYY-MM-DD
    process     TEXT,
    category    TEXT,
    window_title TEXT,
    cpu_percent REAL,
    mem_percent REAL,
    active      INTEGER DEFAULT 1       -- 1 活跃 0 空闲
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_samples(day);

CREATE TABLE IF NOT EXISTS daily_usage (
    day         TEXT PRIMARY KEY,
    active_seconds REAL DEFAULT 0,
    total_seconds  REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    personality TEXT,
    created_at  REAL,
    updated_at  REAL,
    summary     TEXT                     -- 压缩后的历史摘要
);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,           -- preference / fact / taboo
    subject     TEXT,
    content     TEXT NOT NULL,
    weight      REAL DEFAULT 1.0,
    source      TEXT,
    created_at  REAL,
    updated_at  REAL,
    hit_count   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_kind ON memories(kind);

CREATE TABLE IF NOT EXISTS token_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    model       TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    ts          REAL
);
CREATE INDEX IF NOT EXISTS idx_token_day ON token_usage(day);

CREATE TABLE IF NOT EXISTS pomodoro_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL,
    ended_at    REAL,
    kind        TEXT,                    -- work / break / long_break
    planned_seconds INTEGER,
    actual_seconds  INTEGER,
    completed  INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT,
    body        TEXT,
    trigger_at  REAL,                    -- 一次性
    repeat      TEXT,                    -- daily / once
    hhmm        TEXT,                    -- 每日 HH:MM
    enabled     INTEGER DEFAULT 1,
    last_fired  TEXT
);

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    teacher     TEXT,
    location    TEXT,
    weekday     INTEGER,                 -- 1=周一 ... 7=周日
    start_time  TEXT,                    -- HH:MM
    end_time    TEXT,                    -- HH:MM
    weeks       TEXT                     -- "1-16" 或 "1,3,5"
);
"""


def get_db() -> sqlite3.Connection:
    """全局单例连接（WAL 模式 + 写锁）。"""
    global _db
    with _db_lock:
        if _db is None:
            _db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
            _db.row_factory = sqlite3.Row
            _db.execute("PRAGMA journal_mode=WAL")
            _db.execute("PRAGMA synchronous=NORMAL")
            _db.execute("PRAGMA busy_timeout=15000")
            _db.execute("PRAGMA cache_size=-4000")
        return _db


def init_db() -> None:
    with _db_lock:
        db = get_db()
        db.executescript(SCHEMA)
        db.commit()


from contextlib import contextmanager


@contextmanager
def db_tx():
    """写事务上下文：统一加锁 + 自动提交/回滚。"""
    with _db_lock:
        db = get_db()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


# ---------------------------------------------------------------- 杂项
_CJK_RANGES = (
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3000, 0x303F),
    (0xFF00, 0xFFEF), (0xAC00, 0xD7AF),
)


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return any(a <= o <= b for a, b in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """轻量 token 估算：CJK 约 1 字 1 token，其余约 4 字符 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if _is_cjk(c))
    other = len(text) - cjk
    return int(cjk + other / 4) + 1


def today_str(ts: float | None = None) -> str:
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(ts) if ts else _dt.datetime.now()
    return d.strftime("%Y-%m-%d")


def record_token_usage(model: str, prompt_tokens: int, completion_tokens: int):
    import time
    try:
        with db_tx() as db:
            db.execute(
                "INSERT INTO token_usage(day, model, prompt_tokens, completion_tokens,"
                " total_tokens, ts) VALUES(?,?,?,?,?,?)",
                (today_str(), model, prompt_tokens, completion_tokens,
                 prompt_tokens + completion_tokens, time.time()),
            )
    except Exception:
        pass


def today_token_usage() -> dict:
    try:
        db = get_db()
        row = db.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c,"
            " COALESCE(SUM(total_tokens),0) t FROM token_usage WHERE day=?",
            (today_str(),),
        ).fetchone()
        return {"prompt": row["p"], "completion": row["c"], "total": row["t"]}
    except Exception:
        return {"prompt": 0, "completion": 0, "total": 0}


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, m = divmod(seconds, 3600)
    m, s = divmod(m, 60)
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"
