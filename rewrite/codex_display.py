"""Relay public native events and reconcile terminal turns from one Codex session."""
from contextlib import contextmanager
import json
import logging
from queue import Empty, SimpleQueue
from sqlite3 import OperationalError
from threading import Event, Thread

from .codex_app_server import TERMINAL
from .storage import Database, Fault, encode, require, string

SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_display_binding (
 slot INTEGER PRIMARY KEY CHECK(slot=1), session TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codex_display_turns (
 seq INTEGER PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE, sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS codex_display_live (
 seq INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, event TEXT NOT NULL,
 sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS codex_display_live_turn ON codex_display_live(turn_id,seq);
"""


class CodexDisplay:
    def __init__(self,adapter,worker):
        self.db = Database(adapter.db.path,SCHEMA)
        self.host,self.worker = adapter.host,worker
        self.session = adapter.bound_session()
        self.wake,self.stop = Event(),Event()
        self.events = SimpleQueue()
        self.buffered = []

    def observe(self,event):
        self.events.put(event)
        self.wake.set()

    def collect(self):
        while True:
            try:
                self.buffered.append(self.events.get_nowait())
            except Empty:
                break
        turns = self.host.thread(self.session,include_turns=True)["turns"]
        native_ids = [string(turn,"id") for turn in turns]
        require(len(set(native_ids))==len(native_ids),409,"native_display_history_changed")
        with self.db.transaction() as db:
            binding = db.execute("SELECT session FROM codex_display_binding WHERE slot=1").fetchone()
            if not binding:
                # Initial attachment never republishes existing native history.
                db.execute("INSERT INTO codex_display_binding VALUES (1,?)",(self.session,))
                db.executemany("INSERT INTO codex_display_turns(turn_id,sent) VALUES (?,1)",((tid,) for tid in native_ids))
            else:
                require(binding[0]==self.session,409,"display_binding_changed")
                known = [row[0] for row in db.execute("SELECT turn_id FROM codex_display_turns ORDER BY seq")]
                require(known==native_ids[:len(known)],409,"native_display_history_changed")
                db.executemany("INSERT INTO codex_display_turns(turn_id) VALUES (?)",
                               ((tid,) for tid in native_ids[len(known):]))
            state = {row["turn_id"]:row["sent"] for row in db.execute("SELECT turn_id,sent FROM codex_display_turns")}
            waiting = []
            for event in self.buffered:
                tid,key = event["turn_id"],event["event_id"]
                if tid not in state:
                    waiting.append(event)
                elif not state[tid]:
                    raw = encode(event)
                    db.execute("INSERT OR IGNORE INTO codex_display_live(turn_id,event_id,event) VALUES (?,?,?)",
                               (tid,key,raw))
                    known = db.execute("SELECT event FROM codex_display_live WHERE event_id=?",(key,)).fetchone()
                    require(known[0]==raw,409,"display_event_conflict")
        self.buffered = waiting

    def flush(self):
        with self.db.transaction() as db:
            pending = [row[0] for row in db.execute("SELECT turn_id FROM codex_display_turns WHERE sent=0 ORDER BY seq")]
        for tid in pending:
            if self.stop.is_set():
                return
            turn = self.host.read_turn(self.session,tid)
            with self.db.transaction() as db:
                ready = db.execute("SELECT * FROM codex_display_live WHERE turn_id=? AND sent=0 ORDER BY seq",
                                   (tid,)).fetchall()
            for row in ready:
                self.host.publish_public_item(self.worker,json.loads(row["event"]))
                with self.db.transaction() as db:
                    db.execute("UPDATE codex_display_live SET sent=1 WHERE seq=?",(row["seq"],))
            if turn["status"] not in TERMINAL:
                return
            self.host.publish_public_turn(self.session,tid,self.worker,turn=turn)
            with self.db.transaction() as db:
                db.execute("DELETE FROM codex_display_live WHERE turn_id=?",(tid,))
                db.execute("UPDATE codex_display_turns SET sent=1 WHERE turn_id=?",(tid,))

    def pump(self):
        while not self.stop.is_set():
            if not self.wake.wait(.2):
                continue
            self.wake.clear()
            if self.stop.is_set():
                return
            try:
                self.collect()
                self.flush()
                if self.buffered and not self.stop.wait(.2):
                    self.wake.set()
            except (Fault,OSError,OperationalError) as error:
                cause = error.code if isinstance(error,Fault) else type(error).__name__
                logging.warning("Codex display pending: %s",cause)
                retryable = not isinstance(error,Fault) or error.retryable or error.status>=500
                if retryable and not self.stop.wait(1):
                    self.wake.set()

    @contextmanager
    def run(self):
        self.collect()
        self.wake.set()
        thread = Thread(target=self.pump,daemon=True)
        thread.start()
        try:
            yield
        finally:
            self.stop.set()
            self.wake.set()
            thread.join(timeout=3)
