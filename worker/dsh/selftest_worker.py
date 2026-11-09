#!/usr/bin/env python3
"""dsh worker 自测：假 Router（本地 HTTP）+ 假 dsh（临时 shell 脚本），不调用真模型。

    ./selftest_worker.py

覆盖：注册、收任务、结果字段、心跳、4000 字截尾、失败退出码、超时、串行不重叠、
断线重连并重新注册、SIGTERM 优雅退出、重复任务去重、配置文件加载、语法、纯函数，
以及 SSE 空闲/心跳不误判断线、真断才重连；聊天（假 agent + 假 Router）覆盖
session 持久化/续聊、流式节流、排队与超额拒绝、/chat/log、失败与超时重建、
1500 字分条、任务/聊天互斥、旁听失败不影响聊天。
目标：60 秒内跑完并退出，不留常驻进程。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import worker  # noqa: E402
from _sdk import chat as chat_sdk  # noqa: E402
from _sdk import feishu as feishu_sdk  # noqa: E402
from _sdk import subscriptions as subs_sdk  # noqa: E402
from _sdk import watchdog as watchdog_sdk  # noqa: E402

worker.log.propagate = False
worker.log.addHandler(logging.NullHandler())
for _logger_name in ("mowu-worker.feishu", "mowu-worker.watchdog",
                     "mowu-worker.subscriptions"):
    _logger = logging.getLogger(_logger_name)
    _logger.propagate = False
    _logger.addHandler(logging.NullHandler())

TESTS: List[Callable[[], None]] = []
GLOBAL_LIMIT_S = 120.0
PER_TEST_LIMIT_S = 35.0

FAKE_DSH = """#!/bin/sh
# selftest 用的假 dsh：回显参数，按 FAKE_DSH_MODE 改变行为
PY=@PY@
mode="${FAKE_DSH_MODE:-ok}"
trace="${FAKE_DSH_TRACE:-}"
stamp() { "$PY" -c 'import time;print("%.3f" % time.time())'; }
if [ -n "$trace" ]; then printf 'start %s\\n' "$(stamp)" >> "$trace"; fi
rc=0
case "$mode" in
  sleep)
    sleep "${FAKE_DSH_SLEEP:-2}"
    echo "fake-dsh: slow done"
    ;;
  fail)
    echo "fake-dsh: partial output before failure"
    echo "fake-dsh: boom on stderr" >&2
    rc=3
    ;;
  stall)
    sleep "${FAKE_DSH_SLEEP:-10}"
    echo "fake-dsh: should not print before watchdog kills"
    ;;
  big)
    echo "fake-dsh: big output"
    "$PY" -c 'import sys; sys.stdout.write("A"*4800 + "TAIL-MARKER-0123456789\\n")'
    ;;
  stdin)
    cat
    ;;
  *)
    echo "fake-dsh: args=$*"
    echo "fake-dsh: done"
    ;;
esac
if [ -n "$trace" ]; then printf 'end %s\\n' "$(stamp)" >> "$trace"; fi
exit $rc
"""

FAKE_CHAT_DSH = """#!/bin/sh
# 聊天自测用的假 dsh：输出与 ``dsh --json`` 同构的 NDJSON
PY=@PY@
mode="${FAKE_CHAT_MODE:-ok}"
trace="${FAKE_CHAT_TRACE:-}"
if [ -n "$trace" ]; then printf '%s\\n' "$*" >> "$trace"; fi

sid=""
prev=""
for a in "$@"; do
    if [ "$prev" = "--session-id" ]; then sid="$a"; fi
    prev="$a"
done
if [ -z "$sid" ]; then sid="${FAKE_CHAT_SID:-session-chat-1}"; fi

printf '{"type":"session","sessionId":"%s","cwd":"."}\\n' "$sid"
printf '{"type":"status","phase":"turn_start","turn":1}\\n'

case "$mode" in
  fail)
    printf '{"type":"text","text":"准备失败"}\\n'
    echo "fake-chat-dsh: boom-boom" >&2
    exit 3
    ;;
  slow)
    "$PY" -c 'import os,time;time.sleep(float(os.environ.get("FAKE_CHAT_SLEEP","0.6")))'
    ;;
  stream)
    i=1
    while [ "$i" -le 6 ]; do
      printf '{"type":"text","text":"第 %s 段进度"}\\n' "$i"
      printf '{"type":"tool_call","callId":"c%s","tool":"bash","input":{"description":"step %s","command":"echo %s"}}\\n' "$i" "$i" "$i"
      printf '{"type":"tool_result","callId":"c%s","status":"completed","result":"ok"}\\n' "$i"
      "$PY" -c 'import time;time.sleep(0.12)'
      i=$((i+1))
    done
    ;;
esac

printf '{"type":"text","text":"%s"}\\n' "${FAKE_CHAT_REPLY:-第一轮回答}"
printf '{"type":"final","sessionId":"%s","reason":{"kind":"completed"},"text":"%s"}\\n' "$sid" "${FAKE_CHAT_REPLY:-第一轮回答}"
exit 0
"""


def need(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test(fn: Callable[[], None]) -> Callable[[], None]:
    TESTS.append(fn)
    return fn


def make_task(task_id: str = "t-1", goal: str = "hello-from-router",
              context: str = "ctx-abc", **extra: Any) -> Dict[str, Any]:
    task = {"task_id": task_id, "goal": goal, "context": context}
    task.update(extra)
    return task


# --------------------------------------------------------------------------- #
# 假 Router
# --------------------------------------------------------------------------- #


class _QuietHTTPServer(ThreadingHTTPServer):
    """跳过 getfqdn 的反向 DNS：macOS 上偶尔会卡 20 秒以上。"""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host or "127.0.0.1")
        self.server_port = port


class FakeRouter:
    def __init__(self, tasks: Optional[List[Dict[str, Any]]] = None,
                 drop_first_stream: bool = False,
                 stream_mode: str = "ping",
                 ping_interval: float = 0.3,
                 hold_s: float = 6.0,
                 pending_messages: Optional[List[Dict[str, Any]]] = None) -> None:
        self.tasks = list(tasks or [])
        self.drop_first_stream = drop_first_stream
        # ping: 建立后每 ping_interval 秒发 `: ping`（默认，老用例行为）
        # silent: 建立后一个字节都不发
        # slow_ping: 每 ping_interval 秒发一次，持续 hold_s 秒，然后安静
        self.stream_mode = stream_mode
        self.ping_interval = ping_interval
        self.hold_s = hold_s
        self.pings_sent = 0
        self.registrations: List[Dict[str, Any]] = []
        self.heartbeats: List[Dict[str, Any]] = []
        self.results: List[Dict[str, Any]] = []
        self.notices: List[Dict[str, Any]] = []
        self.chat_logs: List[Dict[str, Any]] = []
        self.subscriptions: List[Dict[str, Any]] = []
        self.pending_messages: List[Dict[str, Any]] = list(pending_messages or [])
        self.pending_requests: List[str] = []
        self.stream_opens = 0
        self.stream_auth: List[Optional[str]] = []
        self.stream_xtoken: List[Optional[str]] = []
        self.closed = False
        self.cond = threading.Condition()
        self.httpd = _QuietHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="fake-router", daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    # -- HTTP ------------------------------------------------------------ #

    def _make_handler(self) -> type:
        router = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _fmt: str, *args: Any) -> None:
                pass

            def _read_body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    value = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    return {}
                return value if isinstance(value, dict) else {}

            def _json(self, code: int, obj: Any) -> None:
                payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                body = self._read_body()
                auth = self.headers.get("Authorization")
                xtoken = self.headers.get("X-Router-Token")
                if path == "/register":
                    with router.cond:
                        router.registrations.append(
                            {"ts": time.time(), "body": body, "auth": auth,
                             "xtoken": xtoken})
                        router.cond.notify_all()
                    self._json(200, {"ok": True, "queued": len(router.tasks)})
                elif path == "/heartbeat":
                    with router.cond:
                        router.heartbeats.append(
                            {"ts": time.time(), "body": body, "auth": auth,
                             "xtoken": xtoken})
                        router.cond.notify_all()
                    self._json(200, {"ok": True})
                elif path == "/result":
                    with router.cond:
                        router.results.append(
                            {"ts": time.time(), "body": body, "auth": auth,
                             "xtoken": xtoken})
                        router.cond.notify_all()
                    self._json(200, {"ok": True})
                elif path == "/notice":
                    with router.cond:
                        router.notices.append(
                            {"ts": time.time(), "body": body, "auth": auth,
                             "xtoken": xtoken})
                        router.cond.notify_all()
                    self._json(200, {"ok": True})
                elif path == "/chat/log":
                    with router.cond:
                        router.chat_logs.append(
                            {"ts": time.time(), "path": path, "body": body,
                             "auth": auth, "xtoken": xtoken})
                        router.cond.notify_all()
                    self._json(200, {"ok": True})
                elif path == "/subscriptions":
                    with router.cond:
                        router.subscriptions.append(
                            {"ts": time.time(), "body": body, "auth": auth,
                             "xtoken": xtoken})
                        router.cond.notify_all()
                    self._json(200, {"ok": True, "id": len(router.subscriptions)})
                else:
                    self._json(404, {"error": "not_found"})

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/subscriptions/pending":
                    with router.cond:
                        pending = list(router.pending_messages)
                        router.pending_messages.clear()
                        router.pending_requests.append(self.path)
                        router.cond.notify_all()
                    self._json(200, {"messages": pending})
                    return
                if parsed.path != "/stream":
                    self._json(404, {"error": "not_found"})
                    return
                router._serve_stream(self)

        return Handler

    def _serve_stream(self, handler: BaseHTTPRequestHandler) -> None:
        with self.cond:
            self.stream_opens += 1
            index = self.stream_opens
            self.stream_auth.append(handler.headers.get("Authorization"))
            self.stream_xtoken.append(handler.headers.get("X-Router-Token"))
            self.cond.notify_all()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        try:
            if self.drop_first_stream and index == 1:
                time.sleep(0.2)  # 第一次连接：什么都不发就断
                handler.wfile.flush()
                return
            with self.cond:
                pending = list(self.tasks)
                self.tasks.clear()
            for task in pending:
                frame = ("event: task\ndata: %s\n\n"
                         % json.dumps(task, ensure_ascii=False)).encode("utf-8")
                handler.wfile.write(frame)
                handler.wfile.flush()
                time.sleep(0.05)
            if self.stream_mode == "silent":
                while not self.closed:
                    time.sleep(0.1)
                return
            if self.stream_mode == "slow_ping":
                deadline = time.time() + self.hold_s
                while not self.closed and time.time() < deadline:
                    handler.wfile.write(b": ping\n\n")
                    handler.wfile.flush()
                    with self.cond:
                        self.pings_sent += 1
                        self.cond.notify_all()
                    time.sleep(self.ping_interval)
                while not self.closed:
                    time.sleep(0.1)
                return
            while not self.closed:
                handler.wfile.write(b": ping\n\n")
                handler.wfile.flush()
                with self.cond:
                    self.pings_sent += 1
                time.sleep(self.ping_interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # -- 等待 ------------------------------------------------------------ #

    def _wait_count(self, attr: str, count: int, timeout: float) -> bool:
        end = time.time() + timeout
        with self.cond:
            while len(getattr(self, attr)) < count:
                remaining = end - time.time()
                if remaining <= 0:
                    return False
                self.cond.wait(min(0.1, remaining))
        return True

    def wait_registrations(self, count: int, timeout: float = 12.0) -> bool:
        return self._wait_count("registrations", count, timeout)

    def wait_results(self, count: int, timeout: float = 12.0) -> bool:
        return self._wait_count("results", count, timeout)

    def wait_notices(self, count: int, timeout: float = 12.0) -> bool:
        return self._wait_count("notices", count, timeout)

    def wait_chat_logs(self, count: int, timeout: float = 12.0) -> bool:
        return self._wait_count("chat_logs", count, timeout)

    def wait_subscriptions(self, count: int, timeout: float = 12.0) -> bool:
        return self._wait_count("subscriptions", count, timeout)

    def wait_streams(self, count: int = 1, timeout: float = 12.0) -> bool:
        end = time.time() + timeout
        with self.cond:
            while self.stream_opens < count:
                remaining = end - time.time()
                if remaining <= 0:
                    return False
                self.cond.wait(min(0.1, remaining))
        return True

    def close(self) -> None:
        self.closed = True
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        self.thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# worker 子进程 / 沙箱
# --------------------------------------------------------------------------- #


class Sandbox:
    _counter = 0

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="dsh-selftest-"))
        self.ws = self.dir / "ws"
        self.ws.mkdir()
        self.trace = self.dir / "trace.log"
        self.fake_dsh = self.dir / "fake-dsh"
        self.fake_dsh.write_text(
            FAKE_DSH.replace("@PY@", shlex.quote(sys.executable)),
            encoding="utf-8")
        self.fake_dsh.chmod(0o755)
        self.fake_chat = self.dir / "fake-chat-dsh"
        self.fake_chat.write_text(
            FAKE_CHAT_DSH.replace("@PY@", shlex.quote(sys.executable)),
            encoding="utf-8")
        self.fake_chat.chmod(0o755)
        Sandbox._counter += 1
        self.tag = Sandbox._counter

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class Harness:
    def __init__(self, sandbox: Sandbox, router: FakeRouter,
                 extra: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None,
                 name: str = "dsh-test", config: Optional[Path] = None) -> None:
        self.sandbox = sandbox
        self.err_path = sandbox.dir / ("worker-%d.err" % sandbox.tag)
        self._err = open(self.err_path, "wb")
        args = [sys.executable, str(HERE / "worker.py")]
        if config is not None:
            args += ["--config", str(config), "--no-feishu"]
        else:
            args += ["--router", router.base_url,
                     "--name", name,
                     "--workspace", str(sandbox.ws),
                     "--log-file", str(sandbox.dir / "worker.log"),
                     "--dsh-bin", str(sandbox.fake_dsh),
                     "--no-feishu"]
        args += list(extra or [])
        merged = os.environ.copy()
        merged["FAKE_DSH_TRACE"] = str(sandbox.trace)
        merged.update(env or {})
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=self._err, env=merged)

    def stop(self, sig: int = signal.SIGTERM, timeout: float = 8.0) -> int:
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(sig)
            except ProcessLookupError:
                pass
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait(timeout=3.0)
        try:
            self._err.close()
        except Exception:
            pass
        return code

    def stderr_text(self) -> str:
        try:
            return self.err_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


class Env:
    """一套「假 Router + 沙箱 + worker」；with 退出时必定清理。"""

    def __init__(self, tasks: Optional[List[Dict[str, Any]]] = None,
                 drop_first_stream: bool = False, **router_kwargs: Any) -> None:
        self.sb = Sandbox()
        self.router = FakeRouter(tasks=tasks, drop_first_stream=drop_first_stream,
                                 **router_kwargs)
        self.harness: Optional[Harness] = None

    def launch(self, extra: Optional[List[str]] = None,
               env: Optional[Dict[str, str]] = None,
               name: str = "dsh-test",
               config: Optional[Path] = None) -> Harness:
        self.harness = Harness(self.sb, self.router, extra=extra, env=env,
                               name=name, config=config)
        return self.harness

    def __enter__(self) -> "Env":
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.harness is not None:
            self.harness.stop()
        self.router.close()
        self.sb.cleanup()

    # 便捷断言
    def result(self, index: int = 0) -> Dict[str, Any]:
        return self.router.results[index]["body"]

    def log_text(self) -> str:
        try:
            return (self.sb.dir / "worker.log").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            return ""


def read_trace(path: Path) -> List[tuple]:
    entries: List[tuple] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        parts = line.split()
        if len(parts) == 2:
            entries.append((parts[0], float(parts[1])))
    return entries


class FakeFeishuApi:
    """假 lark Client：只记录 im.v1.message.create 收到什么。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: List[tuple] = []
        self.im = SimpleNamespace(v1=SimpleNamespace(
            message=SimpleNamespace(create=self._create)))

    def _create(self, request: Any) -> Any:
        if hasattr(request, "text"):
            receiver = getattr(request, "receive_id", "")
            text = getattr(request, "text", "")
        else:
            body = request.request_body
            receiver = body.receive_id
            text = json.loads(body.content)["text"]
        self.sent.append((receiver, text))
        return SimpleNamespace(success=lambda: self.ok,
                               code=0 if self.ok else 500, msg="ok")


def feishu_event(text: str, open_id: str, chat_type: str = "p2p",
                 message_type: str = "text",
                 message_id: str = "") -> Any:
    return SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(
            chat_type=chat_type,
            message_type=message_type,
            message_id=message_id,
            content=json.dumps({"text": text}, ensure_ascii=False)),
        sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=open_id))))


def fake_creds(open_id: str = "ou_trainer") -> Any:
    return feishu_sdk.FeishuCreds(app_id="cli_fake", app_secret="secret",
                                  domain="feishu", bot_name="玉藻十字",
                                  open_id=open_id)


def make_worker(env: "Env", name: str = "dsh-test") -> Any:
    cfg = worker.parse_args([
        "--router", env.router.base_url,
        "--name", name,
        "--workspace", str(env.sb.ws),
        "--dsh-bin", str(env.sb.fake_dsh),
        "--log-file", str(env.sb.dir / "worker.log"),
        "--no-feishu",
    ])
    return worker.Worker(cfg, None, str(env.sb.fake_dsh))


class FakeChatBridge:
    """假飞书桥：记录发出/编辑的每条消息。"""

    def __init__(self) -> None:
        self.sent: List[tuple] = []
        self.updates: List[tuple] = []
        self.fail_update = False
        self._counter = 0

    def send_text_message(self, text: str) -> str:
        self._counter += 1
        mid = "om_out_%d" % self._counter
        self.sent.append((mid, text))
        return mid

    def update_text(self, message_id: str, text: str) -> bool:
        if self.fail_update:
            return False
        self.updates.append((message_id, text))
        return True

    def send_text(self, text: str) -> bool:
        return bool(self.send_text_message(text))

    def texts(self) -> List[str]:
        return [text for _mid, text in self.sent]


class EnvVars:
    """临时改 os.environ（agent 子进程会继承）。"""

    def __init__(self, **values: str) -> None:
        self.values = values
        self.saved: Dict[str, Optional[str]] = {}

    def __enter__(self) -> "EnvVars":
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *_exc: Any) -> None:
        for key, old in self.saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0,
               interval: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


def trace_lines(path: Path) -> List[str]:
    try:
        return [line for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except OSError:
        return []


def make_chat_service(env: "Env", bridge: FakeChatBridge,
                      interval: float = 0.2, queue_max: int = 3) -> Any:
    store = chat_sdk.ChatStateStore(str(env.sb.dir / "chat-state.json"))
    return chat_sdk.ChatService(
        "dsh-test", bridge,
        chat_sdk.DshAgent(str(env.sb.fake_chat), workspace=str(env.sb.ws),
                          timeout=20),
        store,
        post_log=lambda payload: worker.http_json(
            env.router.base_url + "/chat/log", payload, None),
        queue_max=queue_max, stream_interval=interval)


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #


@test
def test_01_register() -> None:
    """注册：/register 带 name+version，返回后挂上 SSE"""
    with Env() as env:
        env.launch(["--timeout", "20"])
        need(env.router.wait_registrations(1, 10), "worker 没有注册")
        reg = env.router.registrations[0]["body"]
        need(reg.get("name") == "dsh-test", "注册 name 不对：%r" % reg)
        need(reg.get("version") == worker.VERSION,
             "注册 version 不对：%r" % reg)
        need(env.router.wait_streams(1, 10), "没有打开 /stream")


@test
def test_02_task_result_done() -> None:
    """任务：收到 SSE 任务 → 执行 → /result 字段完整、status=done"""
    with Env(tasks=[make_task("t-100")]) as env:
        env.launch(["--timeout", "20"])
        need(env.router.wait_results(1, 20), "没有收到结果")
        res = env.result()
        for key in ("name", "task_id", "status", "summary", "artifacts", "evidence"):
            need(key in res, "结果缺字段 %s：%r" % (key, res))
        need(res["name"] == "dsh-test", "结果 name 不对")
        need(res["task_id"] == "t-100", "结果 task_id 不对")
        need(res["status"] == "done", "status 不是 done：%r" % res["status"])
        need("hello-from-router" in res["summary"], "goal 没传给 dsh")
        need("fake-dsh: done" in res["summary"], "summary 不是 dsh stdout")
        need(res["artifacts"] == [], "artifacts 应为空数组")
        evidence = res["evidence"]
        need(isinstance(evidence, list) and evidence, "evidence 不是非空数组")
        need(evidence[0].get("type") == "exit", "evidence[0].type 不是 exit")
        need(evidence[0].get("code") == 0, "evidence[0].code 不是 0")


@test
def test_03_token_auth() -> None:
    """鉴权：token 文件 → register/stream/heartbeat/result 都带 X-Router-Token"""
    with Env(tasks=[make_task("t-auth")]) as env:
        token_file = env.sb.dir / "router.token"
        token_file.write_text("sekret-token\n", encoding="utf-8")
        env.launch(["--timeout", "20", "--token-file", str(token_file)])
        need(env.router.wait_registrations(1, 10), "worker 没有注册")
        need(env.router.wait_results(1, 20), "没有收到结果")
        expected = "sekret-token"
        need(env.router.registrations[0]["xtoken"] == expected,
             "register 没带 X-Router-Token：%r" % env.router.registrations[0]["xtoken"])
        need(env.router.stream_xtoken and env.router.stream_xtoken[0] == expected,
             "stream 没带 X-Router-Token")
        need(all(hb["xtoken"] == expected for hb in env.router.heartbeats),
             "heartbeat 没带 X-Router-Token")
        need(env.router.results[0]["xtoken"] == expected,
             "result 没带 X-Router-Token")


@test
def test_04_heartbeat_running() -> None:
    """心跳：任务运行中至少 2 条 state=running，带 task_id/progress_at"""
    with Env(tasks=[make_task("t-hb")]) as env:
        env.launch(["--timeout", "20", "--heartbeat-interval", "0.4",
                    "--idle-heartbeat", "0.5"],
                   env={"FAKE_DSH_MODE": "sleep", "FAKE_DSH_SLEEP": "2"})
        need(env.router.wait_results(1, 20), "没有收到结果")
        running = [hb["body"] for hb in env.router.heartbeats
                   if hb["body"].get("state") == "running"]
        need(len(running) >= 2, "running 心跳太少：%d" % len(running))
        need(all(hb.get("task_id") == "t-hb" for hb in running),
             "running 心跳缺 task_id")
        need(all(float(hb.get("progress_at") or 0) > 0 for hb in running),
             "心跳缺 progress_at")
        need(all(hb.get("name") == "dsh-test" for hb in running),
             "心跳缺 name")


@test
def test_05_summary_tail_4000() -> None:
    """summary：stdout 超长时只留末尾 4000 字，结尾内容完整"""
    with Env(tasks=[make_task("t-big")]) as env:
        env.launch(["--timeout", "20"], env={"FAKE_DSH_MODE": "big"})
        need(env.router.wait_results(1, 20), "没有收到结果")
        summary = env.result()["summary"]
        need(len(summary) == 4000, "summary 长度不是 4000：%d" % len(summary))
        need(summary.endswith("TAIL-MARKER-0123456789"),
             "summary 没保留 stdout 末尾")


@test
def test_06_failure_exit_code() -> None:
    """失败：dsh 退出码 3 → status=failed，evidence.code=3，stderr 进 summary"""
    with Env(tasks=[make_task("t-fail")]) as env:
        env.launch(["--timeout", "20"], env={"FAKE_DSH_MODE": "fail"})
        need(env.router.wait_results(1, 20), "没有收到结果")
        res = env.result()
        need(res["status"] == "failed", "status 不是 failed")
        evidence = res["evidence"]
        need(evidence[0].get("code") == 3, "evidence.code 不是 3：%r" % evidence)
        need("partial output before failure" in res["summary"],
             "summary 缺 stdout 尾部")
        need("boom on stderr" in res["summary"], "summary 缺 stderr")
        need("退出码 3" in res["summary"], "summary 没说明退出码")


@test
def test_07_timeout() -> None:
    """超时：--timeout 1.2 秒 → 杀进程组、status=failed、reason=timeout"""
    with Env(tasks=[make_task("t-slow")]) as env:
        started = time.time()
        env.launch(["--timeout", "1.2"],
                   env={"FAKE_DSH_MODE": "sleep", "FAKE_DSH_SLEEP": "10"})
        need(env.router.wait_results(1, 15), "超时任务没有上报结果")
        elapsed = time.time() - started
        res = env.result()
        need(elapsed < 6.0, "超时没及时返回：%.1fs" % elapsed)
        need(res["status"] == "failed", "超时 status 不是 failed")
        need("超时" in res["summary"], "summary 没说明超时")
        evidence = res["evidence"][0]
        need(evidence.get("reason") == "timeout", "evidence.reason 不是 timeout")
        need(abs(float(evidence.get("timeout_s") or 0) - 1.2) < 0.01,
             "evidence.timeout_s 不对")
        need(env.harness is not None and env.harness.proc.poll() is None,
             "超时后 worker 自己退出了")


@test
def test_08_sequential() -> None:
    """串行：两个任务不并行，第二个在第一个结束后才开始"""
    with Env(tasks=[make_task("t-a"), make_task("t-b")]) as env:
        env.launch(["--timeout", "20"],
                   env={"FAKE_DSH_MODE": "sleep", "FAKE_DSH_SLEEP": "0.6"})
        need(env.router.wait_results(2, 25), "两个任务没有都完成")
        ids = sorted(res["body"]["task_id"] for res in env.router.results)
        need(ids == ["t-a", "t-b"], "结果 task_id 不对：%r" % ids)
        trace = read_trace(env.sb.trace)
        starts = [ts for kind, ts in trace if kind == "start"]
        ends = [ts for kind, ts in trace if kind == "end"]
        need(len(starts) == 2 and len(ends) == 2,
             "dsh 调用次数不对：%r" % trace)
        need(starts[1] >= ends[0] - 0.1,
             "两个 dsh 执行重叠了：start2=%.3f end1=%.3f" % (starts[1], ends[0]))


@test
def test_09_reconnect_reregister() -> None:
    """重连：SSE 被断 → 约 1s 后重连并重新 /register，然后正常接活"""
    with Env(tasks=[make_task("t-r")], drop_first_stream=True) as env:
        env.launch(["--timeout", "20"])
        need(env.router.wait_registrations(2, 15), "断线后没有重新注册")
        need(env.router.wait_results(1, 15), "重连后没收到任务结果")
        need(env.router.stream_opens >= 2,
             "没有第二次打开 /stream：%d" % env.router.stream_opens)
        delta = (env.router.registrations[1]["ts"]
                 - env.router.registrations[0]["ts"])
        need(0.8 <= delta <= 8.0, "重连间隔异常：%.2fs" % delta)
        need(env.result()["status"] == "done", "重连后任务没成功")


@test
def test_10_sigterm_graceful() -> None:
    """退出：空闲时 SIGTERM → 6 秒内退出 0，日志记到信号"""
    with Env() as env:
        harness = env.launch(["--timeout", "20"])
        need(env.router.wait_registrations(1, 10), "worker 没有注册")
        started = time.time()
        code = harness.stop(signal.SIGTERM, timeout=6.0)
        elapsed = time.time() - started
        need(code == 0, "SIGTERM 退出码不是 0：%r" % code)
        need(elapsed < 6.0, "SIGTERM 退得太慢：%.1fs" % elapsed)
        need("SIGTERM" in env.log_text(), "日志没记 SIGTERM")
        need("dsh-worker 退出" in env.log_text(), "日志没有优雅退出记录")


@test
def test_11_duplicate_task() -> None:
    """去重：同一 task_id 推两次只执行一次，结果补发两次"""
    duplicate = make_task("t-dup")
    with Env(tasks=[dict(duplicate), dict(duplicate)]) as env:
        env.launch(["--timeout", "20"])
        need(env.router.wait_results(2, 20), "重复任务没有补发结果")
        trace = read_trace(env.sb.trace)
        starts = [ts for kind, ts in trace if kind == "start"]
        need(len(starts) == 1, "重复任务被重复执行：%r" % trace)
        need(all(res["body"]["status"] == "done" for res in env.router.results),
             "补发结果 status 不对")


@test
def test_12_config_file() -> None:
    """配置：--config 读 worker.json（name/workspace/dsh_bin/log_file）"""
    with Env(tasks=[make_task("t-cfg")]) as env:
        config_path = env.sb.dir / "worker.json"
        config_path.write_text(json.dumps({
            "router": env.router.base_url,
            "name": "cfg-name",
            "workspace": str(env.sb.ws),
            "dsh_bin": str(env.sb.fake_dsh),
            "log_file": str(env.sb.dir / "cfg-worker.log"),
            "timeout": 20,
            "heartbeat_interval": 0.5,
            "idle_heartbeat": 1,
        }), encoding="utf-8")
        env.launch(config=config_path)
        need(env.router.wait_registrations(1, 10), "配置模式没有注册")
        need(env.router.registrations[0]["body"].get("name") == "cfg-name",
             "配置里的 name 没生效")
        need(env.router.wait_results(1, 20), "配置模式没有完成任务")
        need((env.sb.dir / "cfg-worker.log").exists(), "配置里的 log_file 没生效")
        need(env.result()["status"] == "done", "配置模式任务失败")


@test
def test_13_syntax_and_files() -> None:
    """静态：worker/selftest 可编译、start-worker.sh 语法与可执行位、模板合法"""
    pycache = Path(tempfile.mkdtemp(prefix="dsh-pycache-"))
    try:
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = str(pycache)
        for path in (HERE / "worker.py", HERE / "selftest_worker.py"):
            proc = subprocess.run([sys.executable, "-m", "py_compile",
                                   str(path)], capture_output=True, text=True,
                                  env=env)
            need(proc.returncode == 0, "%s 编译失败：%s" % (path.name, proc.stderr))
        proc = subprocess.run(["sh", "-n", str(HERE / "start-worker.sh")],
                              capture_output=True, text=True)
        need(proc.returncode == 0, "start-worker.sh 语法错误：%s" % proc.stderr)
        need(os.access(HERE / "start-worker.sh", os.X_OK),
             "start-worker.sh 没有可执行位")
        json.loads((HERE / "worker.json").read_text(encoding="utf-8"))
        need("Router" in (HERE / "README.md").read_text(encoding="utf-8"),
             "README 缺内容")
    finally:
        shutil.rmtree(pycache, ignore_errors=True)


@test
def test_14_pure_helpers() -> None:
    """纯函数：截尾、summary 预算、SSE 任务解析、prompt、配置优先级"""
    need(worker.tail_text("abcdef", 3) == "def", "tail_text 不对")
    need(worker.tail_text("abc", 10) == "abc", "tail_text 短文本不对")
    summary = worker.compose_summary("超时说明", "X" * 5000, "E" * 2000)
    need(len(summary) <= 4000, "compose_summary 超过 4000：%d" % len(summary))
    need(summary.startswith("超时说明"), "compose_summary 丢了 note")
    need(summary.endswith("X"), "compose_summary 没保留 stdout 尾部")
    short = worker.compose_summary("退出码 3", "out", "err-tail")
    need(len(short) <= 4000 and short.endswith("err-tail"),
         "compose_summary 在 stdout 短时没附 stderr")

    flat = worker.extract_task(
        "message", json.dumps({"task_id": "t-9", "goal": "g"}))
    need(flat and flat["task_id"] == "t-9", "extract_task 不认裸任务")
    nested = worker.extract_task(
        "message", json.dumps({"type": "task",
                               "task": {"task_id": "t-10", "goal": "g"}}))
    need(nested and nested["task_id"] == "t-10", "extract_task 不认嵌套任务")
    need(worker.extract_task(
        "message", json.dumps({"type": "message", "text": "hi"})) is None,
        "extract_task 把普通消息当任务")
    need(worker.extract_task("message", "not-json") is None,
         "extract_task 没忽略坏 JSON")

    prompt = worker.build_prompt("do it", "some context")
    need(prompt.startswith("do it") and "## Context" in prompt
         and "some context" in prompt, "build_prompt 不对")

    cfg_path = Path(tempfile.mkdtemp(prefix="dsh-cfg-")) / "worker.json"
    try:
        cfg_path.write_text(json.dumps({"router": "http://127.0.0.1:9999",
                                        "name": "file-name"}), encoding="utf-8")
        args = worker.parse_args(["--config", str(cfg_path), "--name", "cli-name"])
        need(args.router == "http://127.0.0.1:9999", "配置文件 router 没生效")
        need(args.name == "cli-name", "命令行没有覆盖配置文件")
        cfg_path.write_text(json.dumps({"bogus": 1}), encoding="utf-8")
        try:
            worker.parse_args(["--config", str(cfg_path)])
        except SystemExit:
            pass
        else:
            raise AssertionError("未知配置字段没有被拒绝")
    finally:
        shutil.rmtree(cfg_path.parent, ignore_errors=True)


@test
def test_15_idle_stream_not_disconnect() -> None:
    """空闲：SSE 建立后 3 秒零字节 → 读超时不算断线，register 只 1 次"""
    with Env(stream_mode="silent") as env:
        env.launch(["--timeout", "20", "--idle-timeout", "90"])
        need(env.router.wait_registrations(1, 10), "worker 没有注册")
        need(env.router.wait_streams(1, 10), "没有打开 /stream")
        time.sleep(3.2)
        opens = len(env.router.registrations)
        need(opens == 1, "空闲 3 秒被误判断线，register=%d 次" % opens)
        need(env.router.stream_opens == 1,
             "空闲 3 秒重开了 /stream：%d 次" % env.router.stream_opens)
        need(env.harness is not None and env.harness.proc.poll() is None,
             "worker 在空闲流上退出了")
        need("连接中断" not in env.log_text(),
             "空闲流被记成断线：%r" % env.log_text()[-400:])


@test
def test_16_ping_keepalive_not_disconnect() -> None:
    """心跳：每隔 1 秒 `: ping` 持续 5 秒 → 仍不重连"""
    with Env(stream_mode="slow_ping", ping_interval=1.0, hold_s=6.0) as env:
        env.launch(["--timeout", "20", "--idle-timeout", "90"])
        need(env.router.wait_registrations(1, 10), "worker 没有注册")
        time.sleep(5.2)
        need(env.router.pings_sent >= 4,
             "假 Router 没发出 ping：%d" % env.router.pings_sent)
        opens = len(env.router.registrations)
        need(opens == 1, "ping 保活期间重连了，register=%d 次" % opens)
        need(env.router.stream_opens == 1,
             "ping 保活期间重开了 /stream：%d 次" % env.router.stream_opens)


@test
def test_17_true_close_reconnects() -> None:
    """真断：Router 关闭 SSE 连接 → 必须重连并重新注册（register 2 次）"""
    with Env(drop_first_stream=True) as env:
        env.launch(["--timeout", "20"])
        need(env.router.wait_registrations(2, 15), "真断开后没有重连注册")
        need(env.router.wait_streams(2, 10), "真断开后没有重开 /stream")
        time.sleep(0.5)
        opens = len(env.router.registrations)
        need(opens == 2, "重连次数异常：register=%d 次" % opens)


@test
def test_18_iter_sse_timeout_semantics() -> None:
    """纯函数：读超时/中毒 OSError 当无数据，EOF 与静默超时才断线，ping 忽略"""
    class Stub:
        def __init__(self, script: List[Any]) -> None:
            self.script = list(script)

        def readline(self) -> bytes:
            item = (self.script.pop(0) if self.script
                    else socket.timeout("timed out"))
            if isinstance(item, (socket.timeout, TimeoutError)):
                time.sleep(0.1)  # 模拟真实读超时的一段等待
                raise item
            if isinstance(item, BaseException):
                raise item
            return item

    stop = threading.Event()
    payload = '{"task_id":"t-ok","goal":"g"}'
    stub = Stub([socket.timeout("timed out"),
                 OSError("cannot read from timed out object"),
                 b": ping\n", b"\n",
                 b"event: ping\n", b"data: {}\n", b"\n",
                 b"event: task\n",
                 ("data: %s\n" % payload).encode("utf-8"),
                 b"\n", b": ping\n", b"\n", b""])
    stream = worker.iter_sse(stub, stop, 10.0)
    try:
        event, data = next(stream)
    finally:
        stream.close()
    need(event == "task", "超时后读到的 event 不对：%r" % event)
    need("t-ok" in data, "任务内容不对：%r" % data)

    try:
        list(worker.iter_sse(Stub([b""]), stop, 10.0))
    except worker.RouterError as exc:
        need("关闭" in str(exc), "EOF 报错不对：%s" % exc)
    else:
        raise AssertionError("真 EOF 没有判成断线")

    started = time.time()
    try:
        list(worker.iter_sse(Stub([socket.timeout("timed out")] * 30),
                             stop, 0.3))
    except worker.RouterError as exc:
        need("静默" in str(exc), "静默报错不对：%s" % exc)
    else:
        raise AssertionError("静默超时没有判成断线")
    need(time.time() - started < 2.0, "静默判定太慢")


@test
def test_19_feishu_inbound_to_notice() -> None:
    """飞书入向：p2p 私信 → /notice(urgency=urgent) → 回确认（假 API/假连接）"""
    with Env() as env:
        instance = make_worker(env)
        api = FakeFeishuApi()
        seen: List[str] = []

        def on_text(text: str) -> bool:
            seen.append(text)
            return instance._on_feishu_text(text)

        bridge = feishu_sdk.FeishuBridge(
            fake_creds(), "ou_trainer", on_text,
            confirm_text="收到，已转给溪流", api_client=api)
        bridge._build_message_request = lambda text: SimpleNamespace(
            receive_id="ou_trainer", text=text)  # 自测不依赖 lark 构建器
        bridge.handle_event(feishu_event("先跑一遍 selftest 再动别的", "ou_trainer"))

        need(seen == ["先跑一遍 selftest 再动别的"],
             "入向文本没有交给 on_text：%r" % seen)
        need(env.router.wait_notices(1, 5), "没有调用 /notice")
        body = env.router.notices[0]["body"]
        need(body.get("worker") == "dsh-test", "notice.worker 不对：%r" % body)
        need(body.get("urgency") == "urgent", "notice.urgency 不是 urgent")
        need(body.get("text") == "先跑一遍 selftest 再动别的",
             "notice.text 不对：%r" % body)
        need(api.sent == [("ou_trainer", "收到，已转给溪流")],
             "没有回确认私信：%r" % api.sent)


@test
def test_20_feishu_filters_send_reconnect() -> None:
    """飞书出向/过滤/重连：send_text 到达、群聊/他人/非文本忽略、假长连接断开重启"""
    api = FakeFeishuApi()
    got: List[str] = []

    class FakeWs:
        def __init__(self) -> None:
            self.calls = 0
            self.closed = False

        def start(self) -> None:
            self.calls += 1
            time.sleep(0.02)

        def close(self) -> None:
            self.closed = True

    ws = FakeWs()
    bridge = feishu_sdk.FeishuBridge(
        fake_creds(), "ou_trainer", lambda text: got.append(text) or True,
        api_client=api, ws_client=ws, restart_delay=0.02,
        max_restart_delay=0.05)
    bridge._build_message_request = lambda text: SimpleNamespace(
        receive_id="ou_trainer", text=text)
    bridge.start()
    need(bridge.wait_connected(2), "假长连接没有启动")
    deadline = time.time() + 2
    while time.time() < deadline and ws.calls < 2:
        time.sleep(0.02)
    need(ws.calls >= 2, "长连接返回后没有重连：calls=%d" % ws.calls)
    bridge.stop()
    thread = bridge._thread
    need(thread is not None and not thread.is_alive(), "stop 后飞书线程还在")

    bridge.handle_event(feishu_event("群里的", "ou_trainer", chat_type="group"))
    bridge.handle_event(feishu_event("别人的", "ou_other"))
    bridge.handle_event(feishu_event("图片", "ou_trainer", message_type="image"))
    need(got == [], "不该处理的私信被交给了 on_text：%r" % got)

    need(bridge.send_text("任务完成"), "send_text 失败")
    need(api.sent[-1] == ("ou_trainer", "任务完成"),
         "send_text 内容/收件人不对：%r" % api.sent)
    bad = FakeFeishuApi(ok=False)
    bridge_bad = feishu_sdk.FeishuBridge(fake_creds(), "ou_trainer",
                                         lambda _t: True, api_client=bad)
    bridge_bad._build_message_request = lambda text: SimpleNamespace(
        receive_id="ou_trainer", text=text)
    need(bridge_bad.send_text("失败") is False, "API 报错时 send_text 没返回 False")


@test
def test_21_watchdog_stall_and_kill() -> None:
    """看门狗：有输出/有 CPU 不判死；双静默超阈值判卡死并真的杀掉进程组"""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        size = [0]
        watchdog = watchdog_sdk.StallWatchdog(
            proc, lambda: size[0], stall_seconds=0.35,
            cpu_reader=lambda _pgid: 1.0)
        end = time.time() + 0.6
        while time.time() < end:
            size[0] += 1
            need(watchdog.check() is None, "有输出时被误判卡死")
            time.sleep(0.05)
        stalled = None
        end = time.time() + 2.0
        while time.time() < end:
            stalled = watchdog.check()
            if stalled is not None:
                break
            time.sleep(0.05)
        need(stalled is not None and stalled >= 0.35,
             "双静默没有判卡死：%r" % stalled)
        watchdog.kill()
        need(proc.wait(timeout=5) is not None, "看门狗没杀掉进程")
        need(proc.poll() is not None, "进程仍存活")

        # CPU 一直在变 → 不算卡死（闷头算不打印是正常情况）
        proc2 = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        try:
            ticks = [0.0]

            def cpu(_pgid: int) -> float:
                ticks[0] += 0.5
                return ticks[0]

            wd2 = watchdog_sdk.StallWatchdog(
                proc2, lambda: 0, stall_seconds=0.3, cpu_reader=cpu)
            end = time.time() + 0.7
            while time.time() < end:
                need(wd2.check() is None, "CPU 变化时被误判卡死")
                time.sleep(0.05)
        finally:
            if proc2.poll() is None:
                proc2.kill()
                proc2.wait(timeout=3)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


@test
def test_22_subscription_feed_and_rate_limit() -> None:
    """订阅：POST /subscriptions、pending 落盘、60 秒窗口最多一条飞书汇总"""
    with Env() as env:
        now = [1000.0]
        sent: List[str] = []
        notifier = subs_sdk.FeedNotifier(
            lambda text: sent.append(text) or True, window_s=60.0,
            clock=lambda: now[0])
        feed = env.sb.dir / "subscription-feed.jsonl"
        poller = subs_sdk.SubscriptionPoller(
            worker="dsh-test", sources=["mc_chat", "email"],
            post_json=lambda path, payload: worker.http_json(
                env.router.base_url + path, payload, None),
            get_json=lambda path: worker.http_json(
                env.router.base_url + path, None, None),
            feed_path=str(feed), notifier=notifier, interval=1,
            clock=lambda: now[0])
        need(poller.subscribe_all() == 2, "订阅声明数量不对")
        need(env.router.wait_subscriptions(2, 5), "Router 没收到 /subscriptions")
        bodies = [item["body"] for item in env.router.subscriptions]
        need(all(b.get("worker") == "dsh-test" for b in bodies),
             "订阅 worker 不对：%r" % bodies)
        need(sorted(b.get("source") for b in bodies) == ["email", "mc_chat"],
             "订阅 source 不对：%r" % bodies)

        env.router.pending_messages = [
            {"id": "m1", "source": "mc_chat", "text": "小明：你好"},
            {"id": "m2", "source": "email", "text": "邮件标题"},
            {"id": "m3", "source": "mc_chat", "text": "小明：在吗"},
        ]
        need(poller.poll_once() == 3, "pending 条数不对")
        lines = [json.loads(line) for line in
                 feed.read_text(encoding="utf-8").splitlines() if line.strip()]
        need([item["id"] for item in lines] == ["m1", "m2", "m3"],
             "落盘内容不对：%r" % lines)
        need(all(item.get("worker") == "dsh-test" for item in lines),
             "落盘缺 worker")
        need(len(sent) == 1, "首批没有立即汇总：%d" % len(sent))

        env.router.pending_messages = [{"id": "m4", "source": "mc_chat",
                                        "text": "窗口内第二条"}]
        need(poller.poll_once() == 1, "窗口内消息没落盘")
        need(len(sent) == 1, "60 秒窗口内发了多条：%d" % len(sent))

        now[0] += 61
        need(notifier.flush(), "窗口到期没有补发汇总")
        need(len(sent) == 2, "窗口到期汇总数量不对：%d" % len(sent))
        need("窗口内第二条" in sent[1], "补发汇总里没有窗口内消息：%r" % sent[1])

        env.router.pending_messages = [{"id": "m4", "source": "mc_chat",
                                        "text": "重复投递"}]
        need(poller.poll_once() == 0, "重复消息没有去重")
        need(len(feed.read_text(encoding="utf-8").splitlines()) == 4,
             "重复消息被重复落盘")


@test
def test_23_subscription_worker_integration() -> None:
    """订阅（子进程）：--subscribe 启动后声明订阅并拉取 pending 落盘"""
    with Env(pending_messages=[{"id": "s1", "source": "mc_chat",
                                "text": "hi-from-subscription"}]) as env:
        feed = env.sb.dir / "sub-feed.jsonl"
        env.launch(["--timeout", "20", "--subscribe", "mc_chat",
                    "--subscribe-interval", "0.2",
                    "--subscription-feed", str(feed)])
        need(env.router.wait_subscriptions(1, 10), "worker 没声明订阅")
        body = env.router.subscriptions[0]["body"]
        need(body.get("worker") == "dsh-test" and body.get("source") == "mc_chat",
             "订阅请求体不对：%r" % body)
        deadline = time.time() + 8
        while time.time() < deadline and not (feed.exists()
                                              and feed.read_text().strip()):
            time.sleep(0.1)
        need(feed.exists(), "订阅 feed 没落盘")
        lines = [json.loads(line) for line in
                 feed.read_text(encoding="utf-8").splitlines() if line.strip()]
        need(len(lines) == 1 and lines[0]["id"] == "s1",
             "订阅 feed 内容不对：%r" % lines)


@test
def test_24_cli_and_config_flags() -> None:
    """配置：--idle-reconnect/--stall-seconds/--subscribe/飞书默认凭据"""
    args = worker.parse_args(["--no-feishu", "--idle-reconnect", "12.5",
                              "--stall-seconds", "7", "--subscribe", "a, b"])
    need(abs(args.idle_timeout - 12.5) < 0.01, "--idle-reconnect 没生效")
    need(abs(args.stall_seconds - 7) < 0.01, "--stall-seconds 没生效")
    need(args.subscribe_sources == ["a", "b"], "订阅 source 解析不对")
    need(args.no_feishu is True, "--no-feishu 没生效")

    defaults = worker.parse_args([])
    need(defaults.feishu_creds.endswith("feishu-tsurumaru.json"),
         "默认飞书凭据不是玉藻十字：%r" % defaults.feishu_creds)
    need(abs(defaults.stall_seconds - 300) < 0.01, "默认 stall 不是 300")
    need(abs(defaults.idle_timeout - 90) < 0.01, "默认 idle-reconnect 不是 90")
    need(defaults.chat_enabled is False, "聊天默认应关闭（配置显式开）")
    need(defaults.chat_state.endswith("dsh-chat-state.json"),
         "默认 chat-state 路径不对：%r" % defaults.chat_state)
    need(worker.parse_args(["--chat"]).chat_enabled is True, "--chat 没生效")
    need(worker.parse_args(["--chat", "--no-chat"]).chat_enabled is False,
         "--no-chat 没生效")
    need(worker.parse_args(["--chat-queue", "5"]).chat_queue == 5,
         "--chat-queue 没生效")

    cfg_path = Path(tempfile.mkdtemp(prefix="dsh-cfg2-")) / "worker.json"
    try:
        cfg_path.write_text(json.dumps({"idle_reconnect": 42,
                                        "subscribe": "mc_chat"}), encoding="utf-8")
        from_file = worker.parse_args(["--config", str(cfg_path),
                                       "--no-feishu"])
        need(abs(from_file.idle_timeout - 42) < 0.01,
             "配置 idle_reconnect 没生效")
        need(from_file.subscribe_sources == ["mc_chat"],
             "配置 subscribe 没生效")
    finally:
        shutil.rmtree(cfg_path.parent, ignore_errors=True)

    cfg = json.loads((HERE / "worker.json").read_text(encoding="utf-8"))
    need(str(cfg.get("feishu_creds", "")).endswith("feishu-tsurumaru.json"),
         "worker.json 的飞书凭据不对")
    need("stall_seconds" in cfg and "idle_reconnect" in cfg,
         "worker.json 缺看门狗/重连配置")
    need(cfg.get("chat_enabled") is True, "worker.json 没开 chat_enabled")
    need(str(cfg.get("chat_state", "")).endswith("dsh-chat-state.json"),
         "worker.json 的 chat_state 不对：%r" % cfg.get("chat_state"))


@test
def test_25_worker_stall_integration() -> None:
    """卡死（子进程）：dsh 静默且 CPU 不变 → 看门狗杀进程，result 报 stalled"""
    with Env(tasks=[make_task("t-stall")]) as env:
        env.launch(["--timeout", "20", "--stall-seconds", "1"],
                   env={"FAKE_DSH_MODE": "stall", "FAKE_DSH_SLEEP": "10"})
        need(env.router.wait_results(1, 15), "卡死任务没有上报结果")
        res = env.result()
        need(res["status"] == "failed", "卡死 status 不是 failed")
        need(res["summary"].startswith("stalled: no output for "),
             "summary 不是 stalled 格式：%r" % res["summary"])
        evidence = res["evidence"][0]
        need(evidence.get("reason") == "stalled",
             "evidence.reason 不是 stalled：%r" % evidence)
        need(abs(float(evidence.get("stall_s") or 0) - 1.0) < 0.01,
             "evidence.stall_s 不对：%r" % evidence)
        need(env.harness is not None and env.harness.proc.poll() is None,
             "卡死处理后 worker 自己退出了")
        need("卡死" in env.log_text(), "日志没记卡死")


@test
def test_26_stall_feishu_alert() -> None:
    """卡死告警：worker 先发『开始任务』，判定卡死时发飞书告警再杀进程"""
    with Env() as env:
        instance = make_worker(env)
        instance.cfg.stall_seconds = 1.0
        sent: List[str] = []
        instance.feishu = SimpleNamespace(
            send_text=lambda text: sent.append(text) or True)
        old_mode = os.environ.get("FAKE_DSH_MODE")
        old_sleep = os.environ.get("FAKE_DSH_SLEEP")
        os.environ["FAKE_DSH_MODE"] = "stall"
        os.environ["FAKE_DSH_SLEEP"] = "10"
        try:
            instance.execute(make_task("t-alert"))
        finally:
            if old_mode is None:
                os.environ.pop("FAKE_DSH_MODE", None)
            else:
                os.environ["FAKE_DSH_MODE"] = old_mode
            if old_sleep is None:
                os.environ.pop("FAKE_DSH_SLEEP", None)
            else:
                os.environ["FAKE_DSH_SLEEP"] = old_sleep
        need(any("来自超级溪流的指令" in ((n.get("body") or {}).get("text") or "")
                 and "t-alert" in ((n.get("body") or {}).get("text") or "")
                 for n in env.router.notices),
             "没有发任务开始通知：%r" % env.router.notices)
        need(any("卡死" in ((n.get("body") or {}).get("text") or "")
                 and "t-alert" in ((n.get("body") or {}).get("text") or "")
                 for n in env.router.notices),
             "卡死没有发告警：%r" % env.router.notices)
        need(env.router.wait_results(1, 5), "卡死结果没上报")
        need(env.result()["evidence"][0].get("reason") == "stalled",
             "结果不是 stalled")


@test
def test_27_chat_sessions_and_log() -> None:
    """聊天①：首轮新建 session 落盘；次轮 --session-id；/chat/log 字段完整"""
    with Env() as env:
        bridge = FakeChatBridge()
        service = make_chat_service(env, bridge, interval=0.2)
        trace = env.sb.dir / "chat-trace.log"
        stop = threading.Event()
        with EnvVars(FAKE_CHAT_TRACE=str(trace), FAKE_CHAT_MODE="ok",
                     FAKE_CHAT_SID="session-chat-1",
                     FAKE_CHAT_REPLY="第一轮回答"):
            service.start(stop)
            try:
                service.handle_text("消息一", "om_in_1", "ou_trainer")
                need(wait_until(lambda: "第一轮回答" in bridge.texts(), 10),
                     "首轮没有回复：%r" % bridge.texts())
                state = json.loads(
                    (env.sb.dir / "chat-state.json").read_text(encoding="utf-8"))
                item = state["sessions"]["ou_trainer"]
                need(item["session_id"] == "session-chat-1",
                     "首轮 session 没落盘：%r" % item)
                need(item["suspicious"] is False, "首轮就标了可疑")
                need(item["turns"] == 1, "turns 不对：%r" % item)
                lines = trace_lines(trace)
                need(len(lines) == 1, "首轮 agent 调用次数不对：%r" % lines)
                need("--session-id" not in lines[0],
                     "首轮不该带 --session-id：%r" % lines[0])

                os.environ["FAKE_CHAT_REPLY"] = "第二轮回答"
                service.handle_text("消息二", "om_in_2", "ou_trainer")
                need(wait_until(lambda: "第二轮回答" in bridge.texts(), 10),
                     "次轮没有回复：%r" % bridge.texts())
                lines = trace_lines(trace)
                need(len(lines) == 2, "次轮 agent 调用次数不对：%r" % lines)
                need("--session-id session-chat-1" in lines[1],
                     "次轮没有 resume：%r" % lines[1])
                item = json.loads(
                    (env.sb.dir / "chat-state.json").read_text(
                        encoding="utf-8"))["sessions"]["ou_trainer"]
                need(item["turns"] == 2, "turns 没累加：%r" % item)
                need(service.runs == 2, "ChatService.runs 不对：%d" % service.runs)

                need(env.router.wait_chat_logs(4, 5), "/chat/log 调用不足：%d"
                     % len(env.router.chat_logs))
                entries = env.router.chat_logs
                need(all(item["path"] == "/chat/log" for item in entries),
                     "旁听路径不对")
                logs = [item["body"] for item in entries]
                ins = [b for b in logs if b.get("direction") == "in"]
                outs = [b for b in logs if b.get("direction") == "out"]
                need(len(ins) == 2, "in 日志条数不对：%r" % ins)
                need(len(outs) == 2, "out 日志条数不对：%r" % outs)
                for body in logs:
                    need(body.get("worker") == "dsh-test",
                         "chat/log 缺 worker：%r" % body)
                    need(body.get("direction") in ("in", "out"),
                         "chat/log direction 不对：%r" % body)
                    for key in ("session_id", "message_id", "text"):
                        need(key in body, "chat/log 缺字段 %s：%r" % (key, body))
                    need(body.get("message_id"), "chat/log message_id 为空：%r" % body)
                    need(body.get("text"), "chat/log text 为空：%r" % body)
                need(ins[0]["message_id"] == "om_in_1",
                     "首条 in 的 message_id 不对：%r" % ins[0])
                need(ins[1]["session_id"] == "session-chat-1",
                     "次条 in 没带 session：%r" % ins[1])
                need(outs[0]["session_id"] == "session-chat-1",
                     "out 没带 session：%r" % outs[0])
            finally:
                stop.set()
                service.stop()


@test
def test_28_chat_stream_throttle() -> None:
    """聊天③：多次 NDJSON 事件按间隔节流，只编辑同一条进行中消息（发送 ≤2 条）"""
    with Env() as env:
        bridge = FakeChatBridge()
        service = make_chat_service(env, bridge, interval=0.3)
        trace = env.sb.dir / "chat-trace.log"
        stop = threading.Event()
        with EnvVars(FAKE_CHAT_TRACE=str(trace), FAKE_CHAT_MODE="stream",
                     FAKE_CHAT_REPLY="流式完成"):
            service.start(stop)
            try:
                service.handle_text("来个长任务", "om_stream", "ou_trainer")
                need(wait_until(lambda: "流式完成" in bridge.texts(), 15),
                     "流式任务没有回复：%r" % bridge.texts())
                progress_sent = [t for t in bridge.texts() if "进行中" in t]
                need(len(progress_sent) <= 2,
                     "进度消息发了 %d 条（应 ≤2）：%r"
                     % (len(progress_sent), progress_sent))
                need(len(bridge.updates) >= 2,
                     "进度没有按间隔编辑：%d" % len(bridge.updates))
                targets = {mid for mid, _text in bridge.updates}
                need(len(targets) == 1,
                     "进度编辑了多条消息：%r" % targets)
                last = bridge.updates[-1][1]
                need("进行中" in last, "编辑内容不是进度：%r" % last)
                need("bash" in last or "第 6 段" in last,
                     "进度里没有工具/文本：%r" % last)
            finally:
                stop.set()
                service.stop()


@test
def test_29_chat_busy_queue_and_task_block() -> None:
    """聊天④：忙时最多排队 3 条、超额回忙线；Router 任务期间聊天不抢跑"""
    with Env() as env:
        bridge = FakeChatBridge()
        service = make_chat_service(env, bridge)
        trace = env.sb.dir / "chat-trace.log"
        stop = threading.Event()
        with EnvVars(FAKE_CHAT_TRACE=str(trace), FAKE_CHAT_MODE="slow",
                     FAKE_CHAT_SLEEP="0.5", FAKE_CHAT_REPLY="慢回复"):
            service.start(stop)
            try:
                service.handle_text("消息一", "om_q1", "ou_trainer")
                need(wait_until(lambda: len(trace_lines(trace)) == 1, 10),
                     "第一条没有开跑：%r" % trace_lines(trace))
                for index in range(2, 6):
                    service.handle_text("消息%d" % index, "om_q%d" % index,
                                        "ou_trainer")
                busy = [t for t in bridge.texts() if "前面还在忙" in t]
                need(len(busy) == 1, "忙线提示数量不对：%r" % busy)
                need(wait_until(lambda: len(trace_lines(trace)) == 4, 20),
                     "排队消息没有全部跑完：%r" % trace_lines(trace))
                need(wait_until(lambda: sum(
                    1 for t in bridge.texts() if "慢回复" in t) == 4, 20),
                    "排队回复数量不对：%r" % bridge.texts())
                joined = "\n".join(trace_lines(trace))
                need("消息五" not in joined, "被拒绝的消息仍然执行了：%r" % joined)

                service.task_begin()
                service.handle_text("任务期间的消息", "om_task", "ou_trainer")
                time.sleep(0.6)
                need(len(trace_lines(trace)) == 4,
                     "Router 任务期间聊天抢跑了：%r" % trace_lines(trace))
                service.task_end()
                need(wait_until(lambda: len(trace_lines(trace)) == 5, 10),
                     "任务结束后排队聊天没有继续：%r" % trace_lines(trace))
                need("任务期间的消息" in "\n".join(trace_lines(trace)),
                     "任务期间的消息没被接着执行")
            finally:
                stop.set()
                service.stop()


@test
def test_30_chat_failure_marks_suspicious() -> None:
    """聊天⑥：非零退出发错误摘要、session 标可疑；下一轮重建并恢复"""
    with Env() as env:
        bridge = FakeChatBridge()
        service = make_chat_service(env, bridge)
        service.store.update("ou_trainer", session_id="session-bad",
                             suspicious=False, turns=1)
        trace = env.sb.dir / "chat-trace.log"
        stop = threading.Event()
        with EnvVars(FAKE_CHAT_TRACE=str(trace), FAKE_CHAT_MODE="fail",
                     FAKE_CHAT_SID="session-ignored", FAKE_CHAT_REPLY="不会用到"):
            service.start(stop)
            try:
                service.handle_text("失败一次", "om_f1", "ou_trainer")
                need(wait_until(lambda: any(
                    "出错了" in t for t in bridge.texts()), 10),
                    "失败没有回错误摘要：%r" % bridge.texts())
                need(any("退出码 3" in t for t in bridge.texts()),
                     "错误摘要缺退出码：%r" % bridge.texts())
                need(any("boom-boom" in t for t in bridge.texts()),
                     "错误摘要缺 stderr：%r" % bridge.texts())
                item = service.store.get("ou_trainer")
                need(item.get("session_id") == "session-bad",
                     "失败的 session 没保留：%r" % item)
                need(item.get("suspicious") is True,
                     "失败的 session 没标可疑：%r" % item)
                lines = trace_lines(trace)
                need(lines and "--session-id session-bad" in lines[0],
                     "失败轮没有 resume 旧 session：%r" % lines)

                os.environ["FAKE_CHAT_MODE"] = "ok"
                os.environ["FAKE_CHAT_SID"] = "session-new"
                os.environ["FAKE_CHAT_REPLY"] = "重建成功"
                service.handle_text("重试", "om_f2", "ou_trainer")
                need(wait_until(lambda: "重建成功" in bridge.texts(), 10),
                     "重建轮没有成功：%r" % bridge.texts())
                lines = trace_lines(trace)
                need(len(lines) == 2, "重建轮调用次数不对：%r" % lines)
                need("--session-id" not in lines[1],
                     "可疑 session 没有重建：%r" % lines[1])
                item = service.store.get("ou_trainer")
                need(item.get("session_id") == "session-new",
                     "重建后的 session 没落盘：%r" % item)
                need(item.get("suspicious") is False,
                     "重建成功后可疑标记没清：%r" % item)
                need(item.get("turns") == 2,
                     "turns 应按成功轮累加（失败轮不计）：%r" % item)
            finally:
                stop.set()
                service.stop()


@test
def test_31_chat_timeout_marks_suspicious() -> None:
    """聊天⑥：agent 超时 → 错误摘要发飞书、session 标可疑、进程被杀"""
    with Env() as env:
        bridge = FakeChatBridge()
        store = chat_sdk.ChatStateStore(str(env.sb.dir / "chat-state.json"))
        store.update("ou_trainer", session_id="session-slow", suspicious=False)
        service = chat_sdk.ChatService(
            "dsh-test", bridge,
            chat_sdk.DshAgent(str(env.sb.fake_chat), workspace=str(env.sb.ws),
                              timeout=0.8),
            store, stream_interval=0.2)
        stop = threading.Event()
        with EnvVars(FAKE_CHAT_MODE="slow", FAKE_CHAT_SLEEP="5"):
            service.start(stop)
            started = time.time()
            try:
                service.handle_text("慢到超时", "om_timeout", "ou_trainer")
                need(wait_until(lambda: any(
                    "出错了" in t for t in bridge.texts()), 8),
                    "超时没有回错误摘要：%r" % bridge.texts())
                need(any("超时" in t for t in bridge.texts()),
                     "错误摘要不是超时：%r" % bridge.texts())
                need(time.time() - started < 4.0,
                     "超时没及时杀进程：%.1fs" % (time.time() - started))
                item = service.store.get("ou_trainer")
                need(item.get("suspicious") is True,
                     "超时后 session 没标可疑：%r" % item)
                need(service.errors == 1, "errors 计数不对：%d" % service.errors)
            finally:
                stop.set()
                service.stop()


@test
def test_32_chat_long_reply_split() -> None:
    """聊天②：超过 1500 字的回复分条发送，每条不超限"""
    with Env() as env:
        bridge = FakeChatBridge()
        service = make_chat_service(env, bridge, interval=0.2)
        long_reply = "长" * 3200
        stop = threading.Event()
        with EnvVars(FAKE_CHAT_MODE="ok", FAKE_CHAT_SID="session-long",
                     FAKE_CHAT_REPLY=long_reply):
            service.start(stop)
            try:
                service.handle_text("来段长的", "om_long", "ou_trainer")
                need(wait_until(lambda: sum(
                    1 for t in bridge.texts()
                    if set(t) == {"长"}) >= 3, 10),
                    "长回复没有分条：%r" % [len(t) for t in bridge.texts()])
                chunks = [t for t in bridge.texts() if set(t) == {"长"}]
                need(len(chunks) >= 3, "长回复条数不对：%d" % len(chunks))
                need(all(len(t) <= chat_sdk.REPLY_CHUNK_LIMIT for t in chunks),
                     "有超长分条：%r" % [len(t) for t in chunks])
                need(sum(len(t) for t in chunks) == 3200,
                     "分条丢字：%d" % sum(len(t) for t in chunks))
            finally:
                stop.set()
                service.stop()


@test
def test_33_feishu_chat_callback_and_worker_gate() -> None:
    """聊天⑤：飞书 p2p 文本走 on_chat（聊天模式不回旧确认）；任务与聊天互斥"""
    api = FakeFeishuApi()
    seen: List[tuple] = []
    bridge = feishu_sdk.FeishuBridge(
        fake_creds(), "ou_trainer", on_text=None, api_client=api,
        on_chat=lambda text, mid, oid: seen.append((text, mid, oid)))
    bridge._build_message_request = lambda text: SimpleNamespace(
        receive_id="ou_trainer", text=text)
    bridge.handle_event(feishu_event("聊天消息", "ou_trainer",
                                     message_id="om_chat_1"))
    need(seen == [("聊天消息", "om_chat_1", "ou_trainer")],
         "on_chat 入参不对：%r" % seen)
    need(api.sent == [], "聊天模式仍回了旧确认：%r" % api.sent)

    with Env() as env:
        instance = make_worker(env)
        calls: List[str] = []

        class Gate:
            def task_begin(self) -> None:
                calls.append("begin")

            def task_end(self) -> None:
                calls.append("end")

        instance.chat = Gate()
        instance.execute(make_task("t-gate"))
        need(calls == ["begin", "end"],
             "Router 任务没走聊天互斥门：%r" % calls)
        need(env.router.wait_results(1, 10), "任务结果没上报")
        need(env.result()["status"] == "done", "任务没正常完成")

        cfg = worker.parse_args([
            "--chat", "--router", env.router.base_url,
            "--workspace", str(env.sb.ws),
            "--dsh-bin", str(env.sb.fake_chat),
            "--log-file", str(env.sb.dir / "worker2.log"),
            "--chat-state", str(env.sb.dir / "chat-2.json"),
        ])
        wired = worker.Worker(cfg, None, str(env.sb.fake_chat))
        fake_bridge = FakeChatBridge()
        service = worker.build_chat_service(cfg, fake_bridge, wired,
                                            str(env.sb.fake_chat))
        need(service is not None, "build_chat_service 没建出服务")
        need(getattr(fake_bridge, "on_chat", None) == service.handle_text,
             "聊天回调没挂到飞书桥")
        need(getattr(fake_bridge, "on_text", "x") is None,
             "聊天模式下旧 on_text 没关掉")


@test
def test_34_chat_log_failure_is_harmless() -> None:
    """聊天⑤：/chat/log 失败只记日志，不影响 ack/回复/落状态"""
    with Env() as env:
        bridge = FakeChatBridge()
        store = chat_sdk.ChatStateStore(str(env.sb.dir / "chat-state.json"))

        def broken_log(_payload: Any) -> None:
            raise RuntimeError("router down")

        service = chat_sdk.ChatService(
            "dsh-test", bridge,
            chat_sdk.DshAgent(str(env.sb.fake_chat), workspace=str(env.sb.ws),
                              timeout=20),
            store, post_log=broken_log, stream_interval=0.2)
        stop = threading.Event()
        with EnvVars(FAKE_CHAT_MODE="ok", FAKE_CHAT_SID="session-logfail",
                     FAKE_CHAT_REPLY="照常回复"):
            service.start(stop)
            try:
                service.handle_text("旁听挂了也要回", "om_logfail", "ou_trainer")
                need(wait_until(lambda: "照常回复" in bridge.texts(), 10),
                     "旁听失败拖累了聊天：%r" % bridge.texts())
                need(store.get("ou_trainer").get("session_id") == "session-logfail",
                     "旁听失败后状态没落盘：%r" % store.get("ou_trainer"))
            finally:
                stop.set()
                service.stop()


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #


class _TestTimeout(Exception):
    pass


def _on_alarm(_signum: int, _frame: Any) -> None:
    raise _TestTimeout("单个用例超时")


def main() -> int:
    started = time.time()
    signal.signal(signal.SIGALRM, _on_alarm)
    passed = 0
    failed = 0
    total = len(TESTS)
    for index, fn in enumerate(TESTS, 1):
        label = (fn.__doc__ or fn.__name__).strip().splitlines()[0]
        if time.time() - started > GLOBAL_LIMIT_S:
            print("FAIL %02d %s — 超过总时限 %.0fs" % (index, label, GLOBAL_LIMIT_S))
            failed += 1
            continue
        signal.setitimer(signal.ITIMER_REAL, PER_TEST_LIMIT_S)
        try:
            fn()
            print("PASS %02d %s" % (index, label))
            passed += 1
        except Exception:
            traceback.print_exc()
            print("FAIL %02d %s" % (index, label))
            failed += 1
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        sys.stdout.flush()
    elapsed = time.time() - started
    print("TOTAL %d/%d PASS (%.1fs)" % (passed, total, elapsed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
