#!/usr/bin/env python3
"""Task progress watchdog, invoked by the existing five-minute cron job."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.parse import urlencode
import urllib.request


LOG = logging.getLogger("task_watchdog")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return fallback


def settings(home, config_path=None):
    home = Path(home).expanduser()
    value = read_json(config_path or home / "task-watchdog.json", {})
    if not isinstance(value, dict):
        value = {}
    result = {
        "router_url": "http://127.0.0.1:8790",
        "trigger_url": "http://127.0.0.1:8791/trigger",
        "router_token_file": str(home / "secrets/router.token"),
        "trigger_token_file": str(home / "secrets/trigger.token"),
        "trigger_db": str(home / "trigger.db"),
        "state_file": str(home / "state/watchdog_sent.json"),
        "log_file": str(home / "logs/task-watchdog.log"),
        "threshold_s": 900, "max_threshold_s": 3600,
        "backoff_after": 2, "backoff_factor": 2, "backoff_window_s": 3600,
        "quiet_night": True, "workers": {},
    }
    result.update(value)
    if not isinstance(result["workers"], dict):
        result["workers"] = {}
    for worker, paths in list(result["workers"].items()):
        if not isinstance(paths, dict):
            result["workers"][worker] = {}
            continue
        for field in ("state_files", "workspaces"):
            values = paths.get(field, [])
            paths[field] = [item for item in values if isinstance(item, str) and item] if isinstance(values, list) else []
    for key in ("threshold_s", "max_threshold_s", "backoff_after", "backoff_factor", "backoff_window_s"):
        raw = os.environ.get("WATCHDOG_STUCK_S", result[key]) if key == "threshold_s" else result[key]
        try:
            number = float(raw)
            if not math.isfinite(number) or number < 1:
                raise ValueError(key)
            result[key] = number
        except (ValueError, TypeError):
            LOG.warning("watchdog invalid_setting field=%s", key)
            result[key] = {"threshold_s": 900, "max_threshold_s": 3600,
                           "backoff_after": 2, "backoff_factor": 2, "backoff_window_s": 3600}[key]
    result["max_threshold_s"] = max(result["threshold_s"], result["max_threshold_s"])
    return result


def request(method, url, token_file, body=None, token_header="X-Router-Token"):
    token = Path(token_file).expanduser().read_text(encoding="utf-8-sig").strip()
    headers = {token_header: token}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with OPENER.open(req, timeout=8) as response:
        value = json.load(response)
    if not value.get("ok"):
        raise ValueError("service rejected watchdog request")
    return value


def stamp(value, now):
    if isinstance(value, bool):
        return 0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0
    return result if math.isfinite(result) and 0 < result <= now else 0


def peer_history(config, worker, since):
    messages = []
    before = None
    while True:
        query = {"worker": worker, "view": "history", "limit": 100}
        if before is not None:
            query["before"] = before
        page = request("GET", config["router_url"].rstrip("/") + "/messages?" + urlencode(query),
                       config["router_token_file"])
        rows = page.get("messages", [])
        messages.extend(rows)
        cursor = page.get("next_before")
        if (not rows or not page.get("has_more") or min(row.get("created_at", 0) for row in rows) <= since
                or not isinstance(cursor, int) or before is not None and cursor >= before):
            return messages
        before = cursor


def peer_stamps(messages, task_id, worker, now):
    related = set()
    result = []
    for row in sorted(messages, key=lambda item: item.get("seq", 0)):
        if worker not in (row.get("sender_id", row.get("from")), row.get("recipient_id", row.get("to"))):
            continue
        if (row.get("task_id") == task_id or task_id in str(row.get("text", ""))
                or row.get("reply_to") in related):
            related.add(row.get("message_id"))
            result.append(stamp(row.get("created_at"), now))
    return result


def notice_events(config, since, now):
    path = Path(config["trigger_db"]).expanduser().resolve()
    if not path.is_file():
        return []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        return db.execute("SELECT created_at, payload_json, text FROM events"
                          " WHERE created_at>=? AND created_at<=?"
                          " AND source NOT IN ('hermes_watchdog','私聊','router.peer')",
                          (since, now)).fetchall()


def notice_stamps(events, task_id, worker):
    result = []
    for created, payload, text in events:
        try:
            data = json.loads(payload or "{}")
        except (ValueError, TypeError):
            continue
        if (isinstance(data, dict) and data.get("notice") and data.get("worker") == worker
                and (data.get("task_id") == task_id or task_id in text)):
            result.append(created)
    return result


def git(root, *args):
    try:
        command = ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args]
        result = subprocess.run(command, capture_output=True, timeout=4)
        return result.stdout if result.returncode == 0 else b""
    except (OSError, subprocess.TimeoutExpired):
        return b""


def file_stamp(path, now):
    try:
        return stamp(path.stat().st_mtime, now) if path.is_file() else 0
    except OSError:
        return 0


def filesystem(config, task, saved, now):
    worker = config.get("workers", {}).get(task["worker"], {})
    files = [Path(name).expanduser() for name in worker.get("state_files", [])]
    roots = [task["workspace"]] if task.get("workspace") else worker.get("workspaces", [])
    heads = []
    workspaces = saved.setdefault("workspaces", {})
    fs = [file_stamp(path, now) for path in files]
    for raw in dict.fromkeys(roots):
        root = Path(raw).expanduser()
        if not root.is_absolute() or not root.is_dir():
            continue
        root = root.resolve()
        head = git(root, "rev-parse", "HEAD").decode().strip()
        if not head:
            continue
        previous = workspaces.get(str(root), {})
        changed = previous.get("changed_at", 0)
        if head != previous.get("head"):
            changed = now if previous else stamp(git(root, "show", "-s", "--format=%ct", "HEAD").strip(), now)
        heads.append(changed)
        paths = git(root, "diff", "--no-ext-diff", "--name-only", "-z", "--no-renames", "HEAD")
        paths += git(root, "ls-files", "--others", "--exclude-standard", "-z")
        paths = sorted(set(os.fsdecode(name) for name in paths.split(b"\0") if name))
        times = [file_stamp(root / name, now) for name in paths]
        signature = hashlib.sha256(json.dumps(list(zip(paths, times))).encode()).hexdigest()
        if previous and signature != previous.get("files"):
            fs.append(now)
        fs.extend(times)
        workspaces[str(root)] = {"head": head, "changed_at": changed, "files": signature}
    return fs, heads


@contextmanager
def state_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_state(path):
    value = read_json(path, {})
    if (isinstance(value, dict) and value.get("version") == 2
            and isinstance(value.get("tasks"), dict) and isinstance(value.get("false_alarms"), list)):
        return value
    tasks = {task: {"alerted": True} for task, status in value.items() if status == "sent"} if isinstance(value, dict) else {}
    return {"version": 2, "tasks": tasks, "false_alarms": []}


def save_state(path, state):
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def run_once(config, now=None):
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now)
    if config["quiet_night"] and local.hour * 60 + local.minute < 510:
        LOG.info("watchdog quiet window=00:00-08:30")
        return []
    path = Path(config["state_file"]).expanduser()
    with state_lock(path):
        tasks = request("GET", config["router_url"].rstrip("/") + "/tasks?limit=500", config["router_token_file"])["tasks"]
        workers = request("GET", config["router_url"].rstrip("/") + "/workers", config["router_token_file"])["workers"]
        workers = {row["name"]: row for row in workers}
        state = load_state(path)
        state["false_alarms"] = [value for value in state["false_alarms"] if now - value < config["backoff_window_s"]]
        events = notice_events(config, now - config["max_threshold_s"] * 2, now)
        reports = []
        observed = []
        peer_cache = {}
        for task in tasks:
            tid, worker = task["task_id"], task["worker"]
            previous = state["tasks"].get(tid, {})
            if not task.get("in_running_tasks") and not previous.get("alert_at"):
                continue
            saved = state["tasks"].setdefault(tid, {})
            if worker not in peer_cache:
                peer_cache[worker] = peer_history(config, worker, now - config["max_threshold_s"] * 2)
            birth = stamp(task.get("created_at"), now) or stamp(task.get("started_at"), now)
            row = workers.get(worker, {})
            progress = [stamp(task.get("progress_at"), now)]
            if isinstance(task.get("progress"), dict):
                progress.append(stamp(task["progress"].get("updated_at"), now))
            if row.get("task_id") in (None, tid):
                progress.extend(stamp(row.get(key), now) for key in ("last_progress_at", "progress_at"))
                progress = list(set(progress))
            formal = max([birth, stamp(task.get("started_at"), now), stamp(task.get("accepted_at"), now)] + progress)
            fs, heads = filesystem(config, task, saved, now)
            signals = {"notice": notice_stamps(events, tid, worker),
                       "peer": peer_stamps(peer_cache[worker], tid, worker, now),
                       "fs": fs, "head": heads, "progress": progress}
            signals = {name: [value for value in values if birth <= value <= now and value > 0]
                       for name, values in signals.items()}
            alert_at = saved.get("alert_at")
            if alert_at and not saved.get("false_alarm_counted"):
                late = any(alert_at - saved["alert_threshold"] < value <= alert_at
                           for values in signals.values() for value in values)
                if late:
                    state["false_alarms"].append(now)
                    saved["false_alarm_counted"] = True
                    LOG.info("watchdog false_alarm task=%s reason=late_activity_evidence", tid)
            latest = max([formal, saved.get("last_activity", 0)] + [value for values in signals.values() for value in values])
            saved["last_activity"] = latest
            observed.append((task, saved, birth, formal, signals, latest))
        steps = len(state["false_alarms"]) // int(config["backoff_after"])
        threshold = config["threshold_s"]
        for _ in range(min(steps, 16)):
            threshold = min(config["max_threshold_s"], threshold * config["backoff_factor"])
        if threshold != state.get("threshold_s", config["threshold_s"]):
            LOG.info("watchdog backoff threshold=%ds false_alarms=%d", threshold, len(state["false_alarms"]))
        state["threshold_s"] = threshold
        for task, saved, birth, formal, signals, latest in observed:
            tid, worker = task["task_id"], task["worker"]
            counts = {name: sum(now - value < threshold for value in values) for name, values in signals.items()}
            idle = max(0, now - latest)
            decision = "active" if idle < threshold else "alert"
            if saved.get("alerted"):
                decision = "dedup"
            elif not task.get("in_running_tasks") or not birth:
                decision = "terminal"
            if decision == "alert":
                text = "看门狗：任务 %s 已 %d 分钟无进展（worker=%s）" % (tid, idle // 60, worker)
                try:
                    response = request("POST", config["trigger_url"], config["trigger_token_file"], {
                        "source": "hermes_watchdog", "kind": "watchdog", "level": "notify",
                        "priority": 0, "actor": "溪流", "text": text, "event_id": "watchdog-" + tid},
                        token_header="X-Trigger-Token")
                    saved.update(alerted=True, alert_at=now, alert_threshold=threshold, trigger_id=response.get("id"))
                except (OSError, ValueError) as error:
                    LOG.warning("watchdog alert_failed task=%s error=%s", tid, type(error).__name__)
                    decision = "delivery_failed"
            report = {"task": tid, "silent": max(0, now - formal), "idle": idle,
                      "signals": counts, "threshold": threshold, "decision": decision}
            LOG.info("watchdog judge task=%s silent=%ds signals=notice:%d,peer:%d,fs:%d,head:%d,progress:%d idle=%ds threshold=%ds decision=%s",
                     tid, report["silent"], counts["notice"], counts["peer"], counts["fs"], counts["head"], counts["progress"], idle, threshold, decision)
            reports.append(report)
        live = {task["task_id"] for task in tasks if task.get("in_running_tasks")}
        state["tasks"] = {tid: value for tid, value in state["tasks"].items() if tid in live or value.get("alerted")}
        save_state(path, state)
        return reports


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    args = parser.parse_args()
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    config = settings(home, args.config)
    log = Path(config["log_file"]).expanduser()
    log.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[
        logging.StreamHandler(), RotatingFileHandler(log, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")])
    try:
        reports = run_once(config)
    except (OSError, ValueError, sqlite3.Error) as error:
        LOG.warning("watchdog probe_failed error=%s", type(error).__name__)
        return 1
    return 1 if any(row["decision"] == "delivery_failed" for row in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
