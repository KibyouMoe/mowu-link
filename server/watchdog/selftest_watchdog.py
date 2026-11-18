#!/usr/bin/env python3
"""Real files, Git worktrees, SQLite and authenticated HTTP; no production writes."""

from datetime import datetime
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import watchdog_ping as watchdog


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="task-watchdog-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = datetime(2026, 11, 16, 12, 0).timestamp()
        self.tasks = [{"task_id": "t-files", "worker": "worker", "in_running_tasks": True,
                       "created_at": self.now - 1000, "started_at": self.now - 1000}]
        self.workers = [{"name": "worker", "task_id": "t-files", "last_progress_at": None}]
        self.peers = []
        self.sent = {}
        self.post_count = 0
        self.fail_send = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, code, data):
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.headers.get("X-Router-Token") != "router-test":
                    return self.reply(401, {"ok": False})
                parsed = urlsplit(self.path)
                if parsed.path == "/tasks":
                    return self.reply(200, {"ok": True, "tasks": owner.tasks})
                if parsed.path == "/workers":
                    return self.reply(200, {"ok": True, "workers": owner.workers})
                query = parse_qs(parsed.query)
                before = int(query.get("before", [2**63 - 1])[0])
                worker = query["worker"][0]
                rows = [row for row in owner.peers if row["seq"] < before and worker in (row["from"], row["to"])]
                rows.sort(key=lambda row: row["seq"], reverse=True)
                page = rows[:100]
                self.reply(200, {"ok": True, "messages": page, "has_more": len(rows) > 100,
                                 "next_before": page[-1]["seq"] if len(rows) > 100 else None})

            def do_POST(self):
                if self.headers.get("X-Trigger-Token") != "trigger-test":
                    return self.reply(401, {"ok": False})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.post_count += 1
                if owner.fail_send:
                    return self.reply(503, {"ok": False})
                duplicate = body["event_id"] in owner.sent
                owner.sent[body["event_id"]] = body
                self.reply(200, {"ok": True, "id": len(owner.sent), "duplicate": duplicate})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = watchdog.settings(self.root)
        base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.config.update(router_url=base, trigger_url=base + "/trigger", quiet_night=False)
        for key, text in [("router_token_file", "router-test"), ("trigger_token_file", "trigger-test")]:
            path = Path(self.config[key])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.logs = io.StringIO()
        handler = logging.StreamHandler(self.logs)
        watchdog.LOG.addHandler(handler)
        watchdog.LOG.setLevel(logging.INFO)
        self.addCleanup(watchdog.LOG.removeHandler, handler)
        with sqlite3.connect(self.config["trigger_db"]) as db:
            db.execute("CREATE TABLE events (created_at REAL, payload_json TEXT, text TEXT, source TEXT)")

    def run_watchdog(self, advance=0):
        return watchdog.run_once(self.config, self.now + advance)

    def status_file(self, name, ago=0):
        path = self.root / name
        path.write_text("working " + name, encoding="utf-8")
        os.utime(path, (self.now - ago, self.now - ago))
        return str(path)

    def peer(self, seq, text, task=None, reply=None):
        return {"seq": seq, "message_id": "message-%d" % seq, "from": "worker", "to": "Hermes",
                "text": text, "reply_to": reply, "created_at": self.now - 10, "task_id": task}

    def git(self, root, *args, at=None):
        env = dict(os.environ)
        if at is not None:
            date = "%d +0000" % at
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        return subprocess.check_output(["git", "-c", "commit.gpgsign=false", "-C", str(root), *args], env=env).strip()

    def workspace(self):
        root = self.root / "任务 workspace"
        root.mkdir()
        self.git(root, "init", "-q")
        self.git(root, "config", "user.name", "Watchdog Test")
        self.git(root, "config", "user.email", "watchdog@example.invalid")
        (root / "code.txt").write_text("baseline")
        self.git(root, "add", "code.txt")
        self.git(root, "commit", "-qm", "baseline", at=self.now - 3000)
        os.utime(root / "code.txt", (self.now - 3000, self.now - 3000))
        self.tasks[0]["workspace"] = str(root)
        return root

    def test_files_only_then_frozen_task_alarms_once(self):
        files = [self.status_file("status-%d.json" % index, index) for index in range(3)]
        self.config["workers"] = {"worker": {"state_files": files}}
        active = self.run_watchdog()[0]
        self.assertEqual(active["decision"], "active")
        self.assertEqual(active["signals"]["fs"], 3)
        self.assertEqual(active["signals"]["notice"], 0)
        self.assertEqual(self.post_count, 0)
        stalled = self.run_watchdog(901)[0]
        self.assertEqual(stalled["decision"], "alert")
        self.assertEqual(self.run_watchdog(1000)[0]["decision"], "dedup")
        self.assertEqual(self.post_count, 1)
        self.assertEqual(self.sent["watchdog-t-files"]["level"], "notify")
        print("\nREAL_SCENARIOS\n" + self.logs.getvalue(), end="")

    def test_dirty_workspace_without_commit_or_notice_is_activity(self):
        root = self.workspace()
        head = self.git(root, "rev-parse", "HEAD")
        (root / "code.txt").write_text("actual edit")
        os.utime(root / "code.txt", (self.now - 3, self.now - 3))
        report = self.run_watchdog()[0]
        self.assertEqual(report["decision"], "active")
        self.assertGreater(report["signals"]["fs"], 0)
        self.assertEqual(report["signals"]["head"], 0)
        self.assertEqual(self.git(root, "rev-parse", "HEAD"), head)
        self.assertFalse(self.sent)

    def test_new_head_is_activity_and_old_head_does_not_keep_task_alive(self):
        root = self.workspace()
        self.workers[0]["last_progress_at"] = self.now - 100
        self.run_watchdog()
        self.workers[0]["last_progress_at"] = None
        self.git(root, "commit", "--allow-empty", "-qm", "real head change", at=self.now - 2)
        report = self.run_watchdog()[0]
        self.assertEqual(report["signals"]["head"], 1)
        self.assertEqual(report["decision"], "active")
        self.assertEqual(self.run_watchdog(901)[0]["decision"], "alert")

    def test_task_related_peers_and_linked_replies_count_but_other_tasks_do_not(self):
        self.peers = [self.peer(1, "t-files 申请审批"), self.peer(2, "t-files 进度"),
                      self.peer(3, "t-other unrelated"), self.peer(4, "批准", reply="message-1")]
        report = self.run_watchdog()[0]
        self.assertEqual(report["signals"]["peer"], 3)
        self.assertEqual(report["decision"], "active")
        self.peers = [self.peer(5, "t-other still unrelated")]
        self.assertEqual(self.run_watchdog(901)[0]["decision"], "alert")

    def test_peer_history_reads_the_second_page(self):
        self.peers = [self.peer(index, "unrelated") for index in range(1, 130)]
        self.peers[0]["text"] = "t-files 申请审批"
        report = self.run_watchdog()[0]
        self.assertEqual(report["signals"]["peer"], 1)
        self.assertFalse(self.sent)

    def test_worker_progress_and_notice_metadata_are_read(self):
        self.workers[0]["last_progress_at"] = self.now - 4
        with sqlite3.connect(self.config["trigger_db"]) as db:
            db.execute("INSERT INTO events VALUES (?,?,?,?)", (self.now - 5,
                json.dumps({"notice": True, "worker": "worker", "task_id": "t-files"}), "进度", "router"))
        report = self.run_watchdog()[0]
        self.assertEqual(report["signals"]["progress"], 1)
        self.assertEqual(report["signals"]["notice"], 1)
        self.assertEqual(report["silent"], 4)
        self.assertFalse(self.sent)

    def test_progress_bound_to_another_task_is_ignored(self):
        self.workers[0].update(task_id="t-other", last_progress_at=self.now)
        self.assertEqual(self.run_watchdog()[0]["decision"], "alert")

    def test_each_task_uses_its_own_creation_timestamp(self):
        self.tasks.append({"task_id": "t-missing-time", "worker": "worker", "in_running_tasks": True})
        reports = self.run_watchdog()
        self.assertEqual(reports[0]["decision"], "alert")
        self.assertEqual(reports[1]["decision"], "terminal")
        self.assertEqual(list(self.sent), ["watchdog-t-files"])

    def test_progress_at_fallback_when_last_progress_is_null(self):
        self.workers[0]["progress_at"] = self.now - 4
        report = self.run_watchdog()[0]
        self.assertEqual(report["decision"], "active")
        self.assertEqual(report["signals"]["progress"], 1)

    def test_late_evidence_confirms_false_alerts_and_increases_threshold(self):
        self.tasks.append({**self.tasks[0], "task_id": "t-second"})
        self.run_watchdog()
        self.assertEqual(len(self.sent), 2)
        self.peers = [self.peer(1, "t-files already working"), self.peer(2, "t-second already working")]
        self.run_watchdog(1)
        self.tasks.insert(0, {**self.tasks[0], "task_id": "t-third"})
        report = self.run_watchdog(2)[0]
        self.assertEqual(report["threshold"], 1800)
        self.assertEqual(report["decision"], "active")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("watchdog backoff threshold=1800s", self.logs.getvalue())
        self.assertEqual(self.run_watchdog(4000)[0]["threshold"], 900)

    def test_recovery_after_a_real_alert_is_not_classified_as_a_false_alarm(self):
        self.run_watchdog()
        self.peers = [self.peer(1, "t-files recovered")]
        self.peers[0]["created_at"] = self.now + 1
        self.run_watchdog(2)
        state = watchdog.load_state(Path(self.config["state_file"]))
        self.assertEqual(state["false_alarms"], [])

    def test_legacy_sent_and_terminal_replay_remain_deduplicated(self):
        path = Path(self.config["state_file"])
        path.parent.mkdir(parents=True)
        path.write_text('{"t-files":"sent"}')
        self.assertEqual(self.run_watchdog()[0]["decision"], "dedup")
        self.tasks = []
        self.run_watchdog(1)
        self.tasks = [{"task_id": "t-files", "worker": "worker", "in_running_tasks": True, "created_at": self.now - 2000}]
        self.assertEqual(self.run_watchdog(2)[0]["decision"], "dedup")
        self.assertFalse(self.sent)

    def test_delivery_failure_is_retryable_with_the_same_event_id(self):
        self.fail_send = True
        self.assertEqual(self.run_watchdog()[0]["decision"], "delivery_failed")
        self.fail_send = False
        self.assertEqual(self.run_watchdog(1)[0]["decision"], "alert")
        self.assertEqual(list(self.sent), ["watchdog-t-files"])
        self.assertEqual(self.run_watchdog(2)[0]["decision"], "dedup")

    def test_night_quiet_and_morning_boundary(self):
        self.config["quiet_night"] = True
        for hour, minute in [(0, 0), (8, 29)]:
            now = datetime(2026, 11, 16, hour, minute).timestamp()
            self.assertEqual(watchdog.run_once(self.config, now), [])
        now = datetime(2026, 11, 16, 8, 30).timestamp()
        self.tasks[0].update(created_at=now - 1000, started_at=now - 1000)
        self.assertEqual(watchdog.run_once(self.config, now)[0]["decision"], "alert")

    def test_threshold_configuration_and_backoff_cap(self):
        path = self.root / "task-watchdog.json"
        path.write_text('{"threshold_s":1200,"max_threshold_s":2400,"workers":{}}', encoding="utf-8-sig")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(watchdog.settings(self.root)["threshold_s"], 1200)
        with patch.dict(os.environ, {"WATCHDOG_STUCK_S": "1500"}):
            self.assertEqual(watchdog.settings(self.root)["threshold_s"], 1500)
        self.config["backoff_after"] = 1
        state = {"version": 2, "tasks": {}, "false_alarms": [self.now - 1] * 4}
        target = Path(self.config["state_file"])
        target.parent.mkdir(parents=True)
        watchdog.save_state(target, state)
        self.assertEqual(self.run_watchdog()[0]["threshold"], 3600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
