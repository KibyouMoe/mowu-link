#!/usr/bin/env python3
"""dsh worker — 从 Router 领活，用 ``dsh --profile headless`` 执行，把结果交回去。

协议见 docs/ROUTER.md §二、§五：

    POST /register   {name, version}                        → {ok, queued}
    GET  /stream     ?name=<name>                            → SSE 下行（任务）
    POST /heartbeat  {name, state, progress_at, task_id?}
    POST /result     {name, task_id, status, summary, artifacts[], evidence[]}
    POST /notice     {worker, text, urgency}                 → 飞书入向转给 Hermes
    POST /subscriptions {worker, source}                     → 声明订阅
    GET  /subscriptions/pending?worker=                      → 拉取订阅消息

行为约定：

- **串行**：一次只跑一个任务，跑完才读下一条（Router 按名字排队）。
- **重连**：SSE 断线后 1s→2s→4s→8s→16s→30s 封顶；每次重连先重新 /register。
- **看门狗**：任务期间超过 --stall-seconds 既无输出也无 CPU 变化 → 飞书告警、
  杀进程组、result 报 failed/stalled；SSE 超过 --idle-reconnect 无字节则重连。
- **飞书**：长连接收训练员私信。``chat_enabled`` 时直接跟 agent 聊天（持久
  session、流式进度、排队）；否则按旧行为转 /notice(urgent) + 回确认。
  任务开始/完成/失败/卡死主动发私信（--no-feishu 可关）。
- **聊天**：每个 open_id 一条 session（chat-state.json 原子写）；同一 worker
  一次只跑一个 agent，Router 任务优先、聊天排队（最多 3 条）；每条 in/out
  旁听 POST /chat/log（失败不影响聊天）。
- **订阅**：--subscribe 声明订阅，轮询 pending 落盘 subscription-feed.jsonl，
  飞书按 60 秒窗口汇总（--subscribe 为空则完全不启动）。
- **心跳**：独立线程按间隔上报；任务运行中 state=running 并带 task_id/progress_at。
- **退出**：SIGTERM/SIGINT 停心跳、杀子进程组、把当前任务以 failed/interrupted
  上报（能报就报），然后退出 0。
- **日志**：~/.hermes/logs/dsh-worker.log，只追加，不轮转。

只用标准库，兼容 /usr/bin/python3 (3.9)。
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
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

# 共用 SDK 在 worker/_sdk（dsh 与 codex 同构接入）。
_WORKER_ROOT = str(Path(__file__).resolve().parent.parent)
if _WORKER_ROOT not in sys.path:
    sys.path.insert(0, _WORKER_ROOT)

from _sdk import chat as chat_sdk  # noqa: E402
from _sdk.feishu import (  # noqa: E402
    DEFAULT_TRAINER_OPEN_ID, FeishuBridge, FeishuError, load_creds)
from _sdk.subscriptions import FeedNotifier, SubscriptionPoller  # noqa: E402
from _sdk.watchdog import StallWatchdog, terminate_group  # noqa: E402

VERSION = "0.2.0"
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
DEFAULT_FEISHU_CREDS = "~/.hermes/secrets/feishu-tsurumaru.json"
DEFAULT_STALL_S = 300.0
DEFAULT_SUBSCRIBE_INTERVAL_S = 15.0
DEFAULT_CHAT_TIMEOUT_S = 300.0
DEFAULT_CHAT_QUEUE = 3
DEFAULT_CHAT_STREAM_INTERVAL_S = 1.6
DEFAULT_CHAT_STATE_FILE = "~/.hermes/state/dsh-chat-state.json"
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
    p.add_argument("--idle-timeout", "--idle-reconnect", dest="idle_timeout",
                   type=float, default=DEFAULT_IDLE_TIMEOUT_S,
                   help="SSE 静默多久（无任何字节）判定断线并重连（默认 90）")
    p.add_argument("--stall-seconds", type=float, default=DEFAULT_STALL_S,
                   help="任务期间多久无 stdout 且无 CPU 变化判定卡死（默认 300）")
    p.add_argument("--feishu-creds", default=DEFAULT_FEISHU_CREDS,
                   help="飞书凭据 JSON（默认 玉藻十字）")
    p.add_argument("--feishu-open-id", default="",
                   help="训练员 open_id；空=用凭据文件里的 open_id")
    p.add_argument("--no-feishu", action="store_true",
                   help="不连飞书（自测/排障用）")
    p.add_argument("--chat", dest="chat_enabled",
                   action=argparse.BooleanOptionalAction, default=False,
                   help="飞书私信直接跟 agent 聊天（--no-chat 关闭；worker.json 用 chat_enabled）")
    p.add_argument("--chat-state", default=DEFAULT_CHAT_STATE_FILE,
                   help="聊天 session 状态文件（默认 %s）" % DEFAULT_CHAT_STATE_FILE)
    p.add_argument("--chat-timeout", type=float, default=DEFAULT_CHAT_TIMEOUT_S,
                   help="单轮聊天超时秒数（默认 %s）" % DEFAULT_CHAT_TIMEOUT_S)
    p.add_argument("--chat-queue", type=int, default=DEFAULT_CHAT_QUEUE,
                   help="忙碌时聊天最多排队条数（默认 %d）" % DEFAULT_CHAT_QUEUE)
    p.add_argument("--chat-stream-interval", type=float,
                   default=DEFAULT_CHAT_STREAM_INTERVAL_S,
                   help="流式进度节流间隔秒数（默认 %.1f）"
                        % DEFAULT_CHAT_STREAM_INTERVAL_S)
    p.add_argument("--subscribe", default="",
                   help="启动后订阅的 source，逗号分隔，如 mc_chat,email")
    p.add_argument("--subscribe-interval", type=float,
                   default=DEFAULT_SUBSCRIBE_INTERVAL_S,
                   help="订阅 pending 轮询间隔秒数（默认 15）")
    p.add_argument("--subscription-feed", default="",
                   help="订阅消息落盘文件；空=~/.hermes/logs/<name>-subscription-feed.jsonl")
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
        # --idle-reconnect 是 --idle-timeout 的别名；配置文件两个键都认。
        if "idle_reconnect" in file_cfg:
            file_cfg["idle_timeout"] = file_cfg.pop("idle_reconnect")
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
    if args.stall_seconds <= 0:
        raise SystemExit("--stall-seconds 必须 > 0")
    if args.subscribe_interval <= 0:
        raise SystemExit("--subscribe-interval 必须 > 0")
    if args.chat_timeout <= 0:
        raise SystemExit("--chat-timeout 必须 > 0")
    if args.chat_queue < 1:
        raise SystemExit("--chat-queue 必须 >= 1")
    if args.chat_stream_interval <= 0:
        raise SystemExit("--chat-stream-interval 必须 > 0")
    args.router = str(args.router).rstrip("/")
    args.workspace = expand_path(args.workspace)
    args.token_file = expand_path(args.token_file)
    args.log_file = expand_path(args.log_file)
    args.feishu_creds = expand_path(args.feishu_creds)
    args.chat_state = expand_path(args.chat_state)
    raw_subscribe = args.subscribe
    if isinstance(raw_subscribe, (list, tuple)):
        parts = [str(item) for item in raw_subscribe]
    else:
        parts = str(raw_subscribe or "").split(",")
    args.subscribe_sources = [part.strip() for part in parts if part.strip()]
    if not args.subscription_feed:
        args.subscription_feed = expand_path(
            "~/.hermes/logs/%s-subscription-feed.jsonl" % args.name)
    else:
        args.subscription_feed = expand_path(args.subscription_feed)
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
        self.feishu: Optional[FeishuBridge] = None
        self.chat: Optional[chat_sdk.ChatService] = None
        self.subs: Optional[SubscriptionPoller] = None
        if self.cfg.subscribe_sources:
            self.subs = SubscriptionPoller(
                worker=self.cfg.name,
                sources=self.cfg.subscribe_sources,
                post_json=lambda path, payload: http_json(
                    self._url(path), payload, self.token, timeout=10.0),
                get_json=lambda path: http_json(
                    self._url(path), None, self.token, timeout=10.0),
                feed_path=self.cfg.subscription_feed,
                notifier=FeedNotifier(self._feishu_send, logger=log),
                interval=self.cfg.subscribe_interval,
                logger=log,
            )

    # -- 小工具 ---------------------------------------------------------- #

    def _url(self, path: str) -> str:
        return self.cfg.router + path

    # -- 飞书 / notice --------------------------------------------------- #

    def _feishu_send(self, text: str) -> bool:
        bridge = self.feishu
        if bridge is None:
            return False
        try:
            return bridge.send_text(text)
        except Exception:  # noqa: BLE001 - 通知失败不能影响主流程
            log.exception("飞书通知失败")
            return False

    def _notify_router(self, text: str, urgency: str = "urgent") -> bool:
        """训练员的临时要求 → Router /notice（event 级，唤醒 Hermes）。"""
        payload = {
            "worker": self.cfg.name,
            "text": str(text or ""),
            "urgency": urgency,
        }
        try:
            http_json(self._url("/notice"), payload, self.token, timeout=8.0)
            log.info("已转 /notice（%s）：%s", urgency, str(text)[:160])
            return True
        except RouterError as exc:
            log.warning("/notice 上报失败：%s", exc)
            return False

    def _on_feishu_text(self, text: str) -> bool:
        """飞书入向回调：只负责转 Router，确认语由 FeishuBridge 发。"""
        text = (text or "").strip()
        if not text:
            return False
        return self._notify_router(text, "urgent")

    def _announce(self, text: str) -> None:
        # 退出流程里不再发消息，避免网络慢拖长优雅退出。
        # 出向汇报统一走 Router /notice → trigger → 溪流转达给训练员。
        # 不直连飞书：跨 app 的 open_id 发不出去，架构上 worker 也不持有飞书出口。
        if self.stop.is_set():
            return
        self._notify_router(text, "normal")

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
        # 订阅是“实时听”，不累积：每次（重）连都重新声明。
        if self.subs is not None:
            self.subs.subscribe_all()
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
        """Router 任务入口：跟飞书聊天互斥（同一 worker 一次只跑一个 agent）。"""
        service = self.chat
        if service is None:
            self._execute(task)
            return
        service.task_begin()
        try:
            self._execute(task)
        finally:
            service.task_end()

    def _execute(self, task: Dict[str, Any]) -> None:
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
        self._announce("来自超级溪流的指令（任务 %s）\n%s"
                       % (task_id, goal[:4000]))

        started = time.time()
        tmpdir = tempfile.mkdtemp(prefix="dsh-worker-")
        out_path = os.path.join(tmpdir, "stdout.log")
        err_path = os.path.join(tmpdir, "stderr.log")
        proc: Optional[subprocess.Popen] = None
        timed_out = False
        interrupted = False
        stalled = False
        stall_idle = 0.0
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
                watchdog = StallWatchdog(
                    proc,
                    output_size=lambda: self._output_size(out_path),
                    stall_seconds=self.cfg.stall_seconds,
                    logger=log)
                deadline = started + self.cfg.timeout
                while proc.poll() is None:
                    if self.stop.is_set():
                        interrupted = True
                        break
                    if time.time() >= deadline:
                        timed_out = True
                        break
                    self._touch(self._output_size(out_path, err_path))
                    idle = watchdog.check()
                    if idle is not None:
                        stalled = True
                        stall_idle = idle
                        log.warning("任务 %s 卡死：%.0fs 无输出且 CPU 无变化，"
                                    "杀进程组", task_id, idle)
                        self._announce(
                            "⚠️ %s 任务 %s 卡死：%.0fs 无输出且 CPU 无变化，"
                            "已杀进程" % (self.cfg.name, task_id, idle))
                        watchdog.kill()
                        break
                    time.sleep(0.2)
                if timed_out or interrupted or stalled:
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

        if stalled:
            note = "stalled: no output for %ds" % int(round(stall_idle))
            summary = note
            payload = {
                "name": self.cfg.name,
                "task_id": task_id,
                "status": "failed",
                "summary": summary,
                "artifacts": [],
                "evidence": [{"type": "exit", "reason": "stalled",
                              "stall_s": self.cfg.stall_seconds,
                              "idle_s": round(stall_idle, 1),
                              "duration_ms": elapsed_ms}],
            }
            log.warning("任务 %s 判定卡死（%.0fs 无输出/无 CPU 变化）",
                        task_id, stall_idle)
        elif timed_out:
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
        # 完成/失败不再单独 announce：router 的 task.result notify 已经覆盖，
        # 多发一条就是重复。
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
        """先 SIGTERM 整个进程组，grace 秒后还在就 SIGKILL（SDK 共用实现）。"""
        terminate_group(proc, grace)

    # -- 入口 ------------------------------------------------------------ #

    def run(self) -> int:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)
        if self.chat is not None:
            self.chat.start(self.stop)
        if self.feishu is not None:
            try:
                self.feishu.start()
            except Exception:  # noqa: BLE001 - 飞书坏了也要能干活
                log.exception("飞书桥启动失败（不影响任务执行）")
        if self.subs is not None:
            self.subs.start(self.stop)
        self._hb_thread.start()
        try:
            self._connect_loop()
        finally:
            self.stop.set()
            if self.subs is not None:
                self.subs.stop()
            if self.feishu is not None:
                self.feishu.stop()
            if self.chat is not None:
                self.chat.stop()
            self._hb_thread.join(timeout=3.0)
            log.info("dsh-worker 退出（signal=%s）", self.stop_signal or "none")
        return 0


def build_feishu_bridge(cfg: argparse.Namespace,
                        on_text: Any) -> Optional[FeishuBridge]:
    """按配置建飞书桥；缺凭据/显式关闭都只返回 None，不阻断 worker。"""
    if getattr(cfg, "no_feishu", False) or not getattr(cfg, "feishu_creds", ""):
        log.info("飞书同步未启用（--no-feishu 或未配置凭据）")
        return None
    if importlib.util.find_spec("lark_oapi") is None:
        log.warning("飞书同步未启用：当前解释器没装 lark_oapi（%s）",
                    sys.executable)
        return None
    creds = load_creds(cfg.feishu_creds)
    open_id = (getattr(cfg, "feishu_open_id", "") or creds.open_id
               or DEFAULT_TRAINER_OPEN_ID)
    log.info("飞书同步启用 bot=%s open_id=%s",
             creds.bot_name or creds.app_id, open_id)
    return FeishuBridge(creds, open_id, on_text, logger=log)


def build_chat_service(cfg: argparse.Namespace, bridge: FeishuBridge,
                       instance: "Worker",
                       dsh_bin: str) -> Optional[chat_sdk.ChatService]:
    """按配置建聊天服务；chat_enabled=false 或缺飞书桥都返回 None。"""
    if not getattr(cfg, "chat_enabled", False):
        log.info("飞书聊天未启用（chat_enabled=false）")
        return None
    store = chat_sdk.ChatStateStore(cfg.chat_state, logger=log)
    env = dict(os.environ)
    # dsh 的 deepseek 路由必须走 /anthropic（start-worker.sh 已设，这里兜底）。
    env.setdefault("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
    agent = chat_sdk.DshAgent(
        dsh_bin, profile=cfg.dsh_profile,
        permission_mode=cfg.permission_mode,
        approval_policy=cfg.approval_policy,
        timeout=cfg.chat_timeout, workspace=cfg.workspace,
        env=env, logger=log)
    service = chat_sdk.ChatService(
        worker_name=cfg.name, bridge=bridge, agent=agent, store=store,
        post_log=lambda payload: http_json(
            instance._url("/chat/log"), payload, instance.token, timeout=5.0),
        queue_max=cfg.chat_queue, stream_interval=cfg.chat_stream_interval,
        logger=log)
    # 聊天模式：确认语/进度/回复都由 ChatService 负责，不再走 /notice 确认。
    bridge.on_chat = service.handle_text
    bridge.on_text = None
    log.info("飞书聊天启用 state=%s timeout=%ss queue=%d interval=%.1fs",
             cfg.chat_state, cfg.chat_timeout, cfg.chat_queue,
             cfg.chat_stream_interval)
    return service


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
    instance = Worker(cfg, token, dsh_bin)
    try:
        instance.feishu = build_feishu_bridge(cfg, instance._on_feishu_text)
        if instance.feishu is not None:
            instance.chat = build_chat_service(cfg, instance.feishu,
                                               instance, dsh_bin)
    except FeishuError as exc:
        log.warning("飞书同步未启用：%s", exc)
    return instance.run()


if __name__ == "__main__":
    sys.exit(main())
