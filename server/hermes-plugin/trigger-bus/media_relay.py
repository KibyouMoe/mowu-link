"""Durable native image delivery through the captured Feishu gateway adapter."""
import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path


TITLE = "客户端原生图片输入"
DEDUP_SECONDS = 600
MAX_BYTES = 10_000_000


class NativeImageRelay:
    def __init__(self, database, cache_root, deliver, receipt, *, stop=None,
                 clock=time.time, start_worker=True):
        self.database = Path(database)
        self.cache_root = Path(cache_root).resolve()
        self.deliver = deliver
        self.receipt = receipt
        self.clock = clock
        self.stop = stop or threading.Event()
        self.wake = threading.Event()
        self.logger = logging.getLogger("trigger-bus")
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS events ("
                       "key TEXT PRIMARY KEY, chat TEXT, payload TEXT, "
                       "status TEXT, card_id TEXT, result TEXT, acked INTEGER DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS media ("
                       "chat TEXT, sha TEXT, started REAL, status TEXT, "
                       "message_id TEXT, error TEXT, PRIMARY KEY(chat,sha))")
        self.thread = None
        if start_worker:
            self.thread = threading.Thread(target=self._run, daemon=True,
                                           name="trigger-native-images")
            self.thread.start()

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def submit(self, event, chat_id):
        key = str(event.get("event_id") or event.get("id") or "")
        if not key or not chat_id:
            raise ValueError("native image event and destination are required")
        payload = {"event_id": key, "worker": (event.get("payload") or {}).get("worker") or event.get("actor"),
                   "files": event.get("_attachment_files") or [],
                   "error": event.get("_attachment_error")}
        with closing(self._connect()) as db, db:
            changed = db.execute("INSERT OR IGNORE INTO events "
                                 "(key,chat,payload,status) VALUES (?,?,?,'pending')",
                                 (key, chat_id, json.dumps(payload))).rowcount
        self.wake.set()
        return bool(changed)

    @staticmethod
    def _confirmed(result):
        return isinstance(result, dict) and result.get("success") is True and \
            result.get("platform") == "feishu" and bool(result.get("message_id"))

    def _verified_file(self, item):
        filename = Path(item["path"]).resolve()
        sha = item.get("sha256", "")
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("图片标识校验失败")
        if self.cache_root not in filename.parents or filename.stem != sha:
            raise ValueError("图片缓存路径校验失败")
        size = filename.stat().st_size
        if size > MAX_BYTES:
            raise ValueError("图片过大未转发")
        if not 0 < size == item.get("size"):
            raise ValueError("图片字节数校验失败")
        digest = hashlib.sha256()
        with filename.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha:
            raise ValueError("图片内容校验失败")
        return str(filename), sha, size

    def _image(self, chat, item):
        try:
            filename, sha, size = self._verified_file(item)
        except (ValueError, OSError, KeyError):
            return {"success": False, "sha256": item.get("sha256"),
                    "error": "图片文件校验失败"}
        now = self.clock()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM media WHERE chat=? AND sha=?", (chat, sha)).fetchone()
            if old and now - old["started"] < DEDUP_SECONDS:
                db.commit()
                return {"success": old["status"] == "sent", "sha256": sha, "size": size,
                        "message_id": old["message_id"], "duplicate": True,
                        "error": old["error"] or (None if old["status"] == "sent" else "图片投递结果未确认")}
            db.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,'dispatching',NULL,NULL)", (chat, sha, now))
            db.commit()
        try:
            result = self.deliver("image", chat, filename)
            ok = self._confirmed(result)
        except Exception:
            result, ok = {}, False
        message_id = result.get("message_id") if ok else None
        error = None if ok else "飞书未确认图片送达"
        with closing(self._connect()) as db, db:
            db.execute("UPDATE media SET status=?,message_id=?,error=?,started=? WHERE chat=? AND sha=?",
                       ("sent" if ok else "failed", message_id, error, self.clock(), chat, sha))
        self.logger.info("native_image_delivery name=%s bytes=%d attachment_id=%s delivered=%s message_id=%s",
                         Path(filename).name, size, sha, ok, message_id)
        return {"success": ok, "sha256": sha, "size": size,
                "message_id": message_id, "duplicate": False, "error": error}

    def process_one(self):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM events WHERE status!='done' ORDER BY rowid LIMIT 1").fetchone()
        if row is None:
            return False
        key, chat = row["key"], row["chat"]
        payload = json.loads(row["payload"])
        card_id, error = row["card_id"], payload.get("error")
        if row["status"] == "text":
            error = "文字卡片投递结果未确认"
        elif card_id is None:
            with closing(self._connect()) as db, db:
                db.execute("UPDATE events SET status='text' WHERE key=?", (key,))
            try:
                card = self.deliver("text", chat, TITLE)
                if self._confirmed(card):
                    card_id = card["message_id"]
                else:
                    error = "飞书未确认文字卡片送达"
            except Exception:
                error = "文字卡片发送失败"
            with closing(self._connect()) as db, db:
                db.execute("UPDATE events SET status='media',card_id=? WHERE key=?", (card_id, key))
        images = []
        if not error:
            images = [self._image(chat, item) for item in payload["files"]]
            error = next((image["error"] for image in images if not image["success"]), None)
        if error:
            body = TITLE + "\n图片发送失败：" + error
            try:
                edited = self.deliver("edit", chat, body, card_id) if card_id else None
                if not self._confirmed(edited):
                    fallback = self.deliver("text", chat, body)
                    if not self._confirmed(fallback):
                        self.logger.error("native_image_failure_notice_unconfirmed event_id=%s", key)
            except Exception:
                self.logger.error("native_image_failure_notice_unconfirmed event_id=%s", key)
        result = {"event_id": key, "success": error is None, "platform": "feishu",
                  "text_message_id": card_id, "images": images, "error": error}
        with closing(self._connect()) as db, db:
            db.execute("UPDATE events SET status='done',result=? WHERE key=?", (json.dumps(result), key))
        return True

    def acknowledge_one(self):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM events WHERE status='done' AND acked=0 ORDER BY rowid LIMIT 1").fetchone()
        if row is None:
            return False
        payload, result = json.loads(row["payload"]), json.loads(row["result"])
        try:
            if payload["worker"] and not self.receipt(payload, result):
                return False
        except Exception:
            self.logger.warning("native_image_receipt_pending event_id=%s", row["key"])
            return False
        with closing(self._connect()) as db, db:
            db.execute("UPDATE events SET acked=1 WHERE key=?", (row["key"],))
        return True

    def _run(self):
        while not self.stop.is_set():
            try:
                worked = self.process_one()
                acked = self.acknowledge_one()
                if worked or acked:
                    continue
            except Exception:
                self.logger.exception("native_image_relay_failed")
            self.wake.wait(2)
            self.wake.clear()
