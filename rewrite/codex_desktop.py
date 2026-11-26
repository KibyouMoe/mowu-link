"""Agent host for an explicitly bound local Codex Desktop test chat.

The Desktop-side caller supplies its own read_thread and send_message_to_thread
capabilities. No app-server connection, production Worker binding or account token.
"""
import hashlib
import json
from typing import Protocol
from xml.etree import ElementTree

from .storage import encode, require

PREFIX = "魔物链接独立工单\n"


class DesktopPort(Protocol):
    def read(self) -> dict: ...
    def send(self, prompt: str) -> None: ...


def body_digest(payload):
    return hashlib.sha256(encode(payload).encode()).hexdigest()


def envelope(session, did, payload):
    return PREFIX + encode({"session": session, "delivery_id": did, "payload": payload,
                            "sha256": body_digest(payload)})


def native_input(item, method):
    if (item.get("type"), item.get("namespace"), item.get("name")) != ("functionCallOutput", "codex_app", method):
        return None
    output = item.get("output", {})
    if output.get("truncated") is not False or not isinstance(output.get("text"), str):
        return None
    try:
        root = ElementTree.fromstring(output["text"])
    except ElementTree.ParseError:
        return None
    if root.tag == "codex_delegation":
        source, text = root.findtext("source_thread_id"), root.findtext("input")
        if source and text is not None:
            return source, text
    return None


class CodexDesktop:
    def __init__(self, port: DesktopPort, session, bootstrap):
        self.port, self.session, self.bootstrap = port, session, bootstrap
        self.source = None
        snap = self.snapshot()
        require(snap is not None, 409, "desktop_binding_unconfirmed")
        sources = [record[0] for turn in snap["turns"] for item in turn["items"]
                   if (record := native_input(item, "create_thread")) and record[1] == bootstrap]
        require(len(sources) == 1 and sources[0] and sources[0] != session, 409, "desktop_binding_unconfirmed")
        self.source = sources[0]

    def snapshot(self):
        try:
            snap = self.port.read()
        except (OSError, TimeoutError):
            return None
        meta = snap.get("thread", {})
        require(meta.get("id") == self.session and meta.get("kind") == "codex"
                and meta.get("hostId") == "local", 409, "desktop_target_mismatch")
        if snap.get("page", {}).get("hasMore") is not False:
            return None
        turns = snap.get("turns")
        if not isinstance(turns, list) or any(t.get("nextItemCursor") or "items" not in t for t in turns):
            return None
        for turn in turns:
            for item in turn["items"]:
                if (item.get("type") == "functionCallOutput" and item.get("namespace") == "codex_app"
                        and item.get("name") == "send_message_to_thread"
                        and native_input(item, "send_message_to_thread") is None):
                    return None
        return snap

    def busy(self, session):
        require(session == self.session, 409, "desktop_target_mismatch")
        snap = self.snapshot()
        return snap is None or snap["thread"].get("status", {}).get("type") != "idle"

    def deliver(self, session, did, payload, digest):
        require(session == self.session, 409, "desktop_target_mismatch")
        require(digest == body_digest(payload), 409, "desktop_digest_mismatch")
        if self.busy(session):
            return None
        try:
            self.port.send(envelope(session, did, payload))
        except OSError:
            # The write may have reached Desktop. Only a native read can prove it.
            pass
        return self.lookup(session, did)

    def receipts(self, snap, did):
        matches = []
        for turn in snap["turns"]:
            for item in turn["items"]:
                parsed = native_input(item, "send_message_to_thread")
                if not parsed or parsed[0] != self.source or not isinstance(parsed[1], str):
                    continue
                text = parsed[1]
                if not text.startswith(PREFIX):
                    continue
                try:
                    data = json.loads(text.removeprefix(PREFIX))
                except ValueError:
                    continue
                if not isinstance(data, dict) or data.get("delivery_id") != did:
                    continue
                payload = data.get("payload")
                require(isinstance(payload, dict) and text == envelope(self.session, did, payload)
                        and item.get("id") and turn.get("id"), 409, "desktop_input_mismatch")
                matches.append({"session": self.session, "delivery_id": did, "digest": data["sha256"],
                                "native_id": item["id"], "turn_id": turn["id"],
                                "assignment_id": payload.get("assignment_id"), "task_id": payload.get("task_id")})
        return matches

    def lookup(self, session, did):
        require(session == self.session, 409, "desktop_target_mismatch")
        snap = self.snapshot()
        if snap is None:
            return None
        matches = self.receipts(snap, did)
        require(len(matches) <= 1, 409, "duplicate_desktop_input")
        return matches[0] if matches else None
