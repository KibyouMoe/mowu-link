#!/usr/bin/env python3
"""codex worker 自测：假 Router（本地 HTTP）+ 假 codex（临时 shell 脚本），不调用真模型。

    ./selftest_worker.py

覆盖：注册、收任务、codex argv/代理环境注入、结果字段、心跳、4000 字截尾、
失败退出码、超时、串行不重叠、断线重连并重新注册、SIGTERM（空闲/执行中）、
重复任务去重、配置文件加载、二进制探测优先级、超长 prompt 走 stdin、语法、纯函数，
以及 SSE 空闲/心跳不误判断线、真断才重连。
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
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import worker  # noqa: E402

worker.log.propagate = False
worker.log.addHandler(logging.NullHandler())

TESTS: List[Callable[[], None]] = []
GLOBAL_LIMIT_S = 55.0
PER_TEST_LIMIT_S = 25.0

FAKE_CODEX = """#!/bin/sh
# selftest 用的假 codex：回显参数与代理环境，按 FAKE_CODEX_MODE 改变行为
PY=@PY@
mode="${FAKE_CODEX_MODE:-ok}"
trace="${FAKE_CODEX_TRACE:-}"
stamp() { "$PY" -c 'import time;print("%.3f" % time.time())'; }
if [ -n "$trace" ]; then
    printf 'start %s\\n' "$(stamp)" >> "$trace"
    printf 'env %s|%s|%s|%s\\n' "${HTTPS_PROXY:-}" "${ALL_PROXY:-}" \\
        "${HTTP_PROXY:-}" "${http_proxy:-}" >> "$trace"
    printf 'args %s\\n' "$(printf '%s' "$*" | tr '\\n' ' ')" >> "$trace"
fi
rc=0
case "$mode" in
  sleep)
    sleep "${FAKE_CODEX_SLEEP:-2}"
    echo "fake-codex: slow done"
    ;;
  fail)
    echo "fake-codex: partial output before failure"
    echo "fake-codex: boom on stderr" >&2
    rc=3
    ;;
  big)
    echo "fake-codex: big output"
    "$PY" -c 'import sys; sys.stdout.write("\\x1b[32m" + "A"*4800 + "TAIL-MARKER-0123456789" + "\\x1b[0m\\n")'
    ;;
  stdin)
    n=$("$PY" -c 'import sys; print(len(sys.stdin.buffer.read()))')
    echo "fake-codex: stdin-bytes=$n"
    ;;
  *)
    echo "fake-codex: argv=$*"
    echo "fake-codex: done"
    ;;
esac
if [ -n "$trace" ]; then printf 'end %s\\n' "$(stamp)" >> "$trace"; fi
exit $rc
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


class FakeRouter:
    def __init__(self, tasks: Optional[List[Dict[str, Any]]] = None,
                 drop_first_stream: bool = False,
                 stream_mode: str = "ping",
                 ping_interval: float = 0.3,
                 hold_s: float = 6.0) -> None:
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
        self.stream_opens = 0
        self.stream_auth: List[Optional[str]] = []
        self.stream_xtoken: List[Optional[str]] = []
        self.closed = False
        self.cond = threading.Condition()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
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
                else:
                    self._json(404, {"error": "not_found"})

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
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
        self.dir = Path(tempfile.mkdtemp(prefix="codex-selftest-"))
        self.ws = self.dir / "ws"
        self.ws.mkdir()
        self.trace = self.dir / "trace.log"
        self.fake_codex = self.dir / "fake-codex"
        self.fake_codex.write_text(
            FAKE_CODEX.replace("@PY@", shlex.quote(sys.executable)),
            encoding="utf-8")
        self.fake_codex.chmod(0o755)
        Sandbox._counter += 1
        self.tag = Sandbox._counter

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class Harness:
    def __init__(self, sandbox: Sandbox, router: FakeRouter,
                 extra: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None,
                 name: str = "codex-test", config: Optional[Path] = None) -> None:
        self.sandbox = sandbox
        self.err_path = sandbox.dir / ("worker-%d.err" % sandbox.tag)
        self._err = open(self.err_path, "wb")
        args = [sys.executable, str(HERE / "worker.py")]
        if config is not None:
            args += ["--config", str(config)]
        else:
            args += ["--router", router.base_url,
                     "--name", name,
                     "--workspace", str(sandbox.ws),
                     "--log-file", str(sandbox.dir / "worker.log"),
                     "--codex-bin", str(sandbox.fake_codex)]
        args += list(extra or [])
        merged = os.environ.copy()
        merged["FAKE_CODEX_TRACE"] = str(sandbox.trace)
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
               name: str = "codex-test",
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
    """trace 行 → [(kind, value)]；kind=start/end 时 value 是时间戳字符串。"""
    entries: List[tuple] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        kind, _, value = line.partition(" ")
        if kind:
            entries.append((kind, value))
    return entries


def trace_values(path: Path, kind: str) -> List[str]:
    return [value for item_kind, value in read_trace(path) if item_kind == kind]


def trace_times(path: Path, kind: str) -> List[float]:
    times: List[float] = []
    for value in trace_values(path, kind):
        try:
            times.append(float(value))
        except ValueError:
            pass
    return times


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
        need(reg.get("name") == "codex-test", "注册 name 不对：%r" % reg)
        need(reg.get("version") == worker.VERSION,
             "注册 version 不对：%r" % reg)
        need(reg.get("version") == "0.1.0", "版本不是 0.1.0：%r" % reg.get("version"))
        need(env.router.wait_streams(1, 10), "没有打开 /stream")


@test
def test_02_task_result_done() -> None:
    """任务：收 SSE 任务 → codex exec 执行 → /result 完整、status=done、代理/argv 正确"""
    with Env(tasks=[make_task("t-100")]) as env:
        env.launch(["--timeout", "20"])
        need(env.router.wait_results(1, 20), "没有收到结果")
        res = env.result()
        for key in ("name", "task_id", "status", "summary", "artifacts", "evidence"):
            need(key in res, "结果缺字段 %s：%r" % (key, res))
        need(res["name"] == "codex-test", "结果 name 不对")
        need(res["task_id"] == "t-100", "结果 task_id 不对")
        need(res["status"] == "done", "status 不是 done：%r" % res["status"])
        need("hello-from-router" in res["summary"], "goal 没传给 codex")
        need("ctx-abc" in res["summary"], "context 没传给 codex")
        need("fake-codex: done" in res["summary"], "summary 不是 codex stdout")
        need(res["artifacts"] == [], "artifacts 应为空数组")
        evidence = res["evidence"]
        need(isinstance(evidence, list) and evidence, "evidence 不是非空数组")
        need(evidence[0].get("type") == "exit", "evidence[0].type 不是 exit")
        need(evidence[0].get("code") == 0, "evidence[0].code 不是 0")
        need(isinstance(evidence[0].get("duration_ms"), int),
             "evidence 缺 duration_ms：%r" % evidence)

        # 子命令必须是 codex exec --skip-git-repo-check + 任务文本（含 Context 段）
        argv = trace_values(env.sb.trace, "args")
        need(argv, "假 codex 没被调用")
        need(argv[0].startswith("exec --skip-git-repo-check "),
             "codex 子命令不对：%r" % argv[0])
        need("## Context" in argv[0] and "ctx-abc" in argv[0],
             "prompt 里没有 Context 段：%r" % argv[0][:300])

        # 代理环境：ALL_PROXY 清空，HTTP(S)_PROXY 指向 http 代理
        envs = trace_values(env.sb.trace, "env")
        need(envs, "假 codex 没记录代理环境")
        https, all_proxy, http, http_lower = envs[0].split("|")
        need(https == worker.DEFAULT_PROXY, "HTTPS_PROXY 不对：%r" % https)
        need(http == worker.DEFAULT_PROXY, "HTTP_PROXY 不对：%r" % http)
        need(http_lower == worker.DEFAULT_PROXY, "http_proxy 不对：%r" % http_lower)
        need(all_proxy == "", "ALL_PROXY 没清掉：%r" % all_proxy)


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
                   env={"FAKE_CODEX_MODE": "sleep", "FAKE_CODEX_SLEEP": "2"})
        need(env.router.wait_results(1, 20), "没有收到结果")
        running = [hb["body"] for hb in env.router.heartbeats
                   if hb["body"].get("state") == "running"]
        need(len(running) >= 2, "running 心跳太少：%d" % len(running))
        need(all(hb.get("task_id") == "t-hb" for hb in running),
             "running 心跳缺 task_id")
        need(all(float(hb.get("progress_at") or 0) > 0 for hb in running),
             "心跳缺 progress_at")
        need(all(hb.get("name") == "codex-test" for hb in running),
             "心跳缺 name")


@test
def test_05_summary_tail_4000() -> None:
    """summary：stdout 超长只留末尾 4000 字，结尾完整，ANSI 已清掉"""
    with Env(tasks=[make_task("t-big")]) as env:
        env.launch(["--timeout", "20"], env={"FAKE_CODEX_MODE": "big"})
        need(env.router.wait_results(1, 20), "没有收到结果")
        summary = env.result()["summary"]
        need(len(summary) == 4000, "summary 长度不是 4000：%d" % len(summary))
        need(summary.endswith("TAIL-MARKER-0123456789"),
             "summary 没保留 stdout 末尾")
        need("\x1b[" not in summary, "summary 里还有 ANSI 颜色码")


@test
def test_06_failure_exit_code() -> None:
    """失败：codex 退出码 3 → status=failed，evidence.code=3，stderr 进 summary"""
    with Env(tasks=[make_task("t-fail")]) as env:
        env.launch(["--timeout", "20"], env={"FAKE_CODEX_MODE": "fail"})
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
                   env={"FAKE_CODEX_MODE": "sleep", "FAKE_CODEX_SLEEP": "10"})
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
                   env={"FAKE_CODEX_MODE": "sleep", "FAKE_CODEX_SLEEP": "0.6"})
        need(env.router.wait_results(2, 25), "两个任务没有都完成")
        ids = sorted(res["body"]["task_id"] for res in env.router.results)
        need(ids == ["t-a", "t-b"], "结果 task_id 不对：%r" % ids)
        starts = trace_times(env.sb.trace, "start")
        ends = trace_times(env.sb.trace, "end")
        need(len(starts) == 2 and len(ends) == 2,
             "codex 调用次数不对：%r" % read_trace(env.sb.trace))
        need(starts[1] >= ends[0] - 0.1,
             "两个 codex 执行重叠了：start2=%.3f end1=%.3f" % (starts[1], ends[0]))


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
        need("codex-worker 退出" in env.log_text(), "日志没有优雅退出记录")


@test
def test_11_sigterm_interrupts_task() -> None:
    """退出：任务执行中 SIGTERM → 杀进程组、报 failed/interrupted、worker 退出 0"""
    with Env(tasks=[make_task("t-int")]) as env:
        harness = env.launch(["--timeout", "30"],
                             env={"FAKE_CODEX_MODE": "sleep",
                                  "FAKE_CODEX_SLEEP": "15"})
        # 等假 codex 真的起来（trace 里出现 start）
        deadline = time.time() + 10
        while time.time() < deadline and not trace_values(env.sb.trace, "start"):
            time.sleep(0.1)
        need(trace_values(env.sb.trace, "start"), "codex 没启动")
        started = time.time()
        code = harness.stop(signal.SIGTERM, timeout=8.0)
        elapsed = time.time() - started
        need(code == 0, "SIGTERM 退出码不是 0：%r" % code)
        need(elapsed < 8.0, "任务中 SIGTERM 退得太慢：%.1fs" % elapsed)
        need(env.router.wait_results(1, 5), "中断的任务没有上报结果")
        res = env.result()
        need(res["task_id"] == "t-int", "中断结果 task_id 不对")
        need(res["status"] == "failed", "中断结果 status 不是 failed")
        evidence = res["evidence"][0]
        need(evidence.get("reason") == "interrupted",
             "evidence.reason 不是 interrupted：%r" % evidence)
        need(evidence.get("signal") == "SIGTERM", "evidence.signal 不是 SIGTERM")


@test
def test_12_duplicate_task() -> None:
    """去重：同一 task_id 推两次只执行一次，结果补发两次"""
    duplicate = make_task("t-dup")
    with Env(tasks=[dict(duplicate), dict(duplicate)]) as env:
        env.launch(["--timeout", "20"])
        need(env.router.wait_results(2, 20), "重复任务没有补发结果")
        starts = trace_times(env.sb.trace, "start")
        need(len(starts) == 1, "重复任务被重复执行：%r" % read_trace(env.sb.trace))
        need(all(res["body"]["status"] == "done" for res in env.router.results),
             "补发结果 status 不对")


@test
def test_13_config_file() -> None:
    """配置：--config 读 worker.json（name/workspace/codex_bin/log_file）"""
    with Env(tasks=[make_task("t-cfg")]) as env:
        config_path = env.sb.dir / "worker.json"
        config_path.write_text(json.dumps({
            "router": env.router.base_url,
            "name": "cfg-name",
            "workspace": str(env.sb.ws),
            "codex_bin": str(env.sb.fake_codex),
            "proxy": "http://127.0.0.1:10808",
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
def test_14_syntax_and_files() -> None:
    """静态：worker/selftest 可编译、start-worker.sh 语法与可执行位、模板合法"""
    pycache = Path(tempfile.mkdtemp(prefix="codex-pycache-"))
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
        cfg = json.loads((HERE / "worker.json").read_text(encoding="utf-8"))
        need(cfg.get("name") == "codex", "worker.json 的 name 不是 codex")
        need(cfg.get("codex_bin") == "auto", "worker.json 的 codex_bin 不是 auto")
        need(cfg.get("proxy"), "worker.json 缺 proxy")
        readme = (HERE / "README.md").read_text(encoding="utf-8")
        need("Router" in readme and "codex exec" in readme, "README 缺内容")
        script = (HERE / "start-worker.sh").read_text(encoding="utf-8")
        need("ALL_PROXY" in script and "HTTPS_PROXY" in script,
             "start-worker.sh 没注入代理环境")
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

    # 代理环境：清 socks、设 http 代理；none/空=清空
    fake_env = {"ALL_PROXY": "socks5://127.0.0.1:1080",
                "all_proxy": "socks5://127.0.0.1:1080",
                "HTTP_PROXY": "http://old:1", "https_proxy": "http://old:1"}
    out = worker.apply_proxy_env("http://127.0.0.1:10808", fake_env)
    need("ALL_PROXY" not in out and "all_proxy" not in out, "ALL_PROXY 没清掉")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        need(out.get(key) == "http://127.0.0.1:10808", "%s 没被改写" % key)
    cleared = worker.apply_proxy_env("none", {"HTTPS_PROXY": "http://x:1",
                                              "ALL_PROXY": "socks5://x:1"})
    need("HTTPS_PROXY" not in cleared and "ALL_PROXY" not in cleared,
         "proxy=none 没清代理")
    need(worker.normalize_proxy("") == "" and worker.normalize_proxy("off") == "",
         "normalize_proxy 没识别直连")
    need(worker.strip_ansi("\x1b[31mred\x1b[0m") == "red", "strip_ansi 不对")

    cfg_path = Path(tempfile.mkdtemp(prefix="codex-cfg-")) / "worker.json"
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
def test_15_resolve_codex_priority() -> None:
    """探测：--codex-bin 显式值 > PATH 里的 codex > 固定兜底路径"""
    tmp = Path(tempfile.mkdtemp(prefix="codex-bin-"))
    old_path = os.environ.get("PATH", "")
    old_fallback = worker.FALLBACK_CODEX_BIN
    try:
        explicit = tmp / "explicit-codex"
        explicit.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        explicit.chmod(0o755)
        need(worker.resolve_codex(str(explicit)) == str(explicit),
             "显式路径没优先")
        need(worker.resolve_codex(str(tmp / "missing")) is None,
             "显式路径不存在却没报错")

        bindir = tmp / "bin"
        bindir.mkdir()
        on_path = bindir / "codex"
        on_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        on_path.chmod(0o755)
        os.environ["PATH"] = str(bindir)
        need(worker.resolve_codex("auto") == str(on_path), "PATH 里的 codex 没找到")

        os.environ["PATH"] = str(tmp / "empty")
        (tmp / "empty").mkdir()
        fallback = tmp / "fallback-codex"
        fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fallback.chmod(0o755)
        worker.FALLBACK_CODEX_BIN = str(fallback)
        need(worker.resolve_codex("auto") == str(fallback),
             "PATH 没有时没用兜底路径")
    finally:
        os.environ["PATH"] = old_path
        worker.FALLBACK_CODEX_BIN = old_fallback
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_16_long_prompt_via_stdin() -> None:
    """超长 prompt：>200KB 时走 stdin（argv 只传 -），codex 能读全"""
    goal = "X" * 200_100
    with Env(tasks=[make_task("t-stdin", goal=goal)]) as env:
        env.launch(["--timeout", "20"], env={"FAKE_CODEX_MODE": "stdin"})
        need(env.router.wait_results(1, 20), "超长 prompt 没有结果")
        res = env.result()
        need(res["status"] == "done", "超长 prompt 任务失败")
        argv = trace_values(env.sb.trace, "args")
        need(argv and argv[0].strip() == "exec --skip-git-repo-check -",
             "超长 prompt 没走 stdin：%r" % (argv[:1] or []))
        need("fake-codex: stdin-bytes=" in res["summary"],
             "假 codex 没读到 stdin：%r" % res["summary"][-200:])
        read = int(res["summary"].split("stdin-bytes=")[1].split()[0])
        need(read > 200_000, "stdin 字数不够：%d" % read)


@test
def test_17_idle_stream_not_disconnect() -> None:
    """空闲：SSE 建立后 3 秒零字节 → 读超时不算断线，register 只 1 次"""
    with Env(stream_mode="silent") as env:
        env.launch(["--timeout", "20", "--idle-timeout", "90"])
        need(env.router.wait_registrations(1, 10), "worker 没有注册")
        need(env.router.wait_streams(1, 10), "没有打开 /stream")
        time.sleep(2.2)
        opens = len(env.router.registrations)
        need(opens == 1, "空闲 2 秒被误判断线，register=%d 次" % opens)
        need(env.router.stream_opens == 1,
             "空闲 2 秒重开了 /stream：%d 次" % env.router.stream_opens)
        need(env.harness is not None and env.harness.proc.poll() is None,
             "worker 在空闲流上退出了")
        need("连接中断" not in env.log_text(),
             "空闲流被记成断线：%r" % env.log_text()[-400:])


@test
def test_18_ping_keepalive_not_disconnect() -> None:
    """心跳：每隔 1 秒 `: ping` 持续 4 秒 → 仍不重连"""
    with Env(stream_mode="slow_ping", ping_interval=1.0, hold_s=5.0) as env:
        env.launch(["--timeout", "20", "--idle-timeout", "90"])
        need(env.router.wait_registrations(1, 10), "worker 没有注册")
        time.sleep(3.2)
        need(env.router.pings_sent >= 2,
             "假 Router 没发出 ping：%d" % env.router.pings_sent)
        opens = len(env.router.registrations)
        need(opens == 1, "ping 保活期间重连了，register=%d 次" % opens)
        need(env.router.stream_opens == 1,
             "ping 保活期间重开了 /stream：%d 次" % env.router.stream_opens)


@test
def test_19_true_close_reconnects() -> None:
    """真断：Router 关闭 SSE 连接 → 必须重连并重新注册（register 2 次）"""
    with Env(drop_first_stream=True) as env:
        env.launch(["--timeout", "20"])
        need(env.router.wait_registrations(2, 15), "真断开后没有重连注册")
        need(env.router.wait_streams(2, 10), "真断开后没有重开 /stream")
        time.sleep(0.5)
        opens = len(env.router.registrations)
        need(opens == 2, "重连次数异常：register=%d 次" % opens)


@test
def test_20_iter_sse_timeout_semantics() -> None:
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
