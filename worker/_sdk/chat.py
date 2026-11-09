"""飞书私信 ↔ agent 会话：持久 session、流式进度、排队、旁听日志。

与 ``feishu.FeishuBridge`` 配合使用：

- 入向：bridge.on_chat(text, message_id, open_id) → ``ChatService.handle_text``；
- 出向：``send_text_message`` / ``update_text``（bridge 实现，自测可注入假对象）；
- 旁听：每条 in/out 都 POST Router ``/chat/log``，失败只记日志。

行为约定：

- **session**：每个飞书 open_id 一条 agent session，存 ``chat-state.json``
  （临时文件 + ``os.replace`` 原子替换）。首轮新建，后续 resume。
- **失败**：agent 非零退出/超时 → 把错误摘要发飞书，并把 session 标
  ``suspicious``；下一轮重建（不带旧 session），成功后清除标记。
- **流式**：NDJSON 里的公开文本/工具进度按 ``stream_interval``（默认 1.6s）
  节流，优先编辑同一条“进行中”消息；编辑不可用时最多再补发一条，绝不逐事件
  刷屏。
- **串行**：同一 worker 一次只跑一个 agent。``ChatService`` 的 run_lock 同时
  被 Router 任务占用（``task_begin`` / ``task_end``）；聊天消息在任务期间排队，
  最多 ``queue_max``（默认 3）条，超出回一句“前面还在忙，稍后回你”。
- **回复**：超过 1500 字分条发送。

只依赖标准库；agent 可执行文件由调用方探测后传入。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .watchdog import terminate_group

REPLY_CHUNK_LIMIT = 1500
DEFAULT_ACK_TEXT = "（思考中…）"
DEFAULT_BUSY_TEXT = "前面还在忙，稍后回你"
DEFAULT_STREAM_INTERVAL = 1.6
DEFAULT_QUEUE_MAX = 3
PROGRESS_TAIL_LIMIT = 1200
MAX_PROGRESS_EDITS = 80

log = logging.getLogger("mowu-worker.chat")


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #


def split_text(text: str, limit: int = REPLY_CHUNK_LIMIT) -> List[str]:
    """按 ``limit`` 分条；优先在换行处切，避免把一句话拦腰截断。"""
    text = (text or "").strip()
    if not text:
        return []
    limit = max(1, int(limit))
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        else:
            cut += 1
        chunk = rest[:cut].strip("\n").rstrip()
        if chunk:
            chunks.append(chunk)
        rest = rest[cut:].lstrip("\n")
    rest = rest.strip()
    if rest:
        chunks.append(rest)
    return chunks


def tail_text(text: str, limit: int) -> str:
    text = text or ""
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[-limit:]


def first_line(text: str, limit: int = 160) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[:limit] + "…"


# --------------------------------------------------------------------------- #
# 持久状态
# --------------------------------------------------------------------------- #


class ChatStateStore:
    """``chat-state.json``：open_id → {session_id, suspicious, turns, ...}。

    原子写：同目录临时文件写完 fsync 后 ``os.replace``，读方永远看到完整 JSON。
    """

    def __init__(self, path: str, logger: Optional[logging.Logger] = None) -> None:
        self.path = Path(os.path.expanduser(os.path.expandvars(str(path))))
        self.log = logger or log
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"version": 1, "sessions": {}}
        self._load()

    # -- 读 -------------------------------------------------------------- #

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.log.warning("chat-state 读取失败（%s）：%s", self.path, exc)
            return
        try:
            data = json.loads(raw)
        except ValueError as exc:
            self.log.warning("chat-state 不是合法 JSON（%s）：%s，按空状态处理",
                             self.path, exc)
            return
        if not isinstance(data, dict):
            self.log.warning("chat-state 不是 JSON 对象（%s），按空状态处理", self.path)
            return
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        self._data = {"version": 1,
                      "sessions": {str(k): dict(v) for k, v in sessions.items()
                                   if isinstance(v, dict)}}

    def get(self, key: str) -> Dict[str, Any]:
        with self._lock:
            item = self._data["sessions"].get(str(key))
            return dict(item) if isinstance(item, dict) else {}

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {str(k): dict(v) for k, v in self._data["sessions"].items()}

    # -- 写 -------------------------------------------------------------- #

    def update(self, key: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            item = dict(self._data["sessions"].get(str(key)) or {})
            item.update(fields)
            item.setdefault("created_at", time.time())
            item["updated_at"] = float(fields.get("updated_at") or time.time())
            self._data["sessions"][str(key)] = item
            self._write_locked()
            return dict(item)

    def mark_suspicious(self, key: str, error: str = "") -> Dict[str, Any]:
        with self._lock:
            item = dict(self._data["sessions"].get(str(key)) or {})
            item.setdefault("created_at", time.time())
            item["suspicious"] = True
            item["last_error"] = str(error or "")[:500]
            item["updated_at"] = time.time()
            self._data["sessions"][str(key)] = item
            self._write_locked()
            return dict(item)

    def _write_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(".%s.tmp.%d.%s"
                                      % (self.path.name, os.getpid(),
                                         uuid.uuid4().hex[:8]))
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        except OSError as exc:
            self.log.warning("chat-state 写入失败（%s）：%s", self.path, exc)


# --------------------------------------------------------------------------- #
# agent 适配
# --------------------------------------------------------------------------- #


@dataclass
class RunResult:
    ok: bool
    reply: str = ""
    session_id: str = ""
    error: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    elapsed_s: float = 0.0


class AgentRunner:
    """agent 子进程：构建命令行、解析 NDJSON、看超时。

    子类只需实现 ``build_command`` 和 ``consume``。
    """

    name = "agent"

    def __init__(self, timeout: float = 300.0, workspace: str = "",
                 env: Optional[Dict[str, str]] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.timeout = max(0.1, float(timeout))
        self.workspace = os.path.abspath(
            os.path.expanduser(str(workspace or os.getcwd())))
        self.env = env
        self.log = logger or log

    # -- 子类实现 -------------------------------------------------------- #

    def build_command(self, text: str, session_id: Optional[str]) \
            -> Tuple[List[str], bool]:
        """返回 ``(argv, use_stdin)``。"""
        raise NotImplementedError

    def consume(self, obj: Dict[str, Any], state: Dict[str, Any]) \
            -> List[Tuple[str, str]]:
        """吃一个 NDJSON 事件，更新 state，返回 [(kind, text)] 进度。"""
        return []

    # -- 通用执行 -------------------------------------------------------- #

    def run(self, text: str, session_id: Optional[str] = None,
            on_event: Optional[Callable[[str, str], None]] = None,
            stop_event: Optional[threading.Event] = None) -> RunResult:
        started = time.time()
        try:
            os.makedirs(self.workspace, exist_ok=True)
        except OSError as exc:
            return RunResult(ok=False, session_id=session_id or "",
                             error="工作目录不可用：%s（%s）" % (self.workspace, exc))
        cmd, use_stdin = self.build_command(text, session_id)
        state: Dict[str, Any] = {"session_id": "", "final": "", "error": "",
                                 "raw": ""}
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.workspace,
                env=dict(self.env) if self.env is not None else None,
                stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, close_fds=True)
        except OSError as exc:
            return RunResult(ok=False, session_id=session_id or "",
                             error="%s 启动失败：%s" % (self.name, exc),
                             elapsed_s=time.time() - started)

        lines: "queue.Queue[Optional[bytes]]" = queue.Queue()

        def _reader() -> None:
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    lines.put(raw)
            except Exception:  # noqa: BLE001 - 进程被杀/管道关闭都是正常路径
                pass
            finally:
                lines.put(None)

        reader = threading.Thread(target=_reader, name="chat-stdout", daemon=True)
        reader.start()
        err_file = tempfile.TemporaryFile()
        # stderr 另起线程落临时文件，避免管道写满把 agent 卡住。
        def _err_reader() -> None:
            try:
                assert proc.stderr is not None
                for raw in proc.stderr:
                    err_file.write(raw)
            except Exception:  # noqa: BLE001
                pass

        err_thread = threading.Thread(target=_err_reader, name="chat-stderr",
                                      daemon=True)
        err_thread.start()

        timed_out = False
        aborted = False
        deadline = started + self.timeout
        try:
            if use_stdin and proc.stdin is not None:
                try:
                    proc.stdin.write(text.encode("utf-8"))
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            while True:
                if stop_event is not None and stop_event.is_set():
                    aborted = True
                    break
                try:
                    raw = lines.get(timeout=0.2)
                except queue.Empty:
                    if time.time() >= deadline:
                        timed_out = True
                        break
                    continue
                if raw is None:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    state["raw"] = tail_text(state["raw"] + "\n" + line, 4000)
                    continue
                if not isinstance(obj, dict):
                    continue
                for kind, text_item in self.consume(obj, state):
                    if on_event is not None and text_item:
                        try:
                            on_event(kind, text_item)
                        except Exception:  # noqa: BLE001 - 进度失败不影响主流程
                            self.log.exception("聊天进度回调失败")
            if timed_out or aborted:
                terminate_group(proc, grace=1.0)
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                terminate_group(proc, grace=0.0)
                rc = proc.wait(timeout=5)
        finally:
            try:
                reader.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            err_thread.join(timeout=2.0)
            try:
                err_file.seek(0)
                stderr = err_file.read().decode("utf-8", "replace")
            except OSError:
                stderr = ""
            finally:
                err_file.close()

        elapsed = time.time() - started
        sid = str(state.get("session_id") or session_id or "")
        if timed_out:
            error = "超时：%s 超过 %s 未完成，已终止" % (
                self.name, self._fmt(self.timeout))
            return RunResult(ok=False, reply="", session_id=sid, error=error,
                             exit_code=None, timed_out=True, elapsed_s=elapsed)
        if aborted:
            return RunResult(ok=False, reply="", session_id=sid,
                             error="worker 正在退出，本轮已中断",
                             exit_code=None, elapsed_s=elapsed)
        if rc != 0:
            detail = tail_text((stderr or "").strip() or state["raw"], 400)
            error = "退出码 %s" % rc
            if detail:
                error += "：%s" % detail.replace("\n", " ")
            return RunResult(ok=False, reply="", session_id=sid, error=error,
                             exit_code=rc, elapsed_s=elapsed)
        if state.get("error"):
            return RunResult(ok=False, reply="", session_id=sid,
                             error=str(state["error"])[:500], exit_code=0,
                             elapsed_s=elapsed)
        reply = str(state.get("final") or "").strip()
        if not reply:
            reply = tail_text(str(state.get("raw") or "").strip(), 1500)
        if not reply:
            reply = "（%s 没有输出）" % self.name
        return RunResult(ok=True, reply=reply, session_id=sid, exit_code=0,
                         elapsed_s=elapsed)

    @staticmethod
    def _fmt(seconds: float) -> str:
        if seconds >= 100 or float(seconds).is_integer():
            return "%ds" % int(round(seconds))
        return "%.1fs" % seconds


class DshAgent(AgentRunner):
    """``dsh --profile headless --json``；首轮不带 --session-id。"""

    name = "dsh"

    def __init__(self, bin_path: str, profile: str = "headless",
                 permission_mode: str = "workspace-write",
                 approval_policy: str = "never",
                 timeout: float = 300.0, workspace: str = "",
                 env: Optional[Dict[str, str]] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        super().__init__(timeout=timeout, workspace=workspace, env=env,
                         logger=logger)
        self.bin = bin_path
        self.profile = profile or "headless"
        self.permission_mode = permission_mode or "workspace-write"
        self.approval_policy = approval_policy or ""

    def build_command(self, text: str, session_id: Optional[str]) \
            -> Tuple[List[str], bool]:
        cmd = [self.bin, "--profile", self.profile,
               "--permission-mode", self.permission_mode]
        if self.approval_policy:
            cmd += ["--approval-policy", self.approval_policy]
        if session_id:
            cmd += ["--session-id", session_id]
        use_stdin = text.startswith("-") or len(text) > 100_000
        cmd += ["--json", "-" if use_stdin else text]
        return cmd, use_stdin

    def consume(self, obj: Dict[str, Any], state: Dict[str, Any]) \
            -> List[Tuple[str, str]]:
        kind = str(obj.get("type") or "")
        events: List[Tuple[str, str]] = []
        if kind == "session":
            sid = str(obj.get("sessionId") or "")
            if sid:
                state["session_id"] = sid
        elif kind == "final":
            sid = str(obj.get("sessionId") or "")
            if sid:
                state["session_id"] = sid
            text = str(obj.get("text") or "")
            if text:
                state["final"] = text
        elif kind == "text":
            text = str(obj.get("text") or "").strip()
            if text:
                events.append(("text", text))
        elif kind == "tool_call":
            tool = str(obj.get("tool") or "tool")
            raw_input = obj.get("input")
            payload: Dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
            detail = (payload.get("description") or payload.get("command")
                      or payload.get("path") or "")
            summary = first_line(str(detail), 120)
            events.append(("tool", "%s %s" % (tool, summary) if summary else tool))
        elif kind == "tool_result":
            status = str(obj.get("status") or "")
            if status and status not in ("completed", "ok", "success"):
                events.append(("tool", "工具失败（%s）" % status))
        elif kind in ("error", "run_error", "fatal"):
            message = str(obj.get("message") or obj.get("error") or kind)
            state["error"] = message[:500]
        return events


class CodexAgent(AgentRunner):
    """``codex exec [resume <id>] --json``；首轮从 thread.started 拿 session。"""

    name = "codex"

    def __init__(self, bin_path: str, timeout: float = 300.0,
                 workspace: str = "",
                 env: Optional[Dict[str, str]] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        super().__init__(timeout=timeout, workspace=workspace, env=env,
                         logger=logger)
        self.bin = bin_path

    def build_command(self, text: str, session_id: Optional[str]) \
            -> Tuple[List[str], bool]:
        if session_id:
            cmd = [self.bin, "exec", "resume", str(session_id),
                   "--skip-git-repo-check", "--json"]
        else:
            cmd = [self.bin, "exec", "--skip-git-repo-check", "--json"]
        use_stdin = text.startswith("-") or len(text) > 100_000
        cmd.append("-" if use_stdin else text)
        return cmd, use_stdin

    def consume(self, obj: Dict[str, Any], state: Dict[str, Any]) \
            -> List[Tuple[str, str]]:
        kind = str(obj.get("type") or "")
        events: List[Tuple[str, str]] = []
        if kind == "thread.started":
            sid = str(obj.get("thread_id") or "")
            if sid:
                state["session_id"] = sid
        elif kind in ("item.started", "item.completed"):
            raw_item = obj.get("item")
            item: Dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
            item_type = str(item.get("type") or "")
            if item_type == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    state["final"] = text
                    if kind == "item.completed":
                        events.append(("text", text))
            elif item_type == "command_execution":
                command = first_line(str(item.get("command") or ""), 120)
                if kind == "item.started":
                    events.append(("tool", "命令 %s" % command if command else "命令"))
                else:
                    code = item.get("exit_code")
                    if isinstance(code, int) and code != 0:
                        events.append(("tool", "命令失败 exit=%s" % code))
        elif kind in ("error", "turn.failed", "stream_error"):
            message = str(obj.get("message") or obj.get("error") or kind)
            state["error"] = message[:500]
        return events


# --------------------------------------------------------------------------- #
# 进度节流
# --------------------------------------------------------------------------- #


class ProgressReporter:
    """把 NDJSON 事件攒成一条“进行中”消息，按间隔编辑同一条。"""

    def __init__(self, service: "ChatService", message: "ChatMessage",
                 interval: float = DEFAULT_STREAM_INTERVAL,
                 logger: Optional[logging.Logger] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.service = service
        self.message = message
        self.interval = max(0.05, float(interval))
        self.log = logger or log
        self.clock = clock
        self.started = clock()
        self.last_flush = self.started
        self.lines: List[str] = []
        self.dirty = False
        self.progress_message_id = ""
        self.edits = 0
        self.sent = 0

    def feed(self, kind: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        prefix = "🔧 " if kind == "tool" else ""
        self.lines.append(prefix + first_line(text, 200))
        if len(self.lines) > 20:
            self.lines = self.lines[-20:]
        self.dirty = True
        self.tick()

    def tick(self) -> None:
        now = self.clock()
        if not self.dirty:
            return
        if now - self.last_flush < self.interval:
            return
        self.flush(now)

    def flush(self, now: Optional[float] = None, force: bool = False) -> None:
        now = self.clock() if now is None else now
        if not self.dirty:
            return
        if not force and now - self.last_flush < self.interval:
            return
        body = self._compose(now)
        self.dirty = False
        self.last_flush = now
        if self.edits >= MAX_PROGRESS_EDITS:
            return
        if self.progress_message_id:
            if self.service.update_text(self.progress_message_id, body):
                self.edits += 1
            return
        if self.message.ack_message_id:
            if self.service.update_text(self.message.ack_message_id, body):
                self.progress_message_id = self.message.ack_message_id
                self.edits += 1
                return
        # 没有可编辑的消息（或编辑失败）：只补发一条，绝不逐事件刷屏。
        if self.sent < 1:
            mid = self.service.send_text(body)
            if mid:
                self.progress_message_id = mid
                self.sent += 1

    def finish(self) -> None:
        self.flush(force=True)

    def _compose(self, now: float) -> str:
        elapsed = max(0, int(round(now - self.started)))
        body = tail_text("\n".join(self.lines), PROGRESS_TAIL_LIMIT)
        head = "（进行中… %ds）" % elapsed
        return head + ("\n" + body if body else "")


# --------------------------------------------------------------------------- #
# 聊天服务
# --------------------------------------------------------------------------- #


@dataclass
class ChatMessage:
    text: str
    message_id: str
    open_id: str
    ack_message_id: str = ""
    queued_at: float = field(default_factory=time.time)


class ChatService:
    """飞书私信 → agent → 飞书回复（单 worker 串行）。"""

    def __init__(self, worker_name: str, bridge: Any, agent: AgentRunner,
                 store: ChatStateStore,
                 post_log: Optional[Callable[[Dict[str, Any]], None]] = None,
                 queue_max: int = DEFAULT_QUEUE_MAX,
                 ack_text: str = DEFAULT_ACK_TEXT,
                 busy_text: str = DEFAULT_BUSY_TEXT,
                 stream_interval: float = DEFAULT_STREAM_INTERVAL,
                 logger: Optional[logging.Logger] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.worker_name = worker_name
        self.bridge = bridge
        self.agent = agent
        self.store = store
        self.post_log = post_log
        self.queue_max = max(1, int(queue_max))
        self.ack_text = ack_text or DEFAULT_ACK_TEXT
        self.busy_text = busy_text or DEFAULT_BUSY_TEXT
        self.stream_interval = max(0.05, float(stream_interval))
        self.log = logger or log
        self.clock = clock
        self._cond = threading.Condition()
        self._pending: List[ChatMessage] = []
        self._run_lock = threading.Lock()
        self._task_active = threading.Event()
        self._task_holds_lock = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.runs = 0
        self.busy_replies = 0
        self.errors = 0

    # -- 生命周期 -------------------------------------------------------- #

    def start(self, stop_event: Optional[threading.Event] = None) -> None:
        if stop_event is not None:
            self._stop = stop_event
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="feishu-chat",
                                        daemon=True)
        self._thread.start()
        self.log.info("聊天服务已启动 worker=%s queue_max=%d interval=%.1fs",
                      self.worker_name, self.queue_max, self.stream_interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def task_begin(self) -> None:
        """Router 任务开始：等当前聊天轮次跑完，然后独占 agent 槽位。"""
        self._task_active.set()
        while not self._run_lock.acquire(timeout=0.2):
            if self._stop.is_set():
                return
        self._task_holds_lock = True

    def task_end(self) -> None:
        self._task_active.clear()
        if not self._task_holds_lock:
            return
        self._task_holds_lock = False
        try:
            self._run_lock.release()
        except RuntimeError:
            pass

    def pending_count(self) -> int:
        with self._cond:
            return len(self._pending)

    # -- 入向 ------------------------------------------------------------ #

    def handle_text(self, text: str, message_id: str = "",
                    open_id: str = "") -> bool:
        """收到训练员私信：立即 ack、入队；满了回忙线提示。"""
        text = (text or "").strip()
        if not text:
            return False
        open_id = open_id or "default"
        mid = (message_id or "").strip() or ("local-in-%s" % uuid.uuid4().hex[:12])
        state = self.store.get(open_id)
        session_id = "" if state.get("suspicious") else str(state.get("session_id") or "")

        msg = ChatMessage(text=text, message_id=mid, open_id=open_id)
        with self._cond:
            if len(self._pending) >= self.queue_max:
                full = True
            else:
                self._pending.append(msg)
                full = False
                self._cond.notify_all()
        if full:
            self.busy_replies += 1
            self.send_text(self.busy_text)
            self._log_chat("in", session_id, mid, text, open_id)
            self.log.info("聊天队列已满（%d），回忙线提示", self.queue_max)
            return True

        # 先回轻量反馈（不被 /chat/log 的网络耗时拖延），再旁听。
        msg.ack_message_id = self.send_text(self.ack_text)
        self._log_chat("in", session_id, mid, text, open_id)
        return True

    # -- 出向 ------------------------------------------------------------ #

    def send_text(self, text: str) -> str:
        """发一条私信，返回 message_id（失败/假桥不返回 id 时为空串）。"""
        if not text:
            return ""
        try:
            func = getattr(self.bridge, "send_text_message", None)
            if callable(func):
                mid = func(text)
                return mid if isinstance(mid, str) else ""
            if self.bridge.send_text(text):
                return ""
        except Exception:  # noqa: BLE001 - 发消息失败不能拖垮聊天
            self.log.exception("聊天发送失败")
        return ""

    def update_text(self, message_id: str, text: str) -> bool:
        if not message_id or not text:
            return False
        try:
            func = getattr(self.bridge, "update_text", None)
            if callable(func):
                return bool(func(message_id, text))
        except Exception:  # noqa: BLE001
            self.log.exception("聊天消息编辑失败")
        return False

    def _log_chat(self, direction: str, session_id: str, message_id: str,
                  text: str, open_id: str) -> None:
        if self.post_log is None:
            return
        payload = {
            "worker": self.worker_name,
            "direction": direction,
            "session_id": session_id or "",
            "message_id": message_id or "",
            "text": text or "",
            "ts": self.clock(),
        }
        try:
            self.post_log(payload)
            self.log.debug("chat/log %s session=%s msg=%s",
                           direction, session_id or "-", message_id or "-")
        except Exception as exc:  # noqa: BLE001 - 旁听失败不影响聊天
            self.log.warning("chat/log 上报失败（%s %s）：%s",
                             direction, message_id or "-", exc)

    # -- 队列 ------------------------------------------------------------ #

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                while not self._pending and not self._stop.is_set():
                    self._cond.wait(0.2)
                if self._stop.is_set():
                    return
            if not self._run_lock.acquire(timeout=0.2):
                continue
            msg: Optional[ChatMessage] = None
            with self._cond:
                if self._pending:
                    msg = self._pending.pop(0)
            if msg is None:
                self._run_lock.release()
                continue
            try:
                self._run_turn(msg)
            except Exception:  # noqa: BLE001 - 单条消息失败不能停掉队列
                self.log.exception("聊天轮次异常：%s", msg.message_id)
                self.errors += 1
            finally:
                self._run_lock.release()

    # -- 一轮 ------------------------------------------------------------ #

    def _run_turn(self, msg: ChatMessage) -> None:
        self.runs += 1
        state = self.store.get(msg.open_id)
        stale = str(state.get("session_id") or "")
        if stale and not state.get("suspicious"):
            resume_id: Optional[str] = stale
        else:
            resume_id = None
        reporter = ProgressReporter(self, msg, interval=self.stream_interval,
                                    logger=self.log, clock=self.clock)
        self.log.info("聊天开始 open_id=%s msg=%s session=%s%s",
                      msg.open_id, msg.message_id,
                      resume_id or "(新建)",
                      "（可疑，重建）" if stale and resume_id is None else "")
        started = self.clock()
        result = self.agent.run(msg.text, resume_id,
                                on_event=reporter.feed,
                                stop_event=self._stop)
        reporter.finish()
        elapsed = self.clock() - started
        sid = result.session_id or (resume_id or "")

        if self._stop.is_set():
            self.log.info("worker 正在退出，聊天 %s 不再回发", msg.message_id)
            return

        if result.ok:
            fields: Dict[str, Any] = {
                "session_id": sid,
                "suspicious": False,
                "last_error": None,
                "turns": int(state.get("turns") or 0) + 1,
            }
            self.store.update(msg.open_id, **fields)
            reply = (result.reply or "").strip() or "（没有回复内容）"
            chunks = split_text(reply, REPLY_CHUNK_LIMIT)
            for chunk in chunks:
                mid = self.send_text(chunk)
                self._log_chat("out", sid, mid or self._local_out_id(), chunk,
                               msg.open_id)
            self.log.info("聊天完成 msg=%s session=%s %.1fs 回复 %d 字/%d 条",
                          msg.message_id, sid or "-", elapsed, len(reply),
                          len(chunks))
        else:
            self.errors += 1
            if stale or result.session_id:
                self.store.mark_suspicious(msg.open_id, result.error)
            self.log.warning("聊天失败 msg=%s session=%s：%s",
                             msg.message_id, sid or "-", result.error)
            notice = "❌ 出错了：%s" % tail_text(result.error, 600)
            mid = self.send_text(notice)
            self._log_chat("out", sid, mid or self._local_out_id(), notice,
                           msg.open_id)

    @staticmethod
    def _local_out_id() -> str:
        """发送失败/桥不返回 id 时的本地占位，保证 /chat/log 字段非空。"""
        return "local-out-%s" % uuid.uuid4().hex[:12]


__all__ = [
    "AgentRunner", "ChatMessage", "ChatService", "ChatStateStore",
    "CodexAgent", "DshAgent", "ProgressReporter", "RunResult",
    "DEFAULT_ACK_TEXT", "DEFAULT_BUSY_TEXT", "DEFAULT_QUEUE_MAX",
    "DEFAULT_STREAM_INTERVAL", "REPLY_CHUNK_LIMIT", "split_text",
]
