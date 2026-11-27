"""Resolve the old implementation solely for the migration and rollback tests."""
import os
from pathlib import Path
from contextlib import closing
import subprocess
import sys

from . import legacy_history, legacy_peers
from .cutover import digest, readonly


def archival_compatibility(test):
    """Keep the test available for historical compatibility runs."""
    test.archival_compatibility = True
    return test


def source_for(component):
    root = Path(os.environ.get("MOWULINK_TEST_LEGACY_ROOT") or Path(__file__).resolve().parents[1])
    source = (root/"server"/component).resolve()
    if not (source/"server.py").is_file():
        raise RuntimeError(f"Legacy {component} test source is missing. Set MOWULINK_TEST_LEGACY_ROOT "
                           "to a directory containing the pinned old server sources.")
    return source


def import_acked_peer(loop, source, mid):
    """Write and acknowledge one real legacy HTTP message, then import its archive in the test Router."""
    database = loop.root/"legacy-peer-fixture.db"
    files = loop.root/"legacy-peer-files"
    files.mkdir()
    program = """
from server import RouterStore, RouterServer
from selftest_router import http
from threading import Thread
from unittest.mock import Mock
import sys
store = RouterStore(sys.argv[1])
for name in ("tama","oguri"):
    store.upsert_worker(name)
with RouterServer(("127.0.0.1",0),store,"test-peer-archive",Mock(),Mock()) as server:
    thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/messages"
        packet = {"message_id":sys.argv[2],"from":"tama","to":"oguri","text":"Old Router original text"}
        status,_ = http("POST",url,packet,"test-peer-archive")
        ack,_ = http("POST",url+"/"+sys.argv[2]+"/ack",{"worker":"oguri"},"test-peer-archive")
        assert (status,ack)==(202,200)
    finally:
        server.shutdown()
        thread.join()
store.close()
"""
    process = subprocess.run([sys.executable,"-B","-c",program,str(database),mid],
        env={"PATH":os.defpath,"HOME":str(loop.root),"PYTHONPATH":str(source_for("router"))},
        cwd=loop.root,capture_output=True,text=True,timeout=12)
    if process.returncode:
        raise AssertionError(process.stderr)
    checksum = digest(database)
    with closing(readonly(database)) as old, loop.orders.db.transaction() as db:
        legacy_history.populate(db,old,source,checksum,{})
        legacy_peers.populate(db,old,source,files)
    return database,checksum
