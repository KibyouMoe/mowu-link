import hashlib
import unittest

from .devloop import Loop, agent_report
from .storage import Fault, guid
from .test_mainline import Clock


class WorkFiles(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock,extra_workers=["other"]))
        self.w, self.m = self.loop.worker,self.loop.hermes
        self.tid = self.m.create(guid(),"files","Send tested file","oguri")["task_id"]
        self.w.connect()
        self.w.poll()
        self.clock.advance(10)
        self.w.poll()
        self.w.poll()
        self.aid = self.w.hold()["held_assignment_id"]
        self.native = self.loop.native.entries()[0][1]["native_id"]

    def upload(self,wire):
        data = b"scope proof"
        res = wire.request("POST","uploads",{"name":"proof","sha256":hashlib.sha256(data).hexdigest(),
                           "size_bytes":len(data)},guid())
        return wire.request("PATCH","uploads/"+res["upload_id"],data,headers={"Upload-Offset":"0"})["attachment_id"]

    def test_report_grants_only_linked_files_after_atomic_success(self):
        good = self.upload(self.loop.wire)
        other = self.upload(self.loop.extra_clients["other"])
        args = {"task_id":self.tid,"assignment_id":self.aid,"summary":"evidence","artifact_ids":[good,other]}
        rejected = agent_report(self.w,self.native,args)
        self.assertTrue(rejected["isError"])
        with self.assertRaises(Fault):
            self.loop.manager_wire.request("GET","attachments/"+good)
        self.assertIsNone(self.m.inspect(self.tid)["last_report"])
        args["artifact_ids"] = [good]
        result = agent_report(self.w,self.native,args)
        self.assertFalse(result["isError"])
        self.assertEqual(result,agent_report(self.w,self.native,args))
        self.assertEqual(self.m.inspect(self.tid)["last_report"]["artifact_ids"],[good])
        notify, = self.loop.manager_wire.frames(0)
        self.assertEqual(notify["payload"]["attachment_ids"],[good])
        self.assertEqual(self.loop.manager_wire.request("GET","attachments/"+good)["bytes"], b"scope proof")

    def test_attachment_only_candidate_never_writes_a_report(self):
        good = self.upload(self.loop.wire)
        self.clock.advance(630)
        self.assertIsNone(self.m.inspect(self.tid)["last_report"])
        body = {"session_id":self.w.session,"assignment_id":self.aid,"artifact_ids":[good]}
        received = self.loop.wire.request("POST",f"tasks/{self.tid}/materials",body,guid())
        self.assertNotIn("release_assignment_id",received)
        details = self.m.inspect(self.tid)
        self.assertEqual(details["materials"][0]["artifact_ids"],[good])
        self.assertIsNone(details["last_report"])
        self.assertEqual(details["task"]["state"],"blocked")
