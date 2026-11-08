#!/usr/bin/env python3
"""filterwall.py — L1 only: per-source business filtering + envelope checks.

0.6.0 split the old filter wall in two:

  * **L1 (this file, per source client)** — only what is true for *this*
    source's own business semantics.  The default hook accepts everything
    after a structural check; a client that knows something special (MC's
    "don't send my own chat back", email's "only this folder") overrides
    ``business_filter`` or wraps it.
  * **L2 (server, ``~/.hermes/trigger-rules.json``)** — allow/deny lists,
    rate limits, dedupe windows, priority mapping, quiet periods.  Those are
    cross-source and configured once for the whole bus; they are **not**
    implemented here any more.

Public API::

    from filterwall import business_filter, log_skip

    keep, reason = business_filter(envelope)   # reason == "" when keep
    log_skip("drops.log", "mc_chat", "self_message", text, actor)

``business_filter`` returns ``(True, "")`` for a structurally valid envelope
and ``(False, "schema_invalid")`` for a missing/mistyped field.  A source
client may compose it with its own rule::

    ok, reason = business_filter(env)
    if ok and sender == own_name and not include_self:
        ok, reason = False, "self_message"

Nothing is silently lost: callers write one line per skip to their local
``drops.log`` with :func:`log_skip`, and the server writes its own
``trigger-blocked.jsonl`` for L2 blocks.

Standard library only. Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TEXT_HEAD_CHARS = 80
REASON_SCHEMA = "schema_invalid"
LEVELS = ("event", "notify", "info", "log")
# Legacy input names, folded to the top level by the server.
LEVEL_ALIASES = {"super": "event", "super_event": "event"}

# Field -> expected Python type.  Only fields that a client actually emits
# are checked; ``payload`` is free-form JSON and deliberately unchecked.
_FIELD_TYPES: Dict[str, type] = {
    "source": str,
    "kind": str,
    "level": str,
    "actor": str,
    "text": str,
    "subject": str,
    "body": str,
    "thread_key": str,
    "dedupe_key": str,
    "event_id": str,
    "priority": int,
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _text_head(text: Any, limit: int = TEXT_HEAD_CHARS) -> str:
    return " ".join(str(text or "").split())[:limit]


def schema_problem(envelope: Any) -> str:
    """Return a human-readable schema problem, or ``""`` when valid."""
    if not isinstance(envelope, dict):
        return "envelope is not a JSON object"
    for field in ("source", "text"):
        if field not in envelope:
            return "missing required field %r" % field
        if not isinstance(envelope[field], str):
            return "%s must be a string (got %s)" % (
                field, type(envelope[field]).__name__)
    if not envelope["source"].strip():
        return "source is empty"
    if not envelope["text"].strip():
        return "text is empty"
    for field, expected in _FIELD_TYPES.items():
        if field not in envelope or envelope[field] is None:
            continue
        value = envelope[field]
        if expected is int:  # bool is an int subclass; never accept it
            if isinstance(value, bool) or not isinstance(value, int):
                return "%s must be an integer" % field
        elif not isinstance(value, expected):
            return "%s must be a %s (got %s)" % (
                field, expected.__name__, type(value).__name__)
    if "level" in envelope and LEVEL_ALIASES.get(
            envelope["level"], envelope["level"]) not in LEVELS:
        return "level must be one of %s" % ", ".join(LEVELS)
    return ""


def business_filter(envelope: Any) -> Tuple[bool, str]:
    """L1 hook: structural check only; subclasses/clients add their own.

    Returns ``(keep, reason)``; ``reason`` is ``""`` when the envelope may be
    sent to the trigger bus.
    """
    problem = schema_problem(envelope)
    if problem:
        # The detail goes to the caller's debug log; the machine-readable
        # reason stays stable so drops.log can be aggregated.
        return False, REASON_SCHEMA
    return True, ""


def log_skip(log_path: Any, source: str, reason: str, text: Any,
             actor: str = "") -> None:
    """Append one L1 skip to ``drops.log``; a bad path must never raise."""
    if not log_path:
        return
    entry = {
        "at": now_iso(),
        "source": str(source),
        "reason": str(reason),
        "text_head": _text_head(text),
    }
    if actor:
        entry["actor"] = _text_head(actor, 60)
    try:
        path = Path(log_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print("warning: cannot write skip log %s: %s" % (log_path, exc),
              file=sys.stderr)


# --------------------------------------------------------------------------- #
# legacy drops.log aggregation (L1 skips and old files)
# --------------------------------------------------------------------------- #

def read_drop_stats(log_path: Any) -> Dict[str, Any]:
    path = Path(log_path).expanduser()
    result: Dict[str, Any] = {"log_path": str(path), "exists": path.exists(),
                              "total": 0, "by_reason": {}, "by_source": {},
                              "first_ts": None, "last_ts": None, "recent": []}
    if not path.exists():
        return result
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                result["total"] += 1
                reason = str(record.get("reason") or "unknown")
                source = str(record.get("source") or "unknown")
                result["by_reason"][reason] = result["by_reason"].get(reason, 0) + 1
                result["by_source"][source] = result["by_source"].get(source, 0) + 1
                stamp = record.get("at") or record.get("ts")
                if stamp:
                    if result["first_ts"] is None:
                        result["first_ts"] = stamp
                    result["last_ts"] = stamp
                result["recent"].append(record)
    except OSError as exc:
        result["error"] = str(exc)
        return result
    result["recent"] = result["recent"][-5:]
    return result


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #

def selftest() -> int:
    results: List[Tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    valid = {"source": "mc_chat", "kind": "chat", "actor": "Steve",
             "text": "hello", "priority": 2, "event_id": "e1"}
    keep, reason = business_filter(valid)
    check("01 valid envelope passes", keep is True and reason == "",
          "keep=%s reason=%r" % (keep, reason))

    keep, reason = business_filter({"source": "mc_chat", "text": "hi"})
    check("02 missing actor is fine (optional)",
          keep is True and reason == "", "keep=%s reason=%r" % (keep, reason))

    for label, envelope in (
            ("03 missing source", {"text": "hi"}),
            ("04 missing text", {"source": "mc_chat"}),
            ("05 text not a string", {"source": "mc_chat", "text": 123}),
            ("06 priority not an int", {"source": "mc_chat", "text": "hi",
                                        "priority": "2"}),
            ("07 priority bool", {"source": "mc_chat", "text": "hi",
                                  "priority": True}),
            ("08 actor not a string", {"source": "mc_chat", "text": "hi",
                                       "actor": 7}),
            ("09 empty text", {"source": "mc_chat", "text": "   "}),
            ("10 not an object", ["mc_chat", "hi"]),
            ("11 invalid level", {"source": "mc_chat", "text": "hi",
                                  "level": "warn"}),
    ):
        keep, reason = business_filter(envelope)
        check(label + " -> schema_invalid",
              keep is False and reason == REASON_SCHEMA,
              "keep=%s reason=%r" % (keep, reason))

    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="filterwall-selftest-"))
    log_path = tmpdir / "drops.log"
    log_skip(log_path, "mc_chat", "self_message", "my own line", "SuperCreek")
    stats = read_drop_stats(log_path)
    check("12 log_skip writes one line",
          stats["total"] == 1 and stats["by_reason"] == {"self_message": 1}
          and stats["by_source"] == {"mc_chat": 1},
          json.dumps(stats["by_reason"]))
    check("13 log line has at/source/reason/text_head",
          all(key in stats["recent"][0]
              for key in ("at", "source", "reason", "text_head")),
          json.dumps(stats["recent"][0], ensure_ascii=False))

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    for name, ok, detail in results:
        print("%s  %-44s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("-" * 78)
    print("%d/%d PASS, %d FAIL   (drops.log: %s)" %
          (passed, len(results), failed, log_path))
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="mowu-trigger L1 business-filter hook (library + "
                    "drops.log stats)")
    parser.add_argument("--stats", metavar="DROPS_LOG", default=None,
                        help="aggregate a client drops.log and print JSON")
    parser.add_argument("--selftest", action="store_true",
                        help="run the built-in unit tests")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.stats:
        print(json.dumps(read_drop_stats(args.stats), ensure_ascii=False,
                         indent=2))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
