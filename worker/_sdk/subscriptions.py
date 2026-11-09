"""订阅：向 Router 声明订阅、轮询 pending、落盘 JSONL、飞书汇总通报。

设计取舍（按训练员要求）：
- 订阅消息是“实时听”，Router 端不留存；worker 掉线重连后由自己重新
  ``POST /subscriptions`` 声明（因此 ``subscribe_all`` 在每次 register 后都调）。
- 本地仍落一份 ``subscription-feed.jsonl``，方便事后回看。
- 飞书通报做 60 秒窗口限流：窗口内最多一条汇总，不逐条轰炸。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional
from urllib.parse import urlencode

log = logging.getLogger("mowu-worker.subscriptions")

SUMMARY_WINDOW_S = 60.0
SUMMARY_MAX_LINES = 12


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value or ""))


def extract_messages(payload: Any) -> List[Dict[str, Any]]:
    """从 Router 的 pending 响应里取消息列表，兼容几种常见包法。"""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "items", "pending", "events", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_messages(value)
            if nested:
                return nested
    return []


def _message_key(message: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "message_id", "msg_id", "event_id"):
        value = message.get(key)
        if value:
            return "%s:%s" % (key, value)
    return None


def _content_text(message: Dict[str, Any]) -> str:
    for key in ("text", "content", "message", "body"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for inner in ("text", "content"):
                if isinstance(value.get(inner), str) and value[inner].strip():
                    return value[inner].strip()
    return json.dumps(message, ensure_ascii=False)[:300]


def format_message(message: Dict[str, Any], limit: int = 140) -> str:
    source = str(message.get("source") or message.get("channel") or "?")
    kind = str(message.get("kind") or message.get("level") or "")
    text = _content_text(message)
    if len(text) > limit:
        text = text[:limit] + "…"
    prefix = "[%s%s]" % (source, ("/" + kind) if kind else "")
    who = message.get("from") or message.get("sender") or message.get("author")
    if who:
        prefix += " %s" % who
    return "%s %s" % (prefix, text)


class FeedNotifier:
    """60 秒窗口内最多发一条飞书汇总；``send_text`` 可注入。"""

    def __init__(
        self,
        send_text: Callable[[str], bool],
        window_s: float = SUMMARY_WINDOW_S,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
        max_lines: int = SUMMARY_MAX_LINES,
    ) -> None:
        self.send_text = send_text
        self.window_s = float(window_s)
        self.clock = clock
        self.log = logger or log
        self.max_lines = max_lines
        self._pending: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._last_sent = float("-inf")
        self.batches = 0
        self.dropped = 0

    def add(self, messages: Iterable[Dict[str, Any]]) -> int:
        with self._lock:
            added = [m for m in messages if isinstance(m, dict)]
            self._pending.extend(added)
            return len(added)

    def flush(self, now: Optional[float] = None,
              force: bool = False) -> bool:
        """到期才发；返回本次是否真的发了飞书。"""
        moment = self.clock() if now is None else now
        with self._lock:
            if not self._pending:
                return False
            if not force and moment - self._last_sent < self.window_s:
                return False
            batch = self._pending
            self._pending = []
            self._last_sent = moment
        self.batches += 1
        lines = [format_message(item) for item in batch[:self.max_lines]]
        more = len(batch) - len(lines)
        body = "📨 订阅消息 %d 条\n%s" % (len(batch), "\n".join(lines))
        if more > 0:
            body += "\n…还有 %d 条（已写入 subscription-feed.jsonl）" % more
        try:
            return bool(self.send_text(body))
        except Exception:  # noqa: BLE001 - 通报失败不能停轮询
            self.log.exception("订阅飞书通报失败")
            return False


class SubscriptionPoller:
    """轮询 Router 的 /subscriptions/pending 并落盘。

    ``post_json(path, payload)`` / ``get_json(path)`` 由 worker 注入，
    复用 worker 的 Router token 与错误类型，SDK 不直接碰 urllib。
    """

    def __init__(
        self,
        worker: str,
        sources: Iterable[str],
        post_json: Callable[[str, Dict[str, Any]], Any],
        get_json: Callable[[str], Any],
        feed_path: str,
        notifier: Optional[FeedNotifier] = None,
        interval: float = 15.0,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
        dedupe_size: int = 512,
    ) -> None:
        self.worker = worker
        self.sources = [str(s).strip() for s in sources if str(s).strip()]
        self.post_json = post_json
        self.get_json = get_json
        self.feed_path = _expand(feed_path)
        self.notifier = notifier
        self.interval = max(0.1, float(interval))
        self.clock = clock
        self.log = logger or log
        self._seen: Deque[str] = deque(maxlen=max(1, dedupe_size))
        self._seen_set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.subscribed = 0
        self.polls = 0
        self.written = 0
        self.errors = 0

    # -- 订阅声明 -------------------------------------------------------- #

    def subscribe_all(self) -> int:
        ok = 0
        for source in self.sources:
            try:
                self.post_json("/subscriptions",
                               {"worker": self.worker, "source": source})
                ok += 1
                self.log.info("订阅成功 worker=%s source=%s",
                              self.worker, source)
            except Exception as exc:  # noqa: BLE001 - Router 0.2 可能还没上线
                self.errors += 1
                self.log.warning("订阅失败 source=%s：%s", source, exc)
        self.subscribed = ok
        return ok

    # -- 拉取 ------------------------------------------------------------ #

    def poll_once(self) -> int:
        self.polls += 1
        query = urlencode({"worker": self.worker})
        try:
            payload = self.get_json("/subscriptions/pending?" + query)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self.log.warning("订阅 pending 拉取失败：%s", exc)
            return 0
        messages = extract_messages(payload)
        fresh: List[Dict[str, Any]] = []
        for message in messages:
            key = _message_key(message)
            if key is not None:
                if key in self._seen_set:
                    continue
                if len(self._seen) == self._seen.maxlen:
                    self._seen_set.discard(self._seen[0])
                self._seen.append(key)
                self._seen_set.add(key)
            fresh.append(message)
        if not fresh:
            return 0
        try:
            self._append_jsonl(fresh)
        except OSError as exc:
            self.errors += 1
            self.log.warning("订阅落盘失败（%s）：%s", self.feed_path, exc)
        if self.notifier is not None:
            self.notifier.add(fresh)
            self.notifier.flush(now=self.clock())
        self.log.info("订阅收到 %d 条（worker=%s）", len(fresh), self.worker)
        return len(fresh)

    def _append_jsonl(self, messages: List[Dict[str, Any]]) -> None:
        target = Path(self.feed_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = self.clock()
        with open(target, "a", encoding="utf-8") as handle:
            for message in messages:
                record = dict(message)
                record.setdefault("worker", self.worker)
                record.setdefault("received_at", stamp)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.written += len(messages)

    # -- 线程 ------------------------------------------------------------ #

    def run(self, stop_event: Optional[threading.Event] = None) -> None:
        stop = stop_event or self._stop
        next_poll = self.clock()
        while not stop.is_set():
            now = self.clock()
            if now >= next_poll:
                self.poll_once()
                if self.notifier is not None:
                    self.notifier.flush(now=self.clock())
                next_poll = self.clock() + self.interval
            stop.wait(0.2)

    def start(self, stop_event: Optional[threading.Event] = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run, args=(stop_event or self._stop,),
            name="subscriptions", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def content_key(self, message: Dict[str, Any]) -> str:
        """没有显式 id 时的内容指纹（供调用方/自测使用）。"""
        raw = json.dumps(message, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
