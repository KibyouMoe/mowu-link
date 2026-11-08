#!/usr/bin/env python3
"""dsh worker — 从 Router 领活，用 ``dsh --profile headless`` 执行，把结果交回去。

协议见 docs/ROUTER.md §二、§五：

    POST /register   {name, version}                        → {ok, queued}
    GET  /stream     ?name=<name>                            → SSE 下行（任务）
    POST /heartbeat  {name, state, progress_at, task_id?}
    POST /result     {name, task_id, status, summary, artifacts[], evidence[]}

行为约定：

- **串行**：一次只跑一个任务，跑完才读下一条（Router 按名字排队）。
- **重连**：SSE 断线后 1s→2s→4s→8s→16s→30s 封顶；每次重连先重新 /register。
- **心跳**：独立线程按间隔上报；任务运行中 state=running 并带 task_id/progress_at。
- **退出**：SIGTERM/SIGINT 停心跳、杀子进程组、把当前任务以 failed/interrupted
  上报（能报就报），然后退出 0。
- **日志**：~/.hermes/logs/dsh-worker.log，只追加，不轮转。

只用标准库，兼容 /usr/bin/python3 (3.9)。
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

VERSION = "0.1.1"
DEFAULT_ROUTER = "http://127.0.0.1:8790"
DEFAULT_NAME = "dsh"
DEFAULT_TOKEN_FILE = "~/.hermes/secrets/router.token"
DEFAULT_LOG_FILE = "~/.hermes/logs/dsh-worker.log"
DEFAULT_PERMISSION_MODE = "workspace-write"
DEFAULT_TIMEOUT_S = 900.0
DEFAULT_HEARTBEAT_S = 20.0
DEFAULT_IDLE_HEARTBEAT_S = 60.0
DEFAULT_IDLE_TIMEOUT_S = 90.0
DEFAULT_DSH_PROFILE = "headless"
DEFAULT_APPROVAL_POLICY = "never"  # 无人值守：不等人点“允许”
SUMMARY_LIMIT = 4000
RESULT_CACHE_SIZE = 64
PERMISSION_MODES = ("read-only", "workspace-write", "danger-full-access")
APPROVAL_POLICIES = ("ask", "never", "")

# SSE 读超时（socket 级）。设小一点，SIGTERM 才能在 1 秒内打断阻塞读。
# 注意：超时只代表“这一刻没数据”（SSE 空闲时 Router 只有 ping），绝不算断线；
# 真正的“连接死掉”由 idle_timeout（连续多久没有任何字节）判定。
READ_TIMEOUT_S = 1.0

_OPENER = build_opener(ProxyHandler({}))  # 本机服务，绕过系统代理

log = logging.getLogger("dsh-worker")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


class RouterError(Exception):
    """与 Router 通信失败（连接、HTTP 状态、SSE 断开）。"""

    def __init__(self, message: str, status: Optional[int] = None,
                 body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def expand_path(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value or ""))


def setup_logging(path: str, verbose: bool = False) -> None:
    """日志只追加、不轮转（按训练员要求）。"""
    target = Path(expand_path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
    fh = logging.FileHandler(str(target), mode="a", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    fh.setLevel(logging.DEBUG)
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        sh.setLevel(logging.DEBUG)
        log.addHandler(sh)


def http_json(url: str, payload: Optional[Dict[str, Any]] = None,
              token: Optional[str] = None, timeout: float = 15.0) -> Any:
    """POST/GET JSON；失败统一抛 RouterError。"""
    data = None
    headers = {"Accept": "application/json",
               "User-Agent": "dsh-worker/%s" % VERSION}
    if token:
        # mowu Router 用 X-Router-Token；Authorization 兼容别的实现。
        headers["X-Router-Token"] = token
        headers["Authorization"] = "Bearer %s" % token
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = Request(url, data=data, headers=headers,
                  method="POST" if data is not None else "GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:800]
        except Exception:
            pass
        raise RouterError("HTTP %s %s %s" % (exc.code, url, body),
                          status=exc.code, body=body)
    except URLError as exc:
        raise RouterError("连接失败 %s: %s" % (url, exc.reason))
    except (OSError, http.client.HTTPException) as exc:
        raise RouterError("请求失败 %s: %s" % (url, exc))
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")}


def read_token(path: str) -> Optional[str]:
    """读 router token（第一行，去掉可选的 'Bearer ' 前缀）。缺失不致命。"""
    if not path:
        return None
    target = Path(expand_path(path))
    try:
        text = target.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        log.warning("token 文件不存在（%s），按未鉴权连接", target)
        return None
    except OSError as exc:
        log.warning("token 文件读取失败（%s）：%s", target, exc)
        return None
    if not text:
        return None
    first = text.splitlines()[0].strip()
    if first.lower().startswith("bearer "):
        first = first[7:].strip()
    return first or None


def resolve_dsh(value: str) -> Optional[str]:
    raw = expand_path(value)
    if os.sep in raw:
        candidate = os.path.abspath(raw)
        return candidate if os.path.exists(candidate) else None
    return shutil.which(raw)


def tail_text(text: str, limit: int) -> str:
    text = text or ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def compose_summary(note: str, stdout: str, stderr: str = "",
                    limit: int = SUMMARY_LIMIT) -> str:
    """拼 summary：note 在前，stdout 取末尾，stderr 挤在预算内；总长 ≤ limit。"""
    note = (note or "").strip()
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    budget = limit - (len(note) + 2 if note else 0)
    if budget < 0:
        return note[:limit]
    body = tail_text(out, budget)
    if err and len(body) < budget - 20:
        err_budget = min(800, budget - len(body) - 12)
        if err_budget > 0:
            body += "\n[stderr]\n" + tail_text(err, err_budget)
    summary = (note + "\n\n" + body) if note else body
    if len(summary) > limit:
        summary = summary[-limit:]
    return summary


def build_prompt(goal: str, context: str) -> str:
    goal = (goal or "").strip()
    context = (context or "").strip()
    prompt = goal if not context else "%s\n\n## Context\n%s" % (goal, context)
    # argv 有上限（macOS 约 1MB），留足余量；超长走 stdin 兜底。
    if len(prompt) > 200_000:
        prompt = prompt[:200_000] + "\n\n[上下文过长，已截断]"
    return prompt


def fmt_duration(seconds: float) -> str:
    if seconds >= 100 or float(seconds).is_integer():
        return "%ds" % int(round(seconds))
    return "%.1fs" % seconds


def _is_read_timeout(exc: BaseException) -> bool:
    """是不是“这一刻没数据”的读超时。

    ``socket.timeout`` / ``TimeoutError`` 是正常形态；Python 的 ``SocketIO`` 在
    超时一次后会置 ``_timeout_occurred``，之后每次读都抛
    ``OSError("cannot read from timed out object")``，那同样是超时，不是断线。
    """
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    return "cannot read from timed out object" in str(exc).lower()


def clear_read_timeout(resp: Any) -> bool:
    """清掉 SocketIO 的超时中毒标记；返回是否可以直接继续读。

    标准库没有公开的重置接口，只能清私有标记（3.5–3.13 都是这个名字）。
    找不到标记说明换了实现或没有中毒，按可继续处理。
    """
    fp = getattr(resp, "fp", None)
    for obj in (fp, getattr(fp, "raw", None), getattr(fp, "buffer", None)):
        if obj is None or not hasattr(obj, "_timeout_occurred"):
            continue
        try:
            obj._timeout_occurred = False  # noqa: SLF001 - 标准库无公开复位接口
            return True
        except Exception:  # noqa: BLE001 - 只读属性等，退化成等待
            return False
    return True


def iter_sse(resp: Any, stop_event: threading.Event,
             idle_timeout: float) -> Iterable[Tuple[str, str]]:
    """解析 SSE 流。

    读超时 = 暂无数据，继续等；只有真 EOF、连接重置/其他 IO 异常、或连续
    ``idle_timeout`` 秒一个字节都没收到（ping 注释也算字节），才算断线。
    """
    event = "message"
    data: List[str] = []
    last_activity = time.time()

    def idle_expired() -> bool:
        return time.time() - last_activity > idle_timeout

    def raise_idle() -> None:
        raise RouterError("SSE 静默超过 %ss（无任何字节），按断线处理"
                          % fmt_duration(idle_timeout))

    while not stop_event.is_set():
        try:
            raw = resp.readline()
        except (socket.timeout, TimeoutError):
            if not clear_read_timeout(resp):
                stop_event.wait(0.2)  # 清不掉标记，别空转
            if idle_expired():
                raise_idle()
            continue
        except OSError as exc:
            if not _is_read_timeout(exc):
                raise RouterError("SSE 读取中断：%s" % exc)
            if not clear_read_timeout(resp):
                stop_event.wait(0.2)
            if idle_expired():
                raise_idle()
            continue
        except (ValueError, http.client.HTTPException) as exc:
            if stop_event.is_set():
                return
            raise RouterError("SSE 读取中断：%s" % exc)
        if not raw:
            if data and event.lower() != "ping":
                yield event, "\n".join(data)
            if stop_event.is_set():
                return
            raise RouterError("SSE 连接已被 Router 关闭")
        last_activity = time.time()
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data and event.lower() != "ping":
                yield event, "\n".join(data)
                # 执行任务期间不读流，不能让空闲计时把连接判死。
                last_activity = time.time()
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue  # 注释 / keepalive（: ping），刷新过 last_activity
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value.strip() or "message"
        elif field == "data":
            data.append(value)


def extract_task(event: str, data: str) -> Optional[Dict[str, Any]]:
    """从 SSE 事件里认任务。宽松兼容几种常见包法：

    - ``event: task`` + ``data: {task_id, goal, ...}``
    - ``{"type":"task","task":{...}}``
    - ``{"type":"task","task_id":...,"goal":...}``
    - 裸任务对象（有 task_id/id 且有 goal/task/prompt）
    """
    try:
        obj = json.loads(data)
    except ValueError:
        log.warning("SSE data 不是 JSON，忽略：%r", data[:200])
        return None
    if not isinstance(obj, dict):
        log.warning("SSE data 不是对象，忽略：%r", str(obj)[:200])
        return None
    kind = str(obj.get("type") or event or "").lower()
    inner = obj.get("task")
    candidate = inner if isinstance(inner, dict) else obj
    has_goal = any(candidate.get(k) for k in ("goal", "task", "prompt"))
    if kind in ("task", "job", "dispatch") or has_goal:
        task_id = candidate.get("task_id") or candidate.get("id")
        if not task_id:
            log.warning("任务事件缺 task_id，忽略：%r", str(candidate)[:300])
            return None
        if not has_goal:
            log.warning("任务 %s 缺 goal，交给执行层报错", task_id)
        return candidate
    log.info("忽略 SSE 事件 type=%s（非任务）", kind or "(空)")
    return None


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Router 的 DSH worker：接任务 → dsh headless → 回结果")
    p.add_argument("--config", help="JSON 配置文件（命令行参数优先）")
    p.add_argument("--router", default=DEFAULT_ROUTER, help="Router 地址")
    p.add_argument("--name", default=DEFAULT_NAME, help="worker 名字（Router 按名字挂任务）")
    p.add_argument("--token-file", default=DEFAULT_TOKEN_FILE,
                   help="router token 文件；缺失则不带鉴权")
    p.add_argument("--workspace", default=os.getcwd(), help="默认工作目录")
    p.add_argument("--permission-mode", default=DEFAULT_PERMISSION_MODE,
                   choices=PERMISSION_MODES, help="dsh 权限预设")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                   help="单任务超时秒数（默认 900）")
    p.add_argument("--dsh-bin", default="dsh", help="dsh 可执行文件（默认 PATH 里的 dsh）")
    p.add_argument("--dsh-profile", default=DEFAULT_DSH_PROFILE, help="dsh profile 名")
    p.add_argument("--approval-policy", default=DEFAULT_APPROVAL_POLICY,
                   choices=APPROVAL_POLICIES,
                   help="dsh approval 策略；默认 never（无人值守），空串=不传")
    p.add_argument("--heartbeat-interval", type=float, default=DEFAULT_HEARTBEAT_S,
                   help="任务运行中心跳间隔秒数")
    p.add_argument("--idle-heartbeat", type=float, default=DEFAULT_IDLE_HEARTBEAT_S,
                   help="空闲心跳间隔秒数")
    p.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_S,
                   help="SSE 静默多久判定断线")
    p.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="日志文件（只追加）")
    p.add_argument("--verbose", action="store_true", help="同时把日志打到 stderr")
    p.add_argument("--version", action="version", version="dsh-worker %s" % VERSION)
    return p


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    known, _ = pre.parse_known_args(argv)
    file_cfg: Dict[str, Any] = {}
    if known.config:
        path = Path(expand_path(known.config))
        try:
            file_cfg = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SystemExit("配置文件不存在：%s" % path)
        except ValueError as exc:
            raise SystemExit("配置文件不是合法 JSON：%s（%s）" % (path, exc))
        if not isinstance(file_cfg, dict):
            raise SystemExit("配置文件必须是 JSON 对象：%s" % path)
    valid = {action.dest for action in parser._actions}
    unknown = sorted(set(file_cfg) - valid)
    if unknown:
        raise SystemExit("配置文件含未知字段：%s" % ", ".join(unknown))
    if file_cfg:
        parser.set_defaults(**file_cfg)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout 必须 > 0")
    if args.heartbeat_interval <= 0 or args.idle_heartbeat <= 0:
        raise SystemExit("心跳间隔必须 > 0")
    if args.idle_timeout <= 0:
        raise SystemExit("--idle-timeout 必须 > 0")
    args.router = str(args.router).rstrip("/")
    args.workspace = expand_path(args.workspace)
    args.token_file = expand_path(args.token_file)
    args.log_file = expand_path(args.log_file)
    return args


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #


class Worker:
    def __init__(self, cfg: argparse.Namespace, token: Optional[str],
                 dsh_bin: str) -> None:
        self.cfg = cfg
        self.token = token
        self.dsh_bin = dsh_bin
        self.stop = threading.Event()
        self.stop_signal: Optional[str] = None
        self._lock = threading.Lock()
        self._task_id: Optional[str] = None
        self._task_started = 0.0
        self._progress_at = 0.0
        self._output_bytes = 0
        self._recent: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._hb_last_error = 0.0
        self._stream_resp: Optional[Any] = None
        self._registered = threading.Event()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop,
                                           name="heartbeat", daemon=True)

    # -- 小工具 ---------------------------------------------------------- #

    def _url(self, path: str) -> str:
        return self.cfg.router + path

    def _on_signal(self, signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name
        if not self.stop.is_set():
            log.info("收到 %s，准备优雅退出", name)
        self.stop_signal = name
        self.stop.set()
        # 打断阻塞在 SSE 读上的主线程：关掉流，让 readline 立刻报错返回。
        resp = self._stream_resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _state(self) -> Tuple[Optional[str], float, float, int]:
        with self._lock:
            return (self._task_id, self._task_started,
                    self._progress_at, self._output_bytes)

    def _mark_task(self, task_id: Optional[str]) -> None:
        with self._lock:
            self._task_id = task_id
            now = time.time()
            self._task_started = now if task_id else 0.0
            self._progress_at = now
            self._output_bytes = 0

    def _touch(self, nbytes: int) -> None:
        with self._lock:
            if nbytes != self._output_bytes:
                self._output_bytes = nbytes
                self._progress_at = time.time()

    # -- 心跳 ------------------------------------------------------------ #

    def _heartbeat_payload(self) -> Dict[str, Any]:
        task_id, started, progress_at, nbytes = self._state()
        now = time.time()
        payload: Dict[str, Any] = {
            "name": self.cfg.name,
            "state": "running" if task_id else "idle",
            "progress_at": progress_at or now,
            "ts": now,
        }
        if task_id:
            payload.update({
                "task_id": task_id,
                "started_at": started,
                "elapsed_s": round(now - started, 1),
                "output_bytes": nbytes,
            })
        return payload

    def _send_heartbeat(self, quiet: bool = True) -> None:
        payload = self._heartbeat_payload()
        try:
            http_json(self._url("/heartbeat"), payload, self.token, timeout=5.0)
            log.debug("心跳 %s%s", payload["state"],
                      " task=%s" % payload.get("task_id") if payload.get("task_id") else "")
        except RouterError as exc:
            now = time.time()
            if not quiet or now - self._hb_last_error > 30:
                log.warning("心跳失败：%s", exc)
                self._hb_last_error = now

    def _heartbeat_loop(self) -> None:
        last_sent = time.time()
        while not self.stop.wait(0.2):
            if not self._registered.is_set():
                continue  # 还没注册上，Router 会回 unknown_worker
            task_id, _started, _progress, _nbytes = self._state()
            interval = (self.cfg.heartbeat_interval if task_id
                        else self.cfg.idle_heartbeat)
            now = time.time()
            if now - last_sent < interval:
                continue
            last_sent = now
            self._send_heartbeat()

    # -- Router 交互 ----------------------------------------------------- #

    def _register(self) -> Dict[str, Any]:
        body = {"name": self.cfg.name, "version": VERSION}
        info = http_json(self._url("/register"), body, self.token, timeout=15.0)
        if not isinstance(info, dict):
            info = {}
        self._registered.set()
        log.info("注册成功 name=%s queued=%s", self.cfg.name,
                 info.get("queued", "?"))
        return info

    def _open_stream(self) -> Any:
        query = urlencode({"name": self.cfg.name, "version": VERSION})
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "User-Agent": "dsh-worker/%s" % VERSION,
        }
        if self.token:
            headers["X-Router-Token"] = self.token
            headers["Authorization"] = "Bearer %s" % self.token
        req = Request(self._url("/stream") + "?" + query, headers=headers,
                      method="GET")
        try:
            self._stream_resp = _OPENER.open(req, timeout=READ_TIMEOUT_S)
            return self._stream_resp
        except HTTPError as exc:
            raise RouterError("SSE 打开失败 HTTP %s" % exc.code, status=exc.code)
        except URLError as exc:
            raise RouterError("SSE 打开失败：%s" % exc.reason)
        except (OSError, http.client.HTTPException) as exc:
            raise RouterError("SSE 打开失败：%s" % exc)

    def _connect_loop(self) -> None:
        delay = 1.0
        while not self.stop.is_set():
            started = time.time()
            got_task = False
            resp = None
            try:
                self._register()
                resp = self._open_stream()
                log.info("SSE 已连接（%s）", self.cfg.router)
                got_task = self._consume_stream(resp)
            except RouterError as exc:
                log.warning("连接中断：%s", exc)
            except (OSError, http.client.HTTPException) as exc:
                log.warning("连接中断：%s", exc)
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                    self._stream_resp = None
            if self.stop.is_set():
                break
            self._registered.clear()  # 连接断了，Router 那边已判离线
            lived = time.time() - started
            if got_task or lived >= 15.0:
                delay = 1.0
            log.warning("与 Router 断开，%s 后重连", fmt_duration(delay))
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, 30.0)

    def _consume_stream(self, resp: Any) -> bool:
        got_task = False
        for event, data in iter_sse(resp, self.stop, self.cfg.idle_timeout):
            task = extract_task(event, data)
            if task is None:
                continue
            got_task = True
            self.execute(task)
            if self.stop.is_set():
                break
        return got_task

    # -- 执行 ------------------------------------------------------------ #

    def _report(self, payload: Dict[str, Any]) -> bool:
        url = self._url("/result")
        for attempt in range(1, 4):
            try:
                http_json(url, payload, self.token, timeout=10.0)
                log.info("结果已上报 task=%s status=%s",
                         payload.get("task_id"), payload.get("status"))
                return True
            except RouterError as exc:
                log.warning("结果上报失败（%d/3）：%s", attempt, exc)
                if attempt < 3:
                    if self.stop.wait(1.0 * attempt):
                        break
        return False

    def _remember(self, task_id: str, payload: Dict[str, Any]) -> None:
        self._recent[task_id] = payload
        while len(self._recent) > RESULT_CACHE_SIZE:
            self._recent.popitem(last=False)

    def _fail(self, task_id: str, summary: str,
              evidence: Dict[str, Any]) -> None:
        payload = {
            "name": self.cfg.name,
            "task_id": task_id,
            "status": "failed",
            "summary": tail_text(summary, SUMMARY_LIMIT),
            "artifacts": [],
            "evidence": [evidence],
        }
        self._remember(task_id, payload)
        self._report(payload)

    def execute(self, task: Dict[str, Any]) -> None:
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        if not task_id:
            log.warning("任务没有 task_id，丢弃")
            return
        if self.stop.is_set():
            self._fail(task_id, "worker 正在退出（%s），任务未执行"
                       % (self.stop_signal or "stop"),
                       {"type": "exit", "reason": "interrupted"})
            return
        cached = self._recent.get(task_id)
        if cached is not None:
            log.info("任务 %s 已处理过，重发缓存结果（不重复执行）", task_id)
            self._report(cached)
            return

        goal = str(task.get("goal") or task.get("task")
                   or task.get("prompt") or "").strip()
        context = str(task.get("context") or "").strip()
        if not goal:
            self._fail(task_id, "任务缺少 goal，未执行",
                       {"type": "exit", "reason": "bad_task"})
            return

        workspace_raw = task.get("workspace") or self.cfg.workspace
        workspace = os.path.abspath(expand_path(str(workspace_raw)))
        try:
            os.makedirs(workspace, exist_ok=True)
        except OSError as exc:
            self._fail(task_id, "工作目录不可用：%s（%s）" % (workspace, exc),
                       {"type": "exit", "reason": "bad_workspace"})
            return

        prompt = build_prompt(goal, context)
        use_stdin = prompt.startswith("-") or len(prompt) > 200_000
        cmd = [self.dsh_bin, "--profile", self.cfg.dsh_profile,
               "--permission-mode", self.cfg.permission_mode]
        if self.cfg.approval_policy:
            cmd += ["--approval-policy", self.cfg.approval_policy]
        cmd.append("-" if use_stdin else prompt)

        log.info("开始任务 %s：workspace=%s timeout=%s chars=%d",
                 task_id, workspace, fmt_duration(self.cfg.timeout), len(prompt))
        self._mark_task(task_id)
        self._send_heartbeat()  # 立即宣告 running，不等第一个间隔

        started = time.time()
        tmpdir = tempfile.mkdtemp(prefix="dsh-worker-")
        out_path = os.path.join(tmpdir, "stdout.log")
        err_path = os.path.join(tmpdir, "stderr.log")
        proc: Optional[subprocess.Popen] = None
        timed_out = False
        interrupted = False
        rc: Optional[int] = None
        try:
            with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
                proc = subprocess.Popen(
                    cmd, cwd=workspace,
                    stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
                    stdout=out_f, stderr=err_f,
                    start_new_session=True, close_fds=True)
                if use_stdin and proc.stdin is not None:
                    try:
                        proc.stdin.write(prompt.encode("utf-8"))
                        proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                deadline = started + self.cfg.timeout
                while proc.poll() is None:
                    if self.stop.is_set():
                        interrupted = True
                        break
                    if time.time() >= deadline:
                        timed_out = True
                        break
                    self._touch(self._output_size(out_path, err_path))
                    time.sleep(0.2)
                if timed_out or interrupted:
                    self._terminate_group(proc, grace=1.0)
                try:
                    rc = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._terminate_group(proc, grace=0.0)
                    rc = proc.wait(timeout=5)
                self._touch(self._output_size(out_path, err_path))
        except FileNotFoundError as exc:
            self._mark_task(None)
            self._fail(task_id, "dsh 启动失败：%s" % exc,
                       {"type": "exit", "reason": "spawn_error"})
            shutil.rmtree(tmpdir, ignore_errors=True)
            return
        except OSError as exc:
            self._mark_task(None)
            self._fail(task_id, "dsh 运行失败：%s" % exc,
                       {"type": "exit", "reason": "spawn_error"})
            shutil.rmtree(tmpdir, ignore_errors=True)
            return

        stdout = self._read_text(out_path)
        stderr = self._read_text(err_path)
        elapsed_ms = int((time.time() - started) * 1000)
        self._mark_task(None)

        if timed_out:
            note = ("超时：dsh 超过 %s 未完成（task %s），已终止进程"
                    % (fmt_duration(self.cfg.timeout), task_id))
            summary = compose_summary(note, stdout, stderr)
            payload = {
                "name": self.cfg.name,
                "task_id": task_id,
                "status": "failed",
                "summary": summary,
                "artifacts": [],
                "evidence": [{"type": "exit", "reason": "timeout",
                              "timeout_s": self.cfg.timeout,
                              "duration_ms": elapsed_ms}],
            }
            log.warning("任务 %s 超时（%ss）", task_id, fmt_duration(self.cfg.timeout))
        elif interrupted:
            note = ("worker 收到 %s，任务中断"
                    % (self.stop_signal or "停止信号"))
            summary = compose_summary(note, stdout, stderr)
            payload = {
                "name": self.cfg.name,
                "task_id": task_id,
                "status": "failed",
                "summary": summary,
                "artifacts": [],
                "evidence": [{"type": "exit", "reason": "interrupted",
                              "signal": self.stop_signal,
                              "duration_ms": elapsed_ms}],
            }
            log.warning("任务 %s 因退出信号中断", task_id)
        elif rc == 0:
            summary = compose_summary("", stdout, "", SUMMARY_LIMIT)
            if not summary:
                summary = "（dsh 无输出）"
            payload = {
                "name": self.cfg.name,
                "task_id": task_id,
                "status": "done",
                "summary": summary,
                "artifacts": [],
                "evidence": [{"type": "exit", "code": 0,
                              "duration_ms": elapsed_ms}],
            }
            log.info("任务 %s 完成，用时 %dms", task_id, elapsed_ms)
        else:
            note = "dsh 退出码 %s" % rc
            summary = compose_summary(note, stdout, stderr)
            payload = {
                "name": self.cfg.name,
                "task_id": task_id,
                "status": "failed",
                "summary": summary,
                "artifacts": [],
                "evidence": [{"type": "exit", "code": rc,
                              "duration_ms": elapsed_ms}],
            }
            log.warning("任务 %s 失败，退出码 %s", task_id, rc)

        self._remember(task_id, payload)
        self._report(payload)
        shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _read_text(path: str) -> str:
        try:
            with open(path, "rb") as fh:
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    @staticmethod
    def _output_size(*paths: str) -> int:
        total = 0
        for path in paths:
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return total

    @staticmethod
    def _terminate_group(proc: subprocess.Popen, grace: float) -> None:
        """先 SIGTERM 整个进程组，grace 秒后还在就 SIGKILL。"""
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        if grace > 0:
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    # -- 入口 ------------------------------------------------------------ #

    def run(self) -> int:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)
        self._hb_thread.start()
        try:
            self._connect_loop()
        finally:
            self.stop.set()
            self._hb_thread.join(timeout=3.0)
            log.info("dsh-worker 退出（signal=%s）", self.stop_signal or "none")
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    cfg = parse_args(argv)
    setup_logging(cfg.log_file, cfg.verbose)
    log.info("dsh-worker %s 启动 router=%s name=%s workspace=%s mode=%s",
             VERSION, cfg.router, cfg.name, cfg.workspace,
             cfg.permission_mode)
    dsh_bin = resolve_dsh(cfg.dsh_bin)
    if not dsh_bin:
        log.error("找不到 dsh 可执行文件：%s", cfg.dsh_bin)
        print("dsh-worker: 找不到 dsh 可执行文件：%s" % cfg.dsh_bin,
              file=sys.stderr)
        return 2
    token = read_token(cfg.token_file)
    return Worker(cfg, token, dsh_bin).run()


if __name__ == "__main__":
    sys.exit(main())
