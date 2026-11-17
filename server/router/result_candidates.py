"""Durable, nonterminal result candidates. The Router dispatch lock owns mutations."""

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path


class ResultCandidates:
    def __init__(self, database_path):
        self.root = Path(str(database_path) + ".result-candidates")

    def _path(self, task_id):
        return self.root / (hashlib.sha256(task_id.encode("utf-8")).hexdigest() + ".json")

    def get(self, task_id):
        try:
            value = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if value.get("task_id") != task_id or value.get("version") != 1:
            raise ValueError("invalid_result_candidate")
        return value

    def put(self, task_id, result):
        old = self.get(task_id)
        if old is not None and old["result"] == result:
            return old
        value = {"version": 1, "task_id": task_id, "revision": uuid.uuid4().hex, "result": result}
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="candidate-", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(task_id))
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return value

    def remove(self, task_id):
        try:
            self._path(task_id).unlink()
        except FileNotFoundError:
            pass
