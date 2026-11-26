"""Shared enrollment, local writer locks and session pins for native adapter families."""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from threading import Event, RLock

from .managed_sessions import (enrollment_fields, private_registry, protect_registry_database, read_enrollment,
                               write_enrollment)
from .storage import Database, require, state_writer


@dataclass
class Port:
    adapter: object
    preparation: object
    activation: object
    callback: object
    events: object = None
    approvals: object = None


class SessionFamily:
    def __init__(self,worker,args,schema,root_table):
        self.worker,self.args = worker,args
        self.directory = private_registry(args.managed_sessions_dir)
        self.schema,self.root_table = schema,root_table
        self.resources = ExitStack()
        self.ports = {}
        self.lock = RLock()
        self.stop = Event()

    def _validate(self,document):
        source,operation,session,token = enrollment_fields(document)
        original = self.ports.get(source)
        require(original and original.preparation.prepared(operation).get("session")==session,
                409,"native_enrollment_not_prepared")
        return session,token

    def _register(self,session,token):
        db = self.directory/(session+".sqlite3")
        require(not db.is_symlink(),403,"private_session_registry_required")
        protect_registry_database(db)
        protect_registry_database(db.with_name(db.name+".writer"))
        with ExitStack() as pending:
            pending.enter_context(state_writer(db))
            self.ports[session] = self.make_port(session,db,self.worker.with_token(token),cold=True,resources=pending)
            self.resources.callback(pending.pop_all().close)

    def enroll(self,source,operation_id,session,callback_token):
        document = {"source":source,"operation_id":operation_id,"session":session,"callback_token":callback_token}
        with self.lock:
            require(not self.stop.is_set(),503,"native_host_closed")
            self._validate(document)
            path = self.directory/(session+".json")
            if path.exists():
                require(read_enrollment(path)==document,409,"native_enrollment_conflict")
            else:
                require(not path.is_symlink() and session not in self.ports,409,"native_enrollment_conflict")
                write_enrollment(path,document)
            if session not in self.ports:
                self._register(session,callback_token)
        return {"operation_id":operation_id,"session":session,"state":"enrolled"}

    def port(self,session):
        with self.lock:
            result = self.ports.get(session)
        require(result,409,"wrong_native_session")
        return result

    def enrolled_ports(self):
        with self.lock:
            return list(self.ports.values())

    @contextmanager
    def registry(self):
        with self.resources:
            protect_registry_database(self.directory/"session-registry.writer")
            self.resources.enter_context(state_writer(self.directory/"session-registry"))
            with Database(self.args.db,self.schema).transaction() as db:
                expected = self.args.session,str(self.directory.resolve())
                db.execute(f"INSERT OR IGNORE INTO {self.root_table} VALUES (1,?,?)",expected)
                binding = db.execute(f"SELECT session,directory FROM {self.root_table} WHERE slot=1").fetchone()
                require(tuple(binding)==expected,409,"native_session_registry_changed")
            waiting = [read_enrollment(path) for path in sorted(self.directory.glob("*.json"))]
            registered = {row["session"]+".sqlite3" for row in waiting}
            require(all(path.name in registered for path in self.directory.glob("*.sqlite3")),
                    409,"native_registry_membership_changed")
            session = self.args.session
            # With children present, only the Worker knows which binding may be resumed.
            self.ports[session] = self.make_port(session,self.args.db,self.worker,cold=bool(waiting),
                                                resources=self.resources)
            while waiting:
                ready = [row for row in waiting if enrollment_fields(row)[0] in self.ports]
                require(ready,409,"native_enrollment_parent_unavailable")
                for row in ready:
                    sid,token = self._validate(row)
                    require(sid not in self.ports,409,"native_enrollment_conflict")
                    self._register(sid,token)
                    waiting.remove(row)
            try:
                yield
            finally:
                self.stop.set()
                with self.lock:
                    pass
