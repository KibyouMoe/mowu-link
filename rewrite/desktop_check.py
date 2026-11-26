"""Paired with the Codex Desktop tool-host driver in desktop_check.js."""
import json
import sys
import time

from .codex_desktop import CodexDesktop
from .devloop import Loop
from .storage import encode, guid


class ToolPort:
    def __init__(self):
        self.sends = 0

    def call(self, op, **args):
        print(encode({"op": op, **args}), flush=True)
        line = sys.stdin.readline()
        if not line:
            raise EOFError("Desktop driver disconnected")
        reply = json.loads(line)
        if "error" in reply:
            raise OSError(reply["error"])
        return reply["result"]

    def read(self):
        return self.call("read")

    def send(self, prompt):
        self.sends += 1
        self.call("send", prompt=prompt)


def run(port, session, bootstrap):
    native = CodexDesktop(port, session, bootstrap)
    # New chat only; safe re-runs must create a new test chat.
    initial = native.snapshot()
    if initial is None or len(initial["turns"]) != 1 or native.busy(session):
        raise RuntimeError("expected a fresh idle isolated chat")
    clock = [time.time()]
    with Loop.isolated(lambda: clock[0], lambda _path, _session: native) as loop:
        task = loop.hermes.create(guid(), "验收独立 Desktop 输入",
                                  "请只回复「独立宿主已收到工单」。本次只验收输入，不操作工具或其他会话。", "oguri")
        tid = task["task_id"]
        loop.worker.connect()
        loop.worker.poll()
        clock[0] += 10
        loop.worker.poll()
        loop.worker.poll()
        frames = loop.wire.frames(0)
        frame, = [f for f in frames if f["kind"] == "task.dispatch"]
        did = frame["delivery_id"]
        aid = frame["payload"]["assignment_id"]
        # Replay even if native acceptance is still unknown. The persisted intent
        # prevents a second send. Any subsequent recovery only reads the host.
        loop.worker.process(frame)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            snap = native.snapshot()
            if snap is not None:
                matches = native.receipts(snap, did)
                if matches and snap["thread"]["status"]["type"] == "idle":
                    break
            time.sleep(0.5)
        else:
            return {"status": "unknown", "session": session, "task_id": tid, "assignment_id": aid,
                    "delivery_id": did, "send_calls": port.sends, "reason": "native receipt or idle not observed"}
        if not loop.worker.process(frame):
            raise RuntimeError("Worker did not record recovered native receipt")
        receipt = native.lookup(session, did)
        turn, = [t for t in snap["turns"] if t["id"] == receipt["turn_id"]]
        finals = [i.get("text") for i in turn["items"] if i.get("phase") == "final_answer"]
        if len(matches) != 1 or port.sends != 1 or len(snap["turns"]) != 2:
            raise RuntimeError("expected one native delivery after replay")
        if turn["status"] != "completed" or not finals or receipt["assignment_id"] != aid:
            raise RuntimeError("native turn or assignment mismatch")
        if receipt["task_id"] != tid or loop.adapter.origin(receipt["native_id"]) != {"kind": "task", **frame["payload"]}:
            raise RuntimeError("native input does not match dispatched task")
        loop.worker.turn_finished(receipt["native_id"], finals[-1])
        details = loop.hermes.inspect(tid)
        if details["task"]["state"] != "working" or details["last_report"] is not None:
            raise RuntimeError("native reply was treated as a formal report")
        return {"status": "verified", "receipt": receipt, "final": finals[-1], "native_inputs": len(matches),
                "send_calls": port.sends, "turns_before": len(initial["turns"]), "turns_after_replay": len(snap["turns"]),
                "w03_holder": details["task"]["holder"], "worker_hold": loop.worker.hold(),
                "router_state": details["task"]["state"], "w04": details["last_report"]}


if __name__ == "__main__":
    binding = json.loads(sys.stdin.readline())
    result = run(ToolPort(), binding["session"], binding["bootstrap"])
    print(encode({"op": "result", "result": result}), flush=True)
