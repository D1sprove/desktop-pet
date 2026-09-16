"""Task 3 + 4：DeepSeek 流式对话 / 人格 / 上下文管理。

- openai 兼容 SDK 调用 DeepSeek，支持流式与非流式
- 闲聊走 flash 模型；摘要 / 偏好抽取走 pro 模型
- 会话上下文按 token 预算截断，超阈值用 pro 模型做摘要压缩
"""
from __future__ import annotations

import datetime
import time
import uuid
from typing import Dict, Iterator, List

from PySide6.QtCore import QObject, QThread, Signal

from app.utils import (db_tx, estimate_tokens, get_db, record_token_usage,
                       today_str)

COMPRESS_SYSTEM = (
    "你是对话摘要器。请把下面的历史对话压缩成一段不超过 300 字的中文摘要，"
    "保留：用户身份与目标、已讨论的关键结论、未完成的待办、用户表达过的偏好。"
    "直接输出摘要正文，不要任何前缀。"
)


# ---------------------------------------------------------------- 客户端
class DeepSeekClient:
    """openai 兼容封装（DeepSeek）。同步方法在子线程调用，不阻塞 UI。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._client = None
        api = cfg.get("api", {})
        self.api_key = api.get("api_key", "")
        self.base_url = api.get("base_url", "https://api.deepseek.com")
        self.timeout = float(api.get("timeout", 60))
        self.temperature = float(api.get("temperature", 0.85))
        self.max_tokens = int(api.get("max_tokens", 1024))
        self.models = {"flash": api.get("flash_model", "deepseek-chat"),
                       "pro": api.get("pro_model", "deepseek-chat")}

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def reload(self):
        """设置里改了 Key / 模型后，重新读取配置并丢弃旧 SDK 连接。"""
        api = self.cfg.get("api", {})
        self.api_key = api.get("api_key", "")
        self.base_url = api.get("base_url", "https://api.deepseek.com")
        self.timeout = float(api.get("timeout", 60))
        self.temperature = float(api.get("temperature", 0.85))
        self.max_tokens = int(api.get("max_tokens", 1024))
        self.models = {"flash": api.get("flash_model", "deepseek-chat"),
                       "pro": api.get("pro_model", "deepseek-chat")}
        self._client = None

    def _sdk(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                                  timeout=self.timeout)
        return self._client

    def model_of(self, role: str = "flash") -> str:
        return self.models.get(role, self.models["flash"])

    # ---------------------------------------------------------- 非流式
    def chat_sync(self, messages: List[Dict], model_role: str = "pro",
                  temperature: float | None = None,
                  max_tokens: int | None = None, stream: bool = False) -> str:
        if not self.available:
            raise RuntimeError("未配置 DeepSeek API Key（.env 或 config.yaml）")
        model = self.model_of(model_role)
        resp = self._sdk().chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=max_tokens or self.max_tokens,
            stream=False,
        )
        self._record_usage(model, resp)
        return (resp.choices[0].message.content or "").strip()

    # ---------------------------------------------------------- 流式
    def chat_stream(self, messages: List[Dict], model_role: str = "flash",
                    temperature: float | None = None,
                    max_tokens: int | None = None) -> Iterator[str]:
        if not self.available:
            raise RuntimeError("未配置 DeepSeek API Key（.env 或 config.yaml）")
        model = self.model_of(model_role)
        resp = self._sdk().chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=max_tokens or self.max_tokens,
            stream=True,
        )
        usage = None
        for chunk in resp:
            if not getattr(chunk, "choices", None):
                # DeepSeek 在最后一个 chunk 返回 usage
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                continue
            delta = chunk.choices[0].delta
            if delta and getattr(delta, "content", None):
                yield delta.content
        if usage:
            record_token_usage(model, getattr(usage, "prompt_tokens", 0) or 0,
                               getattr(usage, "completion_tokens", 0) or 0)
        else:
            record_token_usage(model, sum(estimate_tokens(m["content"]) for m in messages), 0)

    @staticmethod
    def _record_usage(model: str, resp):
        try:
            u = getattr(resp, "usage", None)
            if u:
                record_token_usage(model, getattr(u, "prompt_tokens", 0) or 0,
                                   getattr(u, "completion_tokens", 0) or 0)
        except Exception:
            pass


# ---------------------------------------------------------------- 会话
class ChatSession:
    """多轮上下文 + token 预算 + 摘要压缩（纯逻辑，无 Qt 依赖，便于测试）。"""

    def __init__(self, cfg: dict, client: DeepSeekClient, session_id: str | None = None):
        self.cfg = cfg
        self.client = client
        self.id = session_id or uuid.uuid4().hex[:12]
        self.messages: List[Dict] = []      # 不含 system（system 每次动态拼）
        self.summary: str = ""
        self._load()

    # ---------------------------------------------------------- 持久化
    def _load(self):
        db = get_db()
        row = db.execute("SELECT * FROM sessions WHERE id=?", (self.id,)).fetchone()
        if row:
            self.summary = row["summary"] or ""
            rows = db.execute(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY id",
                (self.id,)).fetchall()
            self.messages = [{"role": r["role"], "content": r["content"]} for r in rows]
        else:
            with db_tx() as d:
                d.execute("INSERT INTO sessions(id, title, personality, created_at,"
                          " updated_at, summary) VALUES(?,?,?,?,?,?)",
                          (self.id, "新对话", self.cfg["personality"]["active"],
                           time.time(), time.time(), ""))

    def persist(self, role: str, content: str):
        with db_tx() as d:
            d.execute("INSERT INTO messages(session_id, role, content, ts)"
                      " VALUES(?,?,?,?)", (self.id, role, content, time.time()))
            d.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), self.id))

    def clear(self):
        self.messages.clear()
        self.summary = ""
        with db_tx() as d:
            d.execute("DELETE FROM messages WHERE session_id=?", (self.id,))
            d.execute("UPDATE sessions SET summary='' WHERE id=?", (self.id,))

    # ---------------------------------------------------------- 预算
    def total_tokens(self, system: str = "") -> int:
        return estimate_tokens(system) + sum(
            estimate_tokens(m["content"]) + 4 for m in self.messages)

    def build_system(self, memory_text: str = "", extra: str = "") -> str:
        ps = self.cfg["personality"]
        preset = ps["presets"].get(ps["active"], {})
        base = (preset.get("system", "") or "").strip()
        now = datetime.datetime.now()
        week = "一二三四五六日"[now.weekday()]
        parts = [base,
                 f"\n当前时间：{now:%Y-%m-%d} 星期{week} {now:%H:%M}"]
        if self.summary:
            parts.append(f"\n更早对话的摘要：\n{self.summary}")
        if memory_text:
            parts.append("\n" + memory_text)
        if extra:
            parts.append("\n" + extra)
        return "\n".join(p for p in parts if p)

    def trim(self, system: str = "") -> bool:
        """按 token 预算截断；超预算时用 pro 模型压缩旧消息。返回是否压缩过。"""
        budget = int(self.cfg.get("context", {}).get("max_tokens", 6000))
        keep = int(self.cfg.get("context", {}).get("keep_recent", 12))
        compressed = False
        while self.total_tokens(system) > budget and len(self.messages) > keep:
            older = self.messages[:-keep]
            self.messages = self.messages[-keep:]
            if older:
                self.summary = self._compress(older)
                compressed = True
            else:
                break
            with db_tx() as d:
                d.execute("UPDATE sessions SET summary=? WHERE id=?", (self.summary, self.id))
        return compressed

    def _compress(self, older: List[Dict]) -> str:
        text = "\n".join(f"{m['role']}: {m['content']}" for m in older)
        if self.summary:
            text = f"已有摘要：{self.summary}\n\n新增对话：\n{text}"
        try:
            new_sum = self.client.chat_sync(
                [{"role": "system", "content": COMPRESS_SYSTEM},
                 {"role": "user", "content": text[:8000]}],
                model_role="pro", temperature=0.3, max_tokens=600, stream=False)
            return new_sum.strip()
        except Exception:
            return (self.summary + "\n" + text)[-1500:]

    def tail_text(self, n: int = 12) -> str:
        return "\n".join(f"{m['role']}: {m['content']}" for m in self.messages[-n:])


# ---------------------------------------------------------------- 异步流式
class StreamWorker(QThread):
    delta = Signal(str)
    finished = Signal(str)      # 完整回复
    failed = Signal(str)

    def __init__(self, client: DeepSeekClient, messages: List[Dict],
                 model_role: str = "flash", parent=None):
        super().__init__(parent)
        self.client = client
        self.messages = messages
        self.model_role = model_role

    def run(self):
        buf: List[str] = []
        try:
            for piece in self.client.chat_stream(self.messages, self.model_role):
                buf.append(piece)
                self.delta.emit(piece)
            self.finished.emit("".join(buf))
        except Exception as e:
            self.failed.emit(str(e))


class ChatManager(QObject):
    """把 会话 / 记忆 / 模型 粘起来的业务层，全部通过信号回传 UI。"""

    reply_started = Signal()
    reply_delta = Signal(str)
    reply_finished = Signal(str)
    reply_failed = Signal(str)
    compressed = Signal()

    def __init__(self, cfg: dict, memory_manager=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.client = DeepSeekClient(cfg)
        self.memory = memory_manager
        self.session = ChatSession(cfg, self.client, session_id="default")
        self._worker: StreamWorker | None = None
        self._context_extra = ""

    # ---------------------------------------------------------- 配置
    def set_personality(self, key: str):
        if key in self.cfg["personality"]["presets"]:
            self.cfg["personality"]["active"] = key
            with db_tx() as d:
                d.execute("UPDATE sessions SET personality=? WHERE id=?",
                          (key, self.session.id))

    def set_context_extra(self, text: str):
        """外部注入的上下文（当前应用、番茄钟、课表等）。"""
        self._context_extra = text or ""

    # ---------------------------------------------------------- 对话
    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.isRunning())

    def ask(self, text: str):
        text = (text or "").strip()
        if not text or self.busy:
            return
        if not self.client.available:
            self.reply_failed.emit(
                "还没配置 DeepSeek API Key：把 .env.example 复制为 .env 并填入 Key 后重启。")
            return

        self.session.messages.append({"role": "user", "content": text})
        self.session.persist("user", text)

        mem_text = self.memory.recall(text) if self.memory else ""
        system = self.session.build_system(mem_text, self._context_extra)
        if self.session.trim(system):
            self.compressed.emit()
            system = self.session.build_system(mem_text, self._context_extra)

        messages = [{"role": "system", "content": system}] + self.session.messages
        self.reply_started.emit()
        self._worker = StreamWorker(self.client, messages, "flash", self)
        self._worker.delta.connect(self.reply_delta.emit)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self.reply_failed.emit)
        self._worker.start()

    def _on_finished(self, reply: str):
        self.session.messages.append({"role": "assistant", "content": reply})
        self.session.persist("assistant", reply)
        self.reply_finished.emit(reply)
        if self.memory:
            self.memory.on_turn_finished(self.session.tail_text(16))

    def offline_reply(self, text: str) -> str:
        """无 Key 时的本地兜底（保证界面不卡、功能可演示）。"""
        t = (text or "").strip()
        now = datetime.datetime.now()
        if any(k in t for k in ("时间", "几点")):
            return f"现在是 {now:%H:%M}，{now:%Y年%m月%d日}～"
        if any(k in t for k in ("记忆", "记得")):
            return "（离线模式）把 .env 里的 Key 配好，我就能记住你说过的话啦。"
        return (f"（离线模式）我收到啦：{t[:30]}"
                "\n配置 DeepSeek API Key 后就能正常聊天。")
