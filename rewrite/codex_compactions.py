"""Verify native App Server compaction against its own turn history."""
import json

from .storage import Database, encode, require, string


class CodexCompactions:
    def __init__(self, path, host):
        self.host = host
        self.db = Database(path,"""
            CREATE TABLE IF NOT EXISTS codex_compactions (
                command_id TEXT PRIMARY KEY, session TEXT NOT NULL, anchor TEXT NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 0, receipt TEXT
            );
        """)

    def run(self,session,command_id,*,start):
        host = self.host
        require(session==host.thread_id,409,"wrong_native_session")
        scope = {"session":session,"command_id":command_id}
        with host.write_lock:
            with self.db.transaction() as db:
                prior = db.execute("SELECT * FROM codex_compactions WHERE command_id=?",(command_id,)).fetchone()
            if prior:
                require(prior["session"]==session,409,"wrong_native_session")
                return self.readback(scope,prior)
            if not start:
                return {**scope,"state":"missing"}
            goal = host.request("thread/goal/get",{"threadId":session})["goal"]
            require(goal is None or isinstance(goal,dict) and goal.get("threadId")==session,
                    502,"wrong_native_goal_view")
            if host.busy(session) or goal and goal.get("status")=="active":
                return {**scope,"state":"busy"}
            history = host.thread(session,include_turns=True)
            if history["status"]["type"]!="idle":
                return {**scope,"state":"busy"}
            turns = history["turns"]
            if not turns:
                return {**scope,"state":"empty"}
            anchor = string(turns[-1],"id")
            with self.db.transaction() as db:
                db.execute("INSERT INTO codex_compactions(command_id,session,anchor) VALUES (?,?,?)",
                           (command_id,session,anchor))
            native = host.request("thread/compact/start",{"threadId":session})
            require(native=={},502,"native_compaction_unconfirmed")
            with self.db.transaction() as db:
                db.execute("UPDATE codex_compactions SET accepted=1 WHERE command_id=?",(command_id,))
                current = db.execute("SELECT * FROM codex_compactions WHERE command_id=?",(command_id,)).fetchone()
            return self.readback(scope,current)

    def readback(self,scope,entry):
        if entry["receipt"]:
            return {**scope,**json.loads(entry["receipt"])}
        if not entry["accepted"]:
            return {**scope,"state":"unknown"}
        turns = self.host.thread(scope["session"],include_turns=True)["turns"]
        anchor = next((i for i,turn in enumerate(turns) if turn["id"]==entry["anchor"]),None)
        if anchor is None:
            return {**scope,"state":"unknown"}
        if len(turns)==anchor+1 or turns[anchor+1]["status"]=="inProgress":
            return {**scope,"state":"running"}
        turn = turns[anchor+1]
        items = turn["items"]
        if len(items)!=1 or items[0].get("type")!="contextCompaction":
            return {**scope,"state":"unknown"}
        if turn["status"]!="completed":
            if turn["status"] not in ("failed","interrupted"):
                return {**scope,"state":"unknown"}
            proof = {"state":"failed"}
        else:
            proof = {"state":"completed","turn_id":string(turn,"id"),"compaction_id":string(items[0],"id")}
        with self.db.transaction() as db:
            db.execute("UPDATE codex_compactions SET receipt=? WHERE command_id=?",
                       (encode(proof),scope["command_id"]))
        return {**scope,**proof}
