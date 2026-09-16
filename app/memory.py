"""Task 5：长期记忆与偏好。

- SQLite 记忆表：结构化偏好 / 重要事实 / 禁忌
- 定期把最近对话交给 pro 模型，抽取偏好与禁忌并落库（子线程）
- 新对话启动时按关键词召回相关记忆，拼进 system 上下文
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, List

from PySide6.QtCore import QObject, QThread, Signal

from app.utils import db_tx, estimate_tokens, get_db, today_str

# kind: preference(偏好) | fact(重要事实) | taboo(禁忌/讨厌)
KINDS = ("preference", "fact", "taboo")

EXTRACT_SYSTEM = """你是一个偏好抽取器。阅读对话后，只输出 JSON 数组，不要任何解释。
每条格式：{"kind":"preference|fact|taboo","subject":"主题(2-8字)","content":"一句话陈述(<=40字)"}
- preference: 用户的喜好、习惯、常用工具、作息等
- fact: 关于用户的重要事实（学校、专业、项目、家人朋友、目标等）
- taboo: 用户明确表示讨厌/不想提/禁止的话题
只抽取确信的信息，最多 6 条，没有就输出 []。"""

EXTRACT_USER = "请从下面这段对话中抽取用户偏好与重要事实：\n\n{dialogue}"


# ---------------------------------------------------------------- 关键词
def tokenize(text: str) -> List[str]:
    """中英文混合分词：CJK 取 1-gram + 2-gram，英文取单词。"""
    if not text:
        return []
    words = re.findall(r"[a-zA-Z0-9_]{2,}", text.lower())
    cjk = re.findall(r"[\u4e00-\u9fff]+", text)
    grams: List[str] = []
    for seg in cjk:
        grams.extend(seg)                                     # 1-gram
        if len(seg) > 1:
            grams += [seg[i:i + 2] for i in range(len(seg) - 1)]  # 2-gram
    return words + grams


# ---------------------------------------------------------------- 存储
class MemoryStore:
    def __init__(self):
        pass

    def add(self, kind: str, content: str, subject: str = "", source: str = "chat",
            weight: float = 1.0) -> int:
        kind = kind if kind in KINDS else "fact"
        content = (content or "").strip()
        if not content:
            return -1
        now = time.time()
        with db_tx() as db:
            old = db.execute(
                "SELECT id, weight, hit_count FROM memories WHERE kind=? AND content=?",
                (kind, content)).fetchone()
            if old:
                db.execute(
                    "UPDATE memories SET weight=?, updated_at=? WHERE id=?",
                    (min(3.0, float(old["weight"]) + 0.2), now, old["id"]))
                return old["id"]
            cur = db.execute(
                "INSERT INTO memories(kind, subject, content, weight, source,"
                " created_at, updated_at, hit_count) VALUES(?,?,?,?,?,?,?,0)",
                (kind, subject, content, weight, source, now, now))
            return cur.lastrowid

    def add_many(self, items: List[Dict]):
        n = 0
        for it in items:
            if self.add(it.get("kind", "fact"), it.get("content", ""),
                        it.get("subject", ""), it.get("source", "extract")) >= 0:
                n += 1
        return n

    def recall(self, query: str, limit: int = 8) -> List[Dict]:
        """关键词召回：命中关键词数 * 权重 排序，并累加 hit_count。"""
        keys = set(tokenize(query))
        rows = get_db().execute(
            "SELECT * FROM memories ORDER BY weight DESC, updated_at DESC LIMIT 400"
        ).fetchall()
        scored = []
        for r in rows:
            text = f"{r['subject']} {r['content']}"
            k = set(tokenize(text))
            score = len(keys & k)
            if score <= 0:
                continue
            scored.append((score * float(r["weight"]), dict(r)))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = [d for _, d in scored[:limit]]
        if out:
            with db_tx() as db:
                db.executemany(
                    "UPDATE memories SET hit_count=hit_count+1 WHERE id=?",
                    [(d["id"],) for d in out])
        return out

    def recent(self, limit: int = 10) -> List[Dict]:
        rows = get_db().execute(
            "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def all(self) -> List[Dict]:
        rows = get_db().execute(
            "SELECT * FROM memories ORDER BY kind, updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def delete(self, mid: int):
        with db_tx() as db:
            db.execute("DELETE FROM memories WHERE id=?", (mid,))

    def clear(self):
        with db_tx() as db:
            db.execute("DELETE FROM memories")

    def count(self) -> int:
        return get_db().execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]

    def render(self, items: List[Dict]) -> str:
        """把记忆渲染成可拼进 system 的文本。"""
        if not items:
            return ""
        tag = {"preference": "偏好", "fact": "事实", "taboo": "禁忌"}
        lines = [f"- [{tag.get(i['kind'], i['kind'])}] {i['content']}" for i in items]
        return "你对用户的长期记忆：\n" + "\n".join(lines)


# ---------------------------------------------------------------- 异步抽取
class _ExtractWorker(QThread):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, client, dialogue: str, parent=None):
        super().__init__(parent)
        self.client = client
        self.dialogue = dialogue

    def run(self):
        try:
            raw = self.client.chat_sync(
                messages=[
                    {"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": EXTRACT_USER.format(
                        dialogue=self.dialogue[:6000])},
                ],
                model_role="pro",
                temperature=0.2,
                max_tokens=700,
                stream=False,
            )
            items = self._parse(raw)
            self.done.emit(items)
        except Exception as e:
            self.failed.emit(str(e))

    @staticmethod
    def _parse(raw: str) -> List[Dict]:
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        s, e = raw.find("["), raw.rfind("]")
        if s < 0 or e <= s:
            return []
        try:
            data = json.loads(raw[s:e + 1])
        except Exception:
            return []
        out = []
        for it in data:
            if not isinstance(it, dict):
                continue
            kind = str(it.get("kind", "fact")).lower()
            content = str(it.get("content", "")).strip()
            if not content:
                continue
            out.append({"kind": kind if kind in KINDS else "fact",
                        "subject": str(it.get("subject", ""))[:32],
                        "content": content[:120], "source": "extract"})
        return out


class MemoryManager(QObject):
    """记忆管理：抽取（异步）+ 召回（同步）。"""

    extracted = Signal(int, list)      # 新增条数, 明细
    failed = Signal(str)

    def __init__(self, cfg: dict, client, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.client = client
        self.store = MemoryStore()
        self._turns_since = 0
        self._worker: _ExtractWorker | None = None
        self._busy = False

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("memory", {}).get("enabled", True))

    def recall(self, query: str) -> str:
        if not self.enabled:
            return ""
        limit = int(self.cfg.get("memory", {}).get("recall_limit", 8))
        return self.store.render(self.store.recall(query, limit))

    def recent_text(self, limit: int = 8) -> str:
        return self.store.render(self.store.recent(limit))

    # ---------------------------------------------------------- 自动抽取
    def on_turn_finished(self, dialogue_window: str):
        """每完成一轮对话调用；达到阈值触发后台抽取。"""
        if not self.enabled or self._busy:
            return
        self._turns_since += 1
        every = int(self.cfg.get("memory", {}).get("summarize_every_turns", 16))
        if self._turns_since < every:
            return
        self._turns_since = 0
        self.extract_now(dialogue_window)

    def extract_now(self, dialogue: str):
        if not dialogue.strip():
            return
        self._busy = True
        self._worker = _ExtractWorker(self.client, dialogue, self)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_done(self, items: List[Dict]):
        self._busy = False
        n = self.store.add_many(items)
        self._trim()
        self.extracted.emit(n, items)

    def _on_failed(self, msg: str):
        self._busy = False
        self.failed.emit(msg)

    def _trim(self):
        mx = int(self.cfg.get("memory", {}).get("max_facts", 300))
        with db_tx() as db:
            db.execute(
                "DELETE FROM memories WHERE id IN ("
                "SELECT id FROM memories ORDER BY weight ASC, updated_at ASC "
                "LIMIT MAX(0,(SELECT COUNT(*)-? FROM memories)))", (mx,))


def memory_stats() -> Dict:
    db = get_db()
    rows = db.execute(
        "SELECT kind, COUNT(*) c FROM memories GROUP BY kind").fetchall()
    return {r["kind"]: r["c"] for r in rows}


def tokens_of_memories(items: List[Dict]) -> int:
    return sum(estimate_tokens(i["content"]) for i in items)
