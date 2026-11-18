"""Watch live worker activity and solicit one evidence report per work order."""
import json
import time
import uuid

INTERVAL = 60
IDLE_SECONDS = 120
RESPONSE_SECONDS = 300
SAMPLE_TTL = 90
PROMPT = "当前 task_id = {task_id}。请在当前回合的最终答复中只写 JSON，插件将代交成果：summary（≤200 字）、artifacts（分支/SHA/文件绝对路径）、evidence（跑过的命令与关键输出）、risks（未完成项与风险）。后三项用字符串数组，无则填 []。私信仅用于额外说明。"


class IdleProbe:
    def __init__(self, server):
        self.server = server
        self.store = server.store
        self.clock = time.time
        self.idle_seconds = IDLE_SECONDS
        self.retry_seconds = RESPONSE_SECONDS

    def observe(self, conn, state):
        """Only the current connection's activity, never the derived task-slot busy bit."""
        now = self.clock()
        changed = (state != conn.watch_state or conn.watch_seen is None or
                   now - conn.watch_seen > SAMPLE_TTL)
        conn.watch_state, conn.watch_seen = state, now
        if changed:
            self.check(conn.name, reset=True)
        else:
            # Heartbeats may reach the threshold between periodic checks.
            self.check(conn.name, due_only=True)

    def check(self, name, *, reset=False, due_only=False):
        with self.server.dispatch_lock:
            worker = self.store.get_worker(name)
            if not worker:
                return
            old = worker["watchdog"] or {}
            now = self.clock()
            conn = self.server.current_conn(name)
            online = bool(conn and not conn.closed)
            fresh = online and conn.watch_seen is not None and now - conn.watch_seen <= SAMPLE_TTL
            state = conn.watch_state if fresh else "unknown"
            task = self.store.blocking_task(name)
            task_id = task["task_id"] if task else None
            eligible = bool(task and task["state"] == "running" and state == "idle")
            same = not reset and old.get("task_id") == task_id and old.get("idle_since") is not None
            since = (old["idle_since"] if same else now) if eligible else None
            flow = self.server.reviews.load(task_id) if task_id else None
            start = eligible and not flow and now - since >= self.idle_seconds
            retry = flow and flow.get("delivery_state") == "probe_pending" and now >= flow["delivery_due"]
            if due_only and not start and not retry:
                return
            observation = {"connected": online, "state": state, "task_id": task_id, "idle_since": since,
                           "checked_at": now, "checks": old.get("checks", 0) + 1}
            if start:
                flow = {"probe": True, "request_id": str(uuid.uuid4()), "delivery_state": "probe_pending",
                        "attempts": 0, "delivery_due": now, "raw": [], "pending_notices": {}}
                self.server.reviews.save(task_id, flow)
            if start or retry:
                self.server.reviews.tick(task_id)
                flow = self.server.reviews.load(task_id)
            observation["probe_attempts"] = (flow or {}).get("attempts", 0)
            with self.store._lock:
                self.store._conn.execute("UPDATE workers SET watchdog_json=? WHERE name=?",
                                         (json.dumps(observation), name))

    def sweep(self):
        for worker in self.store.list_workers():
            self.check(worker["name"])

    def advance(self, task, flow, now):
        """One logical request and one deadline, measured from its first issue."""
        conn = self.server.current_conn(task["worker"])
        idle = (conn and not conn.closed and conn.watch_state == "idle" and conn.watch_seen is not None
                and now - conn.watch_seen <= SAMPLE_TTL)
        if not flow["attempts"] and task["state"] in ("paused", "cancelling"):
            # A paused task may resume with the same outstanding request.
            flow["delivery_due"] = now + INTERVAL
            return
        if task["state"] not in ("running", "queued", "paused", "cancelling"):
            flow.update(delivery_due=None, delivery_state="probe_closed")
            return
        if flow["attempts"]:
            # Older persisted requests carried two response windows.
            flow["expires_at"] = min(flow["expires_at"], flow["issued_at"] + self.retry_seconds)
        if flow["attempts"] and now >= flow["expires_at"]:
            flow.update(delivery_due=None, delivery_state="deliverable_missing")
            flow["pending_notices"]["deliverable_missing"] = now
        elif idle and task["state"] == "running" and not flow["attempts"]:
            attempt = flow["attempts"] + 1
            flow.setdefault("issued_at", now)
            flow["expires_at"] = now + self.retry_seconds
            flow.update(attempts=attempt, delivery_due=now + self.retry_seconds)
            self.server.reviews.save(task["task_id"], flow)
            self.server.cancellations_changed.set()
            conn.queue.put({"event": "deliverable_request", "data": {
                "request_id": flow["request_id"], "task_id": task["task_id"],
                "template": PROMPT.format(task_id=task["task_id"]), "issued_at": flow["issued_at"]}})
        else:
            # A busy or disconnected recipient cannot be asked again. Keep the alarm deadline.
            flow["delivery_due"] = min(flow.get("expires_at", now + INTERVAL), now + INTERVAL)

    def answer(self, task_id, body):
        with self.server.dispatch_lock:
            task = self.store.get_task(task_id)
            if not task:
                return 404, {"error": "unknown_task"}
            if task["worker"] != body.get("worker"):
                return 400, {"error": "worker_mismatch"}
            flow = self.server.reviews.load(task_id)
            if not flow or not flow.get("probe") or not flow["attempts"] or body.get("request_id") != flow["request_id"]:
                return 409, {"error": "delivery_request_mismatch", "task_id": task_id}
            if flow.get("deliverable") is not None:
                return 200, {"ok": True, "duplicate": True, "task_id": task_id, "state": task["state"]}
            if task["state"] != "running" or flow["delivery_state"] not in ("probe_pending", "deliverable_missing"):
                return 409, {"error": "delivery_request_closed", "state": task["state"]}

            report = {k: body.get(k) for k in ("summary", "artifacts", "evidence", "risks")}
            if "summary_short" in body:
                report["summary_short"] = body["summary_short"]
            for field in ("summary", "summary_short"):
                if field not in report:
                    continue
                value = report[field]
                if not isinstance(value, str) or not value.strip():
                    return 400, {"error": "invalid_deliverable", "field": field}
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError:
                    return 400, {"error": "invalid_deliverable_unicode", "field": field}
                if len(value) > 200:
                    if not flow["raw"] or flow["raw"][-1]["summary"] != value:
                        flow["raw"].append({"summary": value})
                        self.server.reviews.save(task_id, flow)
                    return 400, {"error": "deliverable_summary_too_long", "field": field,
                                 "detail": f"当前 {len(value)} 字，上限 200，请重新组织"}
            for field in ("artifacts", "evidence", "risks"):
                if not isinstance(report[field], list) or not all(isinstance(v, str) and v.strip() for v in report[field]):
                    return 400, {"error": "invalid_deliverable", "field": field}
            try:
                json.dumps(report, ensure_ascii=False).encode("utf-8")
            except UnicodeEncodeError:
                return 400, {"error": "invalid_deliverable_unicode"}
            now = self.clock()
            flow.update(deliverable=report, delivery_state="received", delivery_due=None,
                        review_state="reviewing", review_due=now + 1800)
            flow["pending_notices"].pop("deliverable_missing", None)
            flow["pending_notices"]["deliverable"] = now
            # One durable UPDATE covers both the answer and the handoff to human review.
            self.server.reviews.save(task_id, flow, state="reviewing")
            with self.store._lock:
                self.store._conn.execute("UPDATE workers SET task_id=NULL WHERE name=? AND task_id=?",
                                         (task["worker"], task_id))
            self.store.refresh_counts(task["worker"])
            self.check(task["worker"])
            dispatched = self.server.dispatch_one(task["worker"])
        self.server.reviews.tick(task_id)
        return 200, {"ok": True, "state":"reviewing", "task_id":task_id, "duplicate":False,
                     "dispatched_next":dispatched["task_id"] if dispatched else None}
