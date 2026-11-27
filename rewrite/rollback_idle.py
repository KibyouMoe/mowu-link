"""Account for ordinary link and idle samples from pre-existing, uninvolved workers."""
from .storage import timestamp


RECONCILE = "trial workers or their outstanding queries need reconciliation"


class IdleObservers:
    def __init__(self, registered_before_trial):
        self.known = registered_before_trial
        self.actors = {}
        self.queries = {}
        self.sessions = {}
        self.presence = {}
        self.links = {}
        self.last_seen = {}

    def include(self, workers):
        if not workers <= self.known:
            raise ValueError(RECONCILE)
        self.actors = {"worker/"+worker:worker for worker in workers}

    def query(self, actor, qid, delivery_id):
        if actor in self.actors:
            if qid in self.queries:
                raise ValueError(RECONCILE)
            self.queries[qid] = actor,delivery_id

    def record(self, actor, kind, body, reply, seq, acked):
        wid = self.actors.get(actor)
        if wid is None:
            return
        session = body.get("session_id")
        if kind==["session"]:
            cursor = reply.get("cursor")
            if (not isinstance(session,str) or not session.strip() or type(cursor) is not int or cursor<0
                    or actor in self.sessions and actor not in self.presence
                    or body!={"session_id":session,"held_task_id":None,"held_assignment_id":None}
                    or reply!={"session_id":session,"reconcile":"no_task","release_assignment_ids":[],
                               "pending_cleanup":[],"cursor":cursor}):
                raise ValueError(RECONCILE)
            self.sessions[actor] = session,seq
            self.presence.pop(actor,None)
            return
        active = self.sessions.get(actor)
        if active is None or active[0]!=session:
            raise ValueError(RECONCILE)
        if kind==["presence"]:
            query,sample = body.get("query_id"),body.get("seq")
            expected = self.queries.pop(query,None)
            previous = self.presence.get(actor,-1)
            if (expected is None or expected[0]!=actor or not active[1]<acked.get(expected[1],0)<seq
                    or type(sample) is not int or sample<=previous or body.get("busy") is not False
                    or body!={"session_id":session,"query_id":query,
                        "seq":sample,"busy":False,"held_task_id":None,"held_assignment_id":None}
                    or reply.keys()!={"observed_at"} or not isinstance(reply["observed_at"],str)):
                raise ValueError(RECONCILE)
            self.presence[actor] = sample
        else:
            number, at = body.get("seq"),reply.get("observed_at")
            old = self.links.get(wid)
            if (type(number) is not int or number<=0 or old and old[0]==session and number<=old[1]
                    or body.get("connected") is not True or body!={"session_id":session,"seq":number,"connected":True}
                    or reply!={"worker_id":wid,"seq":number,"connected":True,"observed_at":at}
                    or not isinstance(at,str)):
                raise ValueError(RECONCILE)
            self.links[wid] = session,number,1,at
        self.last_seen[actor] = reply["observed_at"]

    def finish(self, db):
        if self.queries or self.presence.keys()!=self.actors.keys():
            raise ValueError(RECONCILE)
        for actor,wid in self.actors.items():
            row = db.execute("SELECT session_id,seq,last_seen FROM workers WHERE worker_id=?",(wid,)).fetchone()
            link = db.execute("SELECT session,seq,up,observed FROM worker_adapter_links WHERE worker_id=?",(wid,)).fetchone()
            expected_link = self.links.get(wid)
            if ((tuple(link) if link else None)!=expected_link or not row
                    or (row["session_id"],row["seq"])!=(self.sessions[actor][0],self.presence[actor])
                    or timestamp(row["last_seen"])!=self.last_seen[actor]):
                raise ValueError(RECONCILE)
        return sorted(self.actors.values())
