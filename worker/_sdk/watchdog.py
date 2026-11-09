"""任务看门狗：既没有 stdout 增长、也没有 CPU 时间增长 → 判定卡死。

判定口径（训练员定的「卡死」）：
- 子进程组的 stdout/stderr 文件大小连续 ``stall_seconds`` 秒没变；
- **并且** 整个进程组的累计 CPU 时间也没变（排除“闷头算、不打印”的正常情况）。

CPU 用 ``ps -A -o pgid=,time=`` 汇总整个进程组，macOS / Linux 都适用。
只依赖标准库。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from typing import Callable, Optional


def parse_ps_time(value: str) -> float:
    """解析 ps 的 TIME：``mm:ss.cc`` / ``hh:mm:ss`` / ``dd-hh:mm:ss`` → 秒。"""
    text = (value or "").strip()
    if not text:
        return 0.0
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        try:
            days = int(head)
        except ValueError:
            return 0.0
    parts = text.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return 0.0
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return days * 86400 + seconds


def process_group_cpu_seconds(pgid: int) -> Optional[float]:
    """汇总进程组的累计 CPU 秒数；ps 失败返回 None。"""
    if not pgid:
        return None
    try:
        proc = subprocess.run(
            ["ps", "-A", "-o", "pgid=,time="],
            capture_output=True, text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    total = 0.0
    found = False
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            group = int(parts[0])
        except ValueError:
            continue
        if group != pgid:
            continue
        found = True
        total += parse_ps_time(parts[1])
    return total if found else 0.0


def terminate_group(proc: subprocess.Popen, grace: float = 1.0) -> None:
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


class StallWatchdog:
    """在 ``check()`` 里判断是否卡死；``kill()`` 负责收尸。

    ``output_size`` / ``cpu_reader`` / ``clock`` 都可注入，自测不需要真等 300 秒。
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        output_size: Callable[[], int],
        stall_seconds: float,
        clock: Callable[[], float] = time.time,
        cpu_reader: Optional[Callable[[int], Optional[float]]] = None,
        kill_grace: float = 1.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.proc = proc
        self.output_size = output_size
        self.stall_seconds = max(0.001, float(stall_seconds))
        self.clock = clock
        self.cpu_reader = cpu_reader or process_group_cpu_seconds
        self.kill_grace = kill_grace
        self.log = logger or logging.getLogger("mowu-worker.watchdog")
        self._pgid = proc.pid
        self._last_size: Optional[int] = None
        self._last_cpu: Optional[float] = None
        self._last_progress = self.clock()
        self.stalled_for = 0.0

    def check(self) -> Optional[float]:
        """返回已静默秒数（≥ stall_seconds 即卡死）；未卡死返回 None。"""
        if self.proc.poll() is not None:
            return None
        now = self.clock()
        try:
            size = int(self.output_size() or 0)
        except OSError:
            size = self._last_size or 0
        try:
            cpu = self.cpu_reader(self._pgid)
        except Exception:  # noqa: BLE001 - 监控失败不能杀错进程
            cpu = self._last_cpu
        if cpu is None:
            cpu = self._last_cpu
        if self._last_size is None:
            progress = True
        else:
            progress = size != self._last_size
            if not progress and cpu is not None and self._last_cpu is not None:
                progress = abs(cpu - self._last_cpu) > 1e-6
        if progress:
            self._last_progress = now
        self._last_size = size
        self._last_cpu = cpu
        idle = now - self._last_progress
        self.stalled_for = idle
        if idle >= self.stall_seconds:
            return idle
        return None

    def kill(self) -> None:
        self.log.warning("看门狗终止进程组 pgid=%s（静默 %.1fs）",
                         self._pgid, self.stalled_for)
        terminate_group(self.proc, self.kill_grace)
