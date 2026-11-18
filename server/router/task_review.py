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

    def save(self, task_id, flow, *, state=None):
        deadlines = [t for t in (flow.get("delivery_due"), flow.get("review_due"),
                                 *flow["pending_notices"].values()) if t is not None]
        result = None
        if state in ("done", "failed"):
            result = {**flow["deliverable"], "status": state, "verdict": flow["verdict"]}
            approval = self.store.approval_progress(self.store.get_task(task_id))
            if approval:
                result.update(approval_id=approval["approval_id"], approval_status=approval["state"])
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE tasks SET workflow_json=?, workflow_due=?, state=COALESCE(?,state),"
                " finished_at=CASE WHEN ? IN ('done','failed') THEN ? ELSE finished_at END,"
                " result_json=COALESCE(?,result_json) WHERE task_id=?",
                (json.dumps(flow, ensure_ascii=False), min(deadlines, default=None), state, state, time.time(),
                 json.dumps(result, ensure_ascii=False) if result else None, task_id))

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
            if flow.get("probe"):
                now = self.server.watchdog.clock()
            due = flow.get("delivery_due")
            if flow.get("probe") and due is not None and due <= now:
                self.server.watchdog.advance(task, flow, now)
            elif due is not None and due <= now:
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
        if kind == "deliverable_missing" and task["deliverable_state"] != "deliverable_missing":
            return  # A late valid answer won the race with the notification outbox.
        data = {k: task[k] for k in ("task_id", "worker", "card_id", "board_id")}
        if kind == "deliverable":
            report = task["deliverable"]
            summary = report.get("summary_short", report["summary"])
            data.update(summary=summary, artifacts=report["artifacts"])
            text = summary + "\n产物：" + ("、".join(report["artifacts"]) or "无")
        elif kind in ("done", "failed"):
            text = task["verdict"]["reason"]
        else:
            text = {"summary_oversized": "摘要重写三次仍超长，请人工处理。",
                    "deliverable_missing": "成果询问仍无回执，请核对。",
                    "review_overdue": "任务仍待验收，请溪流核对。"}[kind]
        payload = {"source": "任务成果", "kind": "task." + kind, "level": "notify", "actor": task["worker"],
                   "event_id": task_id + ":" + kind, "text": task_id + " " + text, "payload": data}
        if kind == "deliverable_missing" and task.get("probe_id"):
            payload["level"] = "event"
        elif kind in ("done", "failed"):
            payload = self.server.verdict_notice(task, kind)
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

    def receive(self, task_id, body):
        flow = self.load(task_id)
        if "worker" in body or (flow and flow.get("probe")):
            return self.server.watchdog.answer(task_id, body)
        with self.server.dispatch_lock:
            flow = self.load(task_id)
            task = self.store.get_task(task_id)
            if not task:
                return 404, {"error": "unknown_task"}
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
                flow.update(deliverable=report, delivery_state="received", delivery_due=None)
                flow["pending_notices"]["deliverable"] = now
                response = 200, {"ok": True, "task_id": task_id, "deliverable_state": "received"}
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
            if flow.get("probe") and self.store.get_task(task_id)["state"] != "reviewing" and not flow.get("verdict"):
                return 409, {"error": "task_not_reviewing"}
            if flow.get("verdict"):
                if flow["verdict"] == verdict:
                    return 200, {"ok": True, "duplicate": True, "verdict": verdict}
                return 409, {"error": "verdict_conflict"}
            flow.update(review_state=status, review_due=None, verdict=verdict)
            flow["pending_notices"].pop("review_overdue", None)
            if flow.get("probe"):
                flow["pending_notices"][status] = self.server.watchdog.clock()
            self.save(task_id, flow, state=status if flow.get("probe") else None)
        self.tick(task_id)
        return 200, {"ok": True, "state": self.store.get_task(task_id)["state"], "review_state": status, "verdict": verdict}
