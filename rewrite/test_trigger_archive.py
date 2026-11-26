import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from .cutover import digest
from .legacy_test_fixture import source_for
from .trigger_archive import DATABASE, capture, event, restore, verify


class TriggerArchive(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.root/"old.sqlite3"
        self.snapshot = self.root/"prepared"
        self.restored = self.root/"restored"
        self.blocked = self.root/"blocked.jsonl"
        self.blocked.write_text('{"event_id":"blocked","cause":"fixture"}\n',encoding="utf-8")
        self.old("""
store = TriggerStore(sys.argv[1])
store.ensure_subscription('fixture-live',0,3600)
store.ensure_subscription('fixture-closed',0,3600)
store.close_subscription('fixture-closed')
store.add_event('email','旧原文',level='notify',event_id='heard',payload={'id':'原始标识'})
store.ack('fixture-live',1)
store.add_event('mc_chat','continue',level='event',event_id='pending')
store.add_event('email','downgraded',level='info',event_id='info',disposition='downgraded')
store.add_event('email','drop',level='notify',event_id='blocked',disposition='dropped')
store.add_event('email','archive-only',level='log',event_id='kept')
store.close()
""",self.source)

    def old(self,body,database):
        code = "from server import TriggerStore\nimport json,sys\n"+body
        module = source_for("trigger")
        result = subprocess.run([sys.executable,"-c",code,str(database)],cwd=self.root,check=True,
                                env={"HOME":str(self.root),"PATH":os.defpath,"PYTHONPATH":str(module),
                                     "PYTHONDONTWRITEBYTECODE":"1"},capture_output=True,text=True)
        return result.stdout

    def test_archive_copies_actual_legacy_events_and_receipts_without_replaying_them(self):
        original = digest(self.source)
        metadata = capture(self.source,self.snapshot,blocked=self.blocked)
        sha = metadata["manifest_sha256"]
        counts = metadata["inventory"]
        self.assertEqual(counts["events"],5)
        self.assertEqual(counts["subscriptions"],{"active":1,"closed":1})
        self.assertEqual(counts["active_with_unacknowledged_visible_events"],1)
        self.assertEqual(counts["receipts"],1)
        self.assertEqual(counts["dispositions"],{"delivered":3,"downgraded":1,"dropped":1})
        self.assertEqual(verify(self.snapshot,sha)["inventory"],counts)
        old = event(self.snapshot,sha,"heard")
        self.assertEqual((old["text"],json.loads(old["payload_json"])),("旧原文",{"id":"原始标识"}))
        self.assertEqual(event(self.snapshot,sha,"blocked")["disposition"],"dropped")
        self.assertEqual(event(self.snapshot,sha,"kept")["level"],"log")
        self.assertEqual(restore(self.snapshot,sha,self.restored)["files"],2)
        self.assertEqual(digest(self.restored/DATABASE),original)
        self.assertEqual((self.restored/"blocked.jsonl").read_bytes(),self.blocked.read_bytes())
        readback = json.loads(self.old("""
store = TriggerStore(sys.argv[1])
print(json.dumps({
    'unread': [item['event_id'] for item in store.list_events('fixture-live', None)],
    'closed': store.get_subscription('fixture-closed')['status'],
    'count': store.count(),
}))
store.close()
""",self.restored/DATABASE))
        self.assertEqual(readback,{"unread":["pending","info"],"closed":"closed","count":5})
        self.assertEqual(digest(self.source),original)

    def test_rejects_active_journal_and_tampered_archive_member(self):
        db = sqlite3.connect(self.source)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE events SET priority=1")
        with self.assertRaisesRegex(ValueError,"live journal"):
            capture(self.source,self.snapshot)
        db.rollback()
        db.close()
        self.assertFalse(self.snapshot.exists())
        result = capture(self.source,self.snapshot,blocked=self.blocked)
        (self.snapshot/"blocked.jsonl").write_text("changed",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"bytes differ"):
            restore(self.snapshot,result["manifest_sha256"],self.restored)
        self.assertFalse(self.restored.exists())
