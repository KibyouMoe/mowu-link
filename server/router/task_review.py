"""Post-execution reporting. SQLite keeps delivery, review and retry state across restarts."""
import json
import time
import uuid

PROMPT = """请仅用一个 JSON 对象报送本工单成果。
① summary：做了什么，最多 200 字（含标点）；超长请重新组织。
② artifacts：分支/SHA、文件绝对路径、图片路径。
③ evidence：实际命令、关键输出、回读结果。
④ risks：未完成项、风险、需要训练员决定的事。
后三项使用字符串数组，无则填 []（无）。额外说明可另发带 task_id 的私信。"""
RETRY_SECONDS = (30, 60, 120, 240)  # initial request and three retries
REVIEW_REMINDER_S = 30 * 60


def initial_review(now):
    return {"review_state": "reviewing", "review_due": now + REVIEW_REMINDER_S,
            "delivery_state": "pending", "request_id": str(uuid.uuid4()), "attempts": 0,
            "delivery_due": now, "raw": [], "pending_notices": {}}


class TaskReview:
    def __init__(self, server):
        self.server = server
        self.store = server.store

    def load(self, task_id):
        with self.store._lock:
            row = self.store._conn.execute("SELECT workflow_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def save(self, task_id, flow):
        deadlines = [t for t in (flow.get("delivery_due"), flow.get("review_due"),
                                 *flow["pending_notices"].values()) if t is not None]
        with self.store._lock:
            self.store._conn.execute("UPDATE tasks SET workflow_json=?, workflow_due=? WHERE task_id=?",
                                     (json.dumps(flow, ensure_ascii=False), min(deadlines, default=None), task_id))

    def with_raw(self, task_id, deliverable):
        flow = self.load(task_id)
        if flow and flow["raw"]:
            return {**(deliverable or {}), "raw": [item["summary"] for item in flow["raw"]]}
        return deliverable

    def tick(self, task_id):
        with self.server.dispatch_lock:
            now = time.time()
            flow = self.load(task_id)
            if not flow:
                return
            task = self.store.get_task(task_id)
            due = flow.get("delivery_due")
            if due is not None and due <= now:
                attempt = flow["attempts"]
                if attempt < len(RETRY_SECONDS):
                    flow.update(attempts=attempt + 1, delivery_due=now + RETRY_SECONDS[attempt])
                    self.save(task_id, flow)
                    conn = self.server.current_conn(task["worker"])
                    if conn and not conn.closed:
                        conn.queue.put({"event": "deliverable_request", "data": {
                            "task_id": task_id, "request_id": flow["request_id"], "attempt": attempt + 1,
                            "thread_id": (task["turn_binding"] or {}).get("thread_id"),
                            "prompt": PROMPT, "rewrite": len(flow["raw"])}})
                else:
                    flow.update(delivery_due=None, delivery_state="deliverable_missing")
                    flow["pending_notices"]["deliverable_missing"] = now
            if flow.get("review_due") is not None and flow["review_due"] <= now:
                flow["review_due"] = None
                flow["pending_notices"]["review_overdue"] = now
            notices = [kind for kind, due in flow["pending_notices"].items() if due <= now]
            for kind in notices:
                flow["pending_notices"][kind] = now + 30
            self.save(task_id, flow)
        for kind in notices:
            self.notify(task_id, kind)

    def notify(self, task_id, kind):
        task = self.store.get_task(task_id)
        data = {k: task[k] for k in ("task_id", "worker", "card_id", "board_id")}
        if kind == "deliverable":
            report = task["deliverable"]
            data.update(summary=report["summary"], artifacts=report["artifacts"])
            text = report["summary"] + "\n产物：" + ("、".join(report["artifacts"]) or "无")
        else:
            text = {"summary_oversized": "摘要重写三次仍超长，请人工处理。",
                    "deliverable_missing": "成果询问重试三次仍无回执，请核对。",
                    "review_overdue": "任务仍待验收，请溪流核对。"}[kind]
        payload = {"source": "任务成果", "kind": "task." + kind, "level": "notify", "actor": task["worker"],
                   "event_id": task_id + ":" + kind, "text": task_id + " " + text, "payload": data}
        ok, _ = self.server.notifier.post_task_notice(task_id, kind, payload)
        if ok:
            with self.server.dispatch_lock:
                flow = self.load(task_id)
                flow["pending_notices"].pop(kind, None)
                self.save(task_id, flow)

    def sweep(self):
        with self.store._lock:
            ids = self.store._conn.execute("SELECT task_id FROM tasks WHERE workflow_due<=? ORDER BY workflow_due LIMIT 100",
                                           (time.time(),)).fetchall()
        for row in ids:
            self.tick(row[0])

    def release_idle(self, worker):
        """Publish each staged report once on the next live idle pulse, under dispatch_lock."""
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT task_id, workflow_json FROM tasks WHERE worker=? AND "
                "json_extract(workflow_json, '$.delivery_state')='awaiting_idle'", (worker,)).fetchall()
            for task_id, encoded in rows:
                flow = json.loads(encoded)
                flow.update(delivery_state="received", deliverable=flow.pop("staged_report"))
                flow["pending_notices"]["deliverable"] = time.time()
                self.save(task_id, flow)
        return [row[0] for row in rows]

    def receive(self, task_id, body):
        with self.server.dispatch_lock:
            flow = self.load(task_id)
            task = self.store.get_task(task_id)
            if not flow or body.get("name") != task["worker"]:
                return 409, {"error": "delivery_not_requested"}
            req = body.get("request_id")
            for rejected in flow["raw"]:
                if rejected["request_id"] == req:
                    return 400, rejected["error"]
            if req != flow["request_id"] or flow["attempts"] == 0:
                return 409, {"error": "delivery_request_mismatch"}
            report = body.get("deliverable")
            if not isinstance(report, dict) or set(report) != {"summary", "artifacts", "evidence", "risks"}:
                return 400, {"error": "invalid_deliverable"}
            summary = report["summary"]
            if not isinstance(summary, str) or not summary.strip() or any(
                    not isinstance(report[k], list) or not all(isinstance(v, str) and v.strip() for v in report[k])
                    for k in ("artifacts", "evidence", "risks")):
                return 400, {"error": "invalid_deliverable"}
            try:
                json.dumps(report, ensure_ascii=False).encode("utf-8")
            except UnicodeEncodeError:
                return 400, {"error": "invalid_deliverable_unicode"}
            if flow["delivery_state"] == "received":
                return ((200, {"ok": True, "duplicate": True}) if report == flow["deliverable"]
                        else (409, {"error": "deliverable_conflict"}))
            if flow["delivery_state"] == "awaiting_idle":
                if report != flow["staged_report"]:
                    return 409, {"error": "deliverable_conflict"}
                return 202, {"ok": True, "duplicate": True, "deliverable_state": "awaiting_idle"}
            now = time.time()
            if len(summary) > 200:
                error = {"error": "deliverable_summary_too_long",
                         "detail": "当前 %d 字，上限 200，请重新组织" % len(summary)}
                flow["raw"].append({"request_id": req, "summary": summary, "error": error})
                if len(flow["raw"]) >= 4:
                    flow.update(delivery_state="summary_oversized", delivery_due=None)
                    flow["pending_notices"]["summary_oversized"] = now
                else:
                    flow.update(request_id=str(uuid.uuid4()), attempts=0, delivery_state="pending", delivery_due=now)
                response = 400, error
            else:
                # A cached idle sample may predate this inquiry. Persist the answer
                # before acknowledging it; only a subsequent idle pulse can publish it.
                flow.update(staged_report=report, delivery_state="awaiting_idle", delivery_due=None)
                flow["pending_notices"].pop("deliverable_missing", None)
                response = 202, {"ok": True, "task_id": task_id, "deliverable_state": "awaiting_idle"}
            self.save(task_id, flow)
        self.tick(task_id)
        return response

    def verdict(self, task_id, body):
        status, reason, evidence = (body.get(k) for k in ("status", "reason", "evidence"))
        if (status not in ("done", "failed") or not isinstance(reason, str) or not reason.strip()
                or not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) and e.strip() for e in evidence)):
            return 400, {"error": "verdict_requires_status_reason_evidence"}
        verdict = {"status": status, "reason": reason, "evidence": evidence}
        with self.server.dispatch_lock:
            flow = self.load(task_id)
            if not flow:
                return 409, {"error": "task_not_reviewing"}
            if flow.get("verdict"):
                if flow["verdict"] == verdict:
                    return 200, {"ok": True, "duplicate": True, "verdict": verdict}
                return 409, {"error": "verdict_conflict"}
            flow.update(review_state=status, review_due=None, verdict=verdict)
            flow["pending_notices"].pop("review_overdue", None)
            self.save(task_id, flow)
            return 200, {"ok": True, "review_state": status, "verdict": verdict}
