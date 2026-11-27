"""Audit the stopped Worker and Feishu ledgers alongside a reviewed legacy rollback."""
from contextlib import closing

from . import feishu_history, state_bundle
from .cutover import readonly


class ComponentReturn:
    def __init__(self,bundle,sha,router_sha,dsh_runtimes):
        self.bundle,self.sha,self.runtimes = bundle,sha,dsh_runtimes
        self.manifest = state_bundle.verify(bundle,sha)
        router, = (label for label in self.manifest["files"] if label.startswith("router:"))
        if self.manifest["files"][router]["sha256"]!=router_sha:
            raise ValueError("component proof and trial Router must use the same stopped database")
        with closing(readonly(bundle/state_bundle.filename(router))) as db:
            workers = {row[0] for row in db.execute("SELECT worker_id FROM workers")}
        self.audit = state_bundle.audit_handoff(bundle,sha,dsh_runtimes=dsh_runtimes)
        audited = {row["worker_id"] for row in self.audit["workers"]}
        if workers!=audited:
            raise ValueError("component proof must cover each trial Router Worker")
        for row in self.audit["workers"]:
            pending = [name for name,count in row["native_session_pending"].items() if count!=0]
            if not row["native_session_ledger_clear"]:
                detail = ", ".join(pending+row["issues"])
                raise ValueError("component rollback still pending for "+row["worker_id"]+": "+detail)
        if self.audit["needs_review"]:
            raise ValueError("component rollback still needs reconciliation")
        self.workspaces = state_bundle.workspace_audits(bundle,self.manifest,dsh_runtimes)

    def stage(self,folder,reservations):
        if state_bundle.workspace_audits(self.bundle,self.manifest,self.runtimes,
                                        reservations=reservations)!=self.workspaces:
            raise ValueError("workspace journals changed during component rollback")
        target = folder/"feishu-history"
        platforms = [label for label in sorted(self.manifest["files"]) if label.startswith("feishu:")]
        histories = {}
        if platforms:
            target.mkdir(mode=0o700)
        for label in platforms:
            _,wid = state_bundle.component(label)
            histories[wid] = feishu_history.prepare(self.bundle,self.sha,target/wid,wid)
        return {"component_manifest_sha256":self.sha,"snapshot_id":self.manifest["snapshot_id"],
                "source_revision":self.manifest["source_revision"],"workers":len(self.audit["workers"]),
                "feishu_history":histories}

    def verify(self):
        if state_bundle.verify(self.bundle,self.sha)!=self.manifest:
            raise ValueError("component bundle changed during rollback")
