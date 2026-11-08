#!/usr/bin/env python3
"""mc_worker — 挂在本地母线上的 MC 对话 worker（与 router 对跑三轮）。

流程:
    bus /events (worker-a)  ──[R] round1──▶ 发 [W] round2 在吗
                            ──[R] round2──▶ 发 [W] round3 还在吗
                            ──[R] round3──▶ 发 [W] done 三轮完成  → 退出 0

发送：POST :8765/action {"type":"control","enabled":true} 然后
      POST :8765/action {"type":"chat","text":"..."}

去重：自己发的消息会经同一玩家回流到母线，text 以 `[W]` 开头的一律忽略，
      只有 `[R] roundN` 能驱动状态机。

用法:
    python3 mc_worker.py --kick
    python3 mc_worker.py --kick --since 0
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

HERE = Path(__file__).resolve().parent
DEFAULT_LOG = HERE / "mc_worker.log"
DEFAULT_BUS = "http://127.0.0.1:8792"
DEFAULT_BRIDGE = "http://127.0.0.1:8765"
SELF_PREFIX = "[W]"
ROUTER_PREFIX = "[R]"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3

_OPENER = build_opener(ProxyHandler({}))

log = logging.getLogger("mc_worker")

# stage -> (router marker that advances it, text we answer with, human name)
STEPS = [
    (0, "[r] round1", "[W] round2 在吗", "round1->round2"),
    (1, "[r] round2", "[W] round3 还在吗", "round2->round3"),
    (2, "[r] round3", "[W] done 三轮完成", "round3->done"),
]


def setup_logging(path: Path, verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG)
    log.propagate = False
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        str(path), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
        encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(sh)


def http_json(url: str, payload: Optional[Dict[str, Any]] = None,
              timeout: float = 15.0) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = Request(url, data=data, headers=headers,
                  method="POST" if data is not None else "GET")
    with _OPENER.open(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def strip_speaker(text: str) -> str:
    """'<SuperCreek> [W] hi' -> '[W] hi'."""
    t = (text or "").strip()
    if t.startswith("<") and ">" in t:
        t = t.split(">", 1)[1].lstrip()
    return t


class Worker:
    def __init__(self, args: argparse.Namespace) -> None:
        self.bus = args.bus_url.rstrip("/")
        self.bridge = args.bridge.rstrip("/")
        self.sub = args.subscriber
        self.wait = float(args.wait)
        self.poll_interval = float(args.poll_interval)
        self.max_seconds = float(args.max_seconds)
        self.cursor = int(args.since) if args.since is not None else -1
        self.kick = bool(args.kick)
        self.stage = 0
        self.handled: List[str] = []       # markers already acted on
        self.sent: List[str] = []
        self.started = time.time()
        self.control_ok = False
        self.done = False

    # -- bus --------------------------------------------------------------- #
    def bootstrap_cursor(self) -> None:
        if self.cursor >= 0:
            log.info("cursor pinned by --since: %d", self.cursor)
            return
        try:
            health = http_json(self.bus + "/bus/health", timeout=5)
            self.cursor = int(health.get("last_seq") or 0)
            log.info("cursor from /bus/health last_seq=%d", self.cursor)
        except Exception as exc:  # noqa: BLE001
            self.cursor = 0
            log.warning("cannot read /bus/health (%s); cursor=0", exc)

    def fetch(self, wait: float) -> List[Dict[str, Any]]:
        """Long-poll emulation over /events (server returns immediately)."""
        deadline = time.time() + max(0.0, wait)
        collected: List[Dict[str, Any]] = []
        while True:
            qs = urlencode({"subscriber": self.sub, "since": self.cursor,
                            "limit": 200})
            try:
                data = http_json("%s/events?%s" % (self.bus, qs), timeout=10)
            except (URLError, HTTPError, OSError, ValueError) as exc:
                log.warning("bus poll failed: %s: %s", type(exc).__name__, exc)
                time.sleep(1.0)
                return collected
            sub = (data or {}).get("subscription") or {}
            if data.get("gap"):
                log.warning("bus gap: asked since=%s oldest=%s last=%s",
                            self.cursor, data.get("oldest_seq"),
                            data.get("last_seq"))
            if sub.get("expired"):
                log.warning("subscription %s expired; renewing", self.sub)
                try:
                    http_json(self.bus + "/renew", {"name": self.sub}, timeout=5)
                except Exception as exc:  # noqa: BLE001
                    log.warning("renew failed: %s", exc)
            events = data.get("events") or []
            if events:
                collected.extend(events)
                self.cursor = max(int(e.get("seq") or 0) for e in events)
                return collected
            if time.time() >= deadline:
                return collected
            time.sleep(min(self.poll_interval, max(0.05, deadline - time.time())))

    # -- bridge ------------------------------------------------------------ #
    def send_chat(self, text: str) -> bool:
        try:
            if not self.control_ok:
                ack = http_json(self.bridge + "/action",
                                {"type": "control", "enabled": True})
                log.info("bridge control -> %s", json.dumps(ack, ensure_ascii=False))
                if not ack.get("ok"):
                    return False
                self.control_ok = True
            t0 = time.time()
            res = http_json(self.bridge + "/action", {"type": "chat", "text": text})
            ok = bool(res.get("ok"))
            log.info("SENT %r ok=%s in %.0fms resp=%s", text, ok,
                     (time.time() - t0) * 1000.0,
                     json.dumps(res, ensure_ascii=False)[:200])
            if ok:
                self.sent.append(text)
            return ok
        except (URLError, HTTPError, OSError, ValueError) as exc:
            log.error("SENT FAILED %r: %s: %s", text, type(exc).__name__, exc)
            self.control_ok = False
            return False

    # -- state machine ------------------------------------------------------ #
    def handle_event(self, ev: Dict[str, Any]) -> None:
        text = str(ev.get("text") or "")
        actor = str(ev.get("actor") or "")
        log.info("EVENT seq=%s id=%s actor=%r text=%r",
                 ev.get("seq"), ev.get("event_id"), actor, text)
        if actor.strip().startswith(SELF_PREFIX):
            log.info("SKIP self-actor prefix: %r", actor)
            return
        body = strip_speaker(text)
        if body.startswith(SELF_PREFIX) or SELF_PREFIX in body[:6]:
            log.info("SKIP self-text prefix: %r", body[:60])
            return
        low = body.lower()
        if self.stage >= len(STEPS):
            return
        _, marker, reply, name = STEPS[self.stage]
        if marker in low:
            if marker in self.handled:
                return
            self.handled.append(marker)
            log.info("ROUND %s matched %r -> sending %r", name, marker, reply)
            ok = self.send_chat(reply)
            if not ok:
                log.error("ROUND %s send failed; staying at stage %d", name, self.stage)
                return
            self.stage += 1
            if self.stage >= len(STEPS):
                self.done = True
                log.info("DONE stage=%d sent=%s", self.stage,
                         json.dumps(self.sent, ensure_ascii=False))

    # -- main --------------------------------------------------------------- #
    def run(self) -> int:
        log.info("mc_worker starting: bus=%s bridge=%s subscriber=%s kick=%s "
                 "wait=%.1fs max=%.0fs", self.bus, self.bridge, self.sub,
                 self.kick, self.wait, self.max_seconds)
        self.bootstrap_cursor()
        # 先探一次母线，确保 /events 可用（同时注册订阅）
        try:
            http_json("%s/events?%s" % (self.bus, urlencode(
                {"subscriber": self.sub, "since": self.cursor})), timeout=10)
            log.info("bus reachable; subscriber=%s registered", self.sub)
        except Exception as exc:  # noqa: BLE001
            log.error("bus unreachable: %s: %s", type(exc).__name__, exc)
            return 2

        if self.kick:
            log.info("KICK sending %r", "[W] round1 溪流在吗")
            if not self.send_chat("[W] round1 溪流在吗"):
                log.error("kick send failed; aborting")
                return 3

        deadline = self.started + self.max_seconds
        while not self.done and time.time() < deadline:
            events = self.fetch(min(self.wait, max(0.0, deadline - time.time())))
            for ev in events:
                self.handle_event(ev)
                if self.done:
                    break
        if self.done:
            log.info("EXIT 0: three rounds complete, worker pid=%d", os.getpid())
            return 0
        log.error("EXIT 4: timeout after %.0fs at stage %d (cursor=%d)",
                  self.max_seconds, self.stage, self.cursor)
        return 4


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MC chat worker on the local bus")
    p.add_argument("--bus-url", default=os.environ.get("MOWU_BUS", DEFAULT_BUS))
    p.add_argument("--bridge", default=os.environ.get("MOWU_BRIDGE", DEFAULT_BRIDGE))
    p.add_argument("--subscriber", default="worker-a")
    p.add_argument("--since", type=int, default=None,
                   help="bus cursor (default: current last_seq)")
    p.add_argument("--wait", type=float, default=10.0,
                   help="long-poll window seconds (default 10)")
    p.add_argument("--poll-interval", type=float, default=0.6,
                   help="sleep between /events calls while waiting")
    p.add_argument("--max-seconds", type=float, default=900.0)
    p.add_argument("--kick", action="store_true",
                   help="send '[W] round1 溪流在吗' on startup")
    p.add_argument("--log", default=str(DEFAULT_LOG))
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(Path(os.path.expanduser(args.log)), args.verbose)
    return Worker(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
