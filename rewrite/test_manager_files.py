"""A07 and H02 receive original bytes with an independently checked local receipt."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .devloop import Loop
from .hermes import HermesAdapter
from .hermes_tools import ManagerTools
from .storage import guid
from .trigger import Trigger


class ManagerFiles(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.source = Trigger(self.root/"trigger.db","attachments")
        self.loop = self.enterContext(Loop.isolated(trigger_ids=[self.source.instance_id]))
        self.manager = self.loop.hermes

    def submit(self, level, *contents):
        files = []
        for i, data in enumerate(contents):
            path = self.root/f"原图 {i}.bin"
            path.write_bytes(data)
            files.append(path)
        mid = self.source.record(level,"原件",files)
        self.source.send(self.loop.trigger_clients[self.source.instance_id],mid)
        return mid

    def test_partial_host_download_resumes_without_acknowledging_absent_bytes(self):
        data = bytes(range(256))*2500
        mid = self.submit("Event",data,b"")
        send = self.manager.router.request
        offsets = []
        def drop(method,path,*args,**kwargs):
            if path.startswith("attachments/") and "headers" in kwargs:
                part = kwargs["headers"]["Range"]
                offsets.append(part)
                if len(offsets)==2:
                    raise ConnectionError("interrupted range")
            return send(method,path,*args,**kwargs)
        self.manager.router.request = drop
        with self.assertRaises(ConnectionError):
            self.manager.poll()
        with self.manager.db.transaction() as db:
            self.assertIsNone(db.execute("SELECT receipt FROM queue WHERE message_id=?",(mid,)).fetchone()[0])
        self.assertFalse(self.loop.hermes_native.entries())
        restarted = HermesAdapter(self.manager.db.path,self.manager.router,self.manager.host)
        receipt = restarted.poll()
        self.assertEqual(offsets,["bytes=0-262143","bytes=262144-524287","bytes=262144-524287","bytes=524288-639999"])
        self.assertEqual(receipt["media_sha256"],[hashlib.sha256(d).hexdigest() for d in (data,b"")])
        payload, _ = self.loop.hermes_native.entries()[0]
        self.assertEqual([f["name"] for f in payload["attachments"]],["原图 0.bin","原图 1.bin"])
        self.assertIsNone(restarted.poll())
        self.assertEqual(len(self.loop.hermes_native.entries()),1)

    def test_on_demand_access_checks_message_scope_even_for_cached_file(self):
        data = b"owned by the first Info"
        mid = self.submit("Info",data)
        other = self.submit("Info",b"second")
        binding = ManagerTools(self.manager,"manager")
        def call(args,session="manager"):
            return json.loads(binding.around_call("router_info",lambda a:binding.invoke("router_info",a),args,
                                                  session_id=session))
        file_id = call({"message_id":mid})["items"][0]["attachments"][0]["attachment_id"]
        args = {"message_id":mid,"attachment_id":file_id}
        result = call(args)
        path = Path(result["local_path"])
        self.assertEqual(path.parent,self.manager.file_store.root)
        self.assertEqual(path.read_bytes(),data)
        self.assertEqual(call({**args,"message_id":other})["status"],404)
        self.assertEqual(call({**args,"message_id":guid()})["status"],404)
        self.assertEqual(call(args,"unbound")["status"],403)
        self.assertEqual(call({"attachment_id":file_id})["status"],422)
        self.assertFalse(self.loop.manager_wire.frames(0))
        self.assertFalse(self.loop.hermes_native.entries())
