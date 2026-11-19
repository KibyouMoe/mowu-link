"""Solicit one structured summary and coordinate cooperative cancellation."""

import hashlib
import json
import logging
import queue
import threading
import time
import uuid
from task_lifecycle import settings

CANCEL_PROMPT = "这单要取消，请确认后自己发 cancel，并清掉本地 TaskID。"
CORRECTION_LIMIT = 3
CLEARED_PROMPT = "这单已取消，请清掉本地 TaskID。"
SUMMARY_PROMPT = "请调用 submit_deliverable 工具报送本任务的总结。"


class SummaryRequests:
    def __init__(self, server, config_path):
        self.server = server
        self.orders = server.store.orders
        self.interval, self.max_attempts = settings(config_path)
        self.clock = time.monotonic
        self.samples = {}
        self.flush_lock = threading.Lock()

    def notice(self, task, kind, text, **extra):
        tid = task["task_id"]
        self.orders.add(
            tid,
            "outbox",
            "notify:" + kind,
            notice_kind=kind,
            payload={
                "kind": "task." + kind,
                "source": "Router",
                "actor": task["worker"],
                "text": text,
                "level": "event",
                "event_id": tid + ":" + kind,
                "payload": {"task_id": tid, "task_info": task["task_info"], **extra},
            },
        )

    def flush(self):
        if self.flush_lock.acquire(blocking=False):
            try:
                self._flush_pending()
            finally:
                self.flush_lock.release()

    def _flush_pending(self):
        with self.orders.store._lock:
            rows = self.orders.db.execute(
                "SELECT task_id,data,event_key FROM task_facts n WHERE kind='outbox' "
                "AND NOT EXISTS(SELECT 1 FROM task_facts a WHERE a.task_id=n.task_id AND a.event_key='sent:'||n.event_key) "
                "ORDER BY seq LIMIT 64"
            ).fetchall()
        for row in rows:
            entry = json.loads(row["data"])
            ok, _ = self.server.notifier.post_task_notice(
                row["task_id"], entry["notice_kind"], entry["payload"]
            )
            if ok:
                self.orders.add(
                    row["task_id"],
                    "notice_sent",
                    "sent:" + row["event_key"],
                    notice_kind=entry["notice_kind"],
                )

        db = self.orders.db
        with self.orders.store._lock:
            alerts = db.execute(
                "SELECT id,worker,task_id,attempts FROM worker_task_corrections "
                "WHERE attempts=? AND notified_at IS NULL ORDER BY id LIMIT 64", (CORRECTION_LIMIT,)
            ).fetchall()
        for row in alerts:
            event = {
                "kind": "worker.task_id_correction_limit",
                "source": "Router",
                "level": "event",
                "actor": row["worker"],
                "text": f"{row['worker']} 连续 {row['attempts']} 次上报失效 TaskID {row['task_id']}，已放行派单。",
                "event_id": "worker:task-id-correction:" + str(row["id"]),
                "payload": {"worker": row["worker"], "TaskID": row["task_id"], "attempts": row["attempts"]},
            }
            ok, _ = self.server.trigger_client.post_trigger(event)
            if ok:
                with self.orders.store._lock:
                    db.execute(
                        "UPDATE worker_task_corrections SET notified_at=? WHERE id=?", (time.time(), row["id"])
                    )

    def open_correction(self, worker):
        with self.orders.store._lock:
            return self.orders.db.execute(
                "SELECT id,task_id,attempts FROM worker_task_corrections "
                "WHERE worker=? AND cleared_at IS NULL", (worker,),
            ).fetchone()

    def contact(self, task, conn, purpose, attempt):
        tid = task["task_id"]
        request_id = str(uuid.uuid4())
        # Commit the request identity before the worker can answer it.
        self.orders.add(
            tid, purpose + "_request", f"{purpose}:{attempt}", request_id=request_id, attempt=attempt
        )
        self.send_request(conn, tid, purpose, request_id, attempt, time.time())
        if purpose == "cancel" and attempt > 1:
            logging.getLogger("router").info(
                "cancel_retry worker=%s task=%s request=%s attempt=%s",
                conn.name, tid, request_id, attempt,
            )
        sample = self.samples.get(conn.name)
        if sample and sample[0] == tid:
            self.samples[conn.name] = (tid, self.clock(), sample[2])

    def send_request(self, conn, tid, purpose, request_id, attempt, issued_at):
        if purpose == "cancel" and conn.offered_cancel == request_id:
            return
        payload = {
            "task_id": tid, "request_id": request_id, "attempt": attempt, "purpose": purpose,
            "issued_at": issued_at, "template": CANCEL_PROMPT if purpose == "cancel" else SUMMARY_PROMPT,
        }
        try:
            conn.queue.put_nowait({"event": "deliverable_request", "data": payload})
            if purpose == "cancel":
                conn.offered_cancel = request_id
            logging.getLogger("router").info(
                "probe_queued task=%s request=%s attempt=%s via=sse purpose=%s",
                tid, request_id, attempt, purpose,
            )
        except queue.Full:
            logging.getLogger("router").warning(
                "probe_error task=%s request=%s attempt=%s via=sse reason=queue_full",
                tid, request_id, attempt,
            )
            self.orders.add(tid, "stream_full", "full:" + request_id, request_id=request_id)

    def detached(self, name):
        self.samples.pop(name, None)
        tid = self.orders.held(name)
        task = self.orders.get(tid) if tid else None
        if task and task.get("state") == "cancelling":
            self.offline_archive(task, "worker_disconnected")

    def wait_for_release(self, worker):
        conn = self.server.current_conn(worker)
        if conn:
            conn.awaiting_clear = True

    def offline_archive(self, task, why):
        with self.orders.atomic():
            self.orders.archive(task["task_id"], why, confirmed_by_worker=False)
            self.wait_for_release(task["worker"])
            self.notice(
                task,
                "offline_archive",
                f"{task['task_id']} 按失联归档，未收到执行人取消命令。",
                reason=why,
            )

    def cancel(self, tid, body):
        with self.server.dispatch_lock, self.orders.atomic():
            task = self.orders.get(tid)
            if not task:
                return 404, {"error": "unknown_task"}
            if task.get("legacy"):
                return 409, {"error": "historical_record"}
            worker = task["worker"]
            pending = task.get("state") == "cancelling"
            if "request_id" in body or "name" in body:
                return 409, {"error": "use_deliverable_tool_to_confirm"}
            if not task["in_running_tasks"] and not task.get("archive"):
                self.orders.archive(tid, "withdrawn_before_dispatch", actor="hermes")
                return 200, {"ok": True, "task_id": tid, "archived": True, "duplicate": False}
            if not task["in_running_tasks"]:
                return 200, {"ok": True, "task_id": tid, "archived": True, "duplicate": True}
            conn = self.server.current_conn(worker)
            if not conn or conn.closed:
                self.offline_archive(task, "worker_offline")
                return 200, {"ok": True, "task_id": tid, "archived": True, "confirmed_by_worker": False}
            if not pending:
                self.contact(task, conn, "cancel", 1)
            return 202, {
                "ok": True,
                "task_id": tid,
                "notified_worker": True,
                "duplicate": pending,
                "state": "cancelling",
            }

    def reconcile(self, name, reported, source):
        """Only live requests can carry a correction back to the worker."""
        conn = self.server.current_conn(name)
        with self.orders.atomic():
            current = self.open_correction(name)
            held = self.orders.held(name)
            stale = reported and reported != held
            if current and (not stale or current["task_id"] != reported):
                self.orders.db.execute(
                    "UPDATE worker_task_corrections SET cleared_at=? WHERE id=?",
                    (time.time(), current["id"]),
                )
                # A speculative offer at the limit may have arrived before the
                # worker cleared its old ID. Replay only that offer, once.
                if not reported and conn and conn.offered_task == held and current["attempts"] == CORRECTION_LIMIT:
                    conn.offered_task = None
                current = None

            if not stale:
                if conn:
                    conn.awaiting_clear = False
                if reported and source == "register":
                    task = self.orders.get(reported)
                    if task["state"] == "cancelling":
                        return reported, {"reason": "等待取消确认", "cancel_request_id": task["request_id"]}
                    return reported, {"reason": "这单还在", "resume_task_id": reported,
                                      "resume_prompt": f"这单还在，请继续处理原工单 {reported}。"}
                return reported, {}

            previous = current["attempts"] if current else 0
            if previous == 0:
                self.orders.db.execute(
                    "INSERT INTO worker_task_corrections(worker,task_id,attempts) VALUES(?,?,1)",
                    (name, reported),
                )
            elif previous < CORRECTION_LIMIT:
                self.orders.db.execute(
                    "UPDATE worker_task_corrections SET attempts=attempts+1 WHERE id=?", (current["id"],),
                )
            attempt = min(previous + 1, CORRECTION_LIMIT)
            if conn:
                conn.awaiting_clear = attempt < CORRECTION_LIMIT
            if previous == CORRECTION_LIMIT and source != "register":
                return None, {}

            task = self.orders.get(reported)
            complete = task and task["worker"] == name and task.get("deliverable")
            reason = "总结已归档" if complete else "已取消"
            logger = logging.getLogger("router")
            emit = logger.warning if previous == CORRECTION_LIMIT - 1 else logger.info
            emit("task_id_correction worker=%s task=%s source=%s attempt=%d dispatch=%s reason=%s",
                 name, reported, source, attempt, "allowed" if attempt == CORRECTION_LIMIT else "held", reason)
            return None, {
                "clear_task_id": reported,
                "cancel_prompt": None if complete else CLEARED_PROMPT,
                "reason": reason,
            }

    def observe(self, name, reported, busy):
        with self.server.dispatch_lock:
            now = self.clock()
            conn = self.server.current_conn(name)
            task = self.orders.get(reported) if reported else None
            if (
                not conn
                or conn.closed
                or busy
                or not task
                or task["worker"] != name
                or not task["in_running_tasks"]
            ):
                self.samples.pop(name, None)
                return
            sample = self.samples.get(name)
            if not sample or sample[0] != reported or now - sample[2] > 15:
                self.samples[name] = (reported, now, now)
                return
            self.samples[name] = (reported, sample[1], now)
            self.advance(name)

    def advance(self, name):
        sample = self.samples.get(name)
        if not sample:
            return
        tid, since, seen = sample
        now = self.clock()
        if now - seen > 15 or now - since < self.interval:
            return
        conn = self.server.current_conn(name)
        task = self.orders.get(tid)
        if not task or not task["in_running_tasks"] or not conn or conn.closed:
            return
        purpose = "cancel" if task["state"] == "cancelling" else "summary"
        requests = [f for f in task["facts"] if f["kind"] == purpose + "_request"]
        if purpose == "cancel":
            last = requests[-1]
            logging.getLogger("router").warning(
                "cancel_timeout worker=%s task=%s request=%s attempt=%s",
                name, tid, last["request_id"], last["attempt"],
            )
        if len(requests) < self.max_attempts:
            self.contact(task, conn, purpose, len(requests) + 1)
        elif purpose == "summary":
            if not any(f.get("notice_kind") == "summary_missing" for f in task["facts"]):
                self.notice(task, "summary_missing", f"{tid} 索取总结 {len(requests)} 次仍未收到，请核查。")
        else:
            self.offline_archive(task, "archive_unconfirmed")
            self.samples.pop(name, None)

    def sweep(self):
        with self.server.dispatch_lock:
            for name in list(self.samples):
                self.advance(name)
        self.flush()

    def receive(self, tid, body):
        with self.server.dispatch_lock, self.orders.atomic():
            task = self.orders.get(tid)
            if not task:
                return 404, {"error": "unknown_task"}
            rid = body.get("request_id")
            cancel_request = any(
                f["kind"] == "cancel_request" and f["request_id"] == rid for f in task["facts"]
            )
            if body.get("worker") != task["worker"]:
                return 409, {"error": "delivery_request_mismatch"}
            if "action" in body and (not cancel_request or body["action"] != "cancel"):
                return 409, {"error": "delivery_request_mismatch"}
            confirm_cancel = cancel_request and body.get("action") == "cancel"
            if not confirm_cancel and not any(
                f["kind"] == "summary_request" and f["request_id"] == rid for f in task["facts"]
            ):
                return 409, {"error": "delivery_request_mismatch"}
            if task.get("deliverable"):
                accepted = next(f for f in task["facts"] if f["kind"] == "summary")
                if accepted["request_id"] == rid and accepted["report"] == {
                    k: body[k]
                    for k in ("summary", "summary_short", "artifacts", "evidence", "risks")
                    if k in body
                }:
                    return 200, {
                        "ok": True,
                        "duplicate": True,
                        "task_id": tid,
                        "archived": True,
                        "worker_task_id": 0,
                    }
            if confirm_cancel and task.get("archive", {}):
                if task["archive"].get("request_id") == rid:
                    return 200, {
                        "ok": True,
                        "task_id": tid,
                        "duplicate": True,
                        "archived": True,
                        "worker_task_id": 0,
                    }
            if not task["in_running_tasks"] or (not confirm_cancel and task["state"] == "cancelling"):
                self.orders.add(
                    tid, "late_receipt", "late_receipt", request_id=rid, detail="迟到回执已忽略"
                )
                return 409, {"error": "delivery_request_closed", "detail": "迟到回执已忽略"}
            report = {k: body.get(k) for k in ("summary", "artifacts", "evidence", "risks")}
            if "summary_short" in body:
                report["summary_short"] = body["summary_short"]
            for field in ("summary", "summary_short"):
                if field not in report:
                    continue
                s = report[field]
                if not isinstance(s, str) or not s.strip():
                    return 400, {"error": "invalid_deliverable", "field": field}
                if len(s) > 200:
                    key = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
                    self.orders.add(tid, "raw_report", "raw:" + key, raw=report)
                    return 400, {
                        "error": "deliverable_summary_too_long",
                        "field": field,
                        "detail": f"当前 {len(s)} 字，上限 200，请重新组织",
                    }
            if any(
                not isinstance(report[k], list)
                or not all(isinstance(x, str) and x.strip() for x in report[k])
                for k in ("artifacts", "evidence", "risks")
            ):
                return 400, {"error": "invalid_deliverable"}
            try:
                json.dumps(report, ensure_ascii=False).encode("utf-8")
            except UnicodeEncodeError:
                return 400, {"error": "invalid_deliverable", "detail": "invalid Unicode"}
            if confirm_cancel:
                self.orders.add(
                    tid,
                    "worker_cancel_reply",
                    "worker_cancel_reply",
                    actor=task["worker"],
                    request_id=rid,
                    report=report,
                )
                self.orders.archive(
                    tid, "worker_cancel", actor=task["worker"], request_id=rid, confirmed_by_worker=True
                )
                self.wait_for_release(task["worker"])
                self.samples.pop(task["worker"], None)
                return 200, {"ok": True, "task_id": tid, "archived": True, "worker_task_id": 0}
            self.orders.add(tid, "summary", "summary", request_id=rid, report=report)
            self.orders.archive(tid, "summary_received")
            self.wait_for_release(task["worker"])
            summary = report.get("summary_short", report["summary"])
            self.notice(
                task,
                "deliverable",
                f"成果报送 · {task['worker']} · {tid}\n{summary}\n产物 {len(report['artifacts'])} 项",
                deliverable=report,
                context=task.get("context"),
            )
            self.samples.pop(task["worker"], None)
        self.flush()
        return 200, {
            "ok": True,
            "duplicate": False,
            "task_id": tid,
            "archived": True,
            "worker_task_id": 0,
        }
