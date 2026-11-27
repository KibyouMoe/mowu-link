"""Index proven Feishu chat in the platform ledger, next to its original receipts."""
import json


SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_arrivals (
 mid TEXT PRIMARY KEY, seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_confirmed_finals (
 seq INTEGER PRIMARY KEY, mid TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL, seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_finals_by_turn ON chat_confirmed_finals(session,turn);
CREATE TABLE IF NOT EXISTS chat_history_index (
 id INTEGER PRIMARY KEY, direction TEXT NOT NULL CHECK(direction IN ('in','out')), mid TEXT NOT NULL, source_seq INTEGER,
 session TEXT NOT NULL, turn TEXT, seen_at REAL NOT NULL, UNIQUE(direction,mid)
);
CREATE INDEX IF NOT EXISTS chat_history_turn ON chat_history_index(direction,session,turn);
"""


def arrived(db,mid,seen_at):
    db.execute("INSERT INTO chat_arrivals VALUES (?,?)",(mid,seen_at))


def correlate(db,session,turn):
    db.execute("""INSERT OR IGNORE INTO chat_history_index(direction,mid,source_seq,session,turn,seen_at)
        SELECT 'out',f.mid,f.seq,f.session,f.turn,f.seen_at FROM chat_confirmed_finals f
        WHERE f.session=? AND f.turn=? AND EXISTS (SELECT 1 FROM chat_history_index h
             WHERE h.direction='in' AND h.session=f.session AND h.turn=f.turn) ORDER BY f.seq""",(session,turn))


def receipt_turn(receipt):
    value = receipt.get("turn_id")
    return value if isinstance(value,str) and value else None


def received(db,mid,receipt):
    session,turn = receipt.get("session"),receipt_turn(receipt)
    if receipt.get("delivery_id")!="chat-"+mid or not isinstance(session,str) or not session:
        return
    db.execute("""INSERT OR IGNORE INTO chat_history_index(direction,mid,session,turn,seen_at)
        SELECT 'in',mid,?,?,seen_at FROM chat_arrivals WHERE mid=?""",(session,turn,mid))
    if turn:
        correlate(db,session,turn)


def final_confirmed(db,seq,event,mid,seen_at):
    if (event.get("kind")!="final" or not all(isinstance(event.get(key),str) and event[key]
                                             for key in ("session","turn_id","text"))):
        return
    session,turn = event["session"],event["turn_id"]
    db.execute("INSERT INTO chat_confirmed_finals VALUES (?,?,?,?,?)",(seq,mid,session,turn,seen_at))
    correlate(db,session,turn)


def export_rows(db,worker,private_turns):
    from .feishu_inbound import parts

    for row in db.execute("""SELECT h.*,p.body,p.receipt,p.state,v.event,v.mid AS committed_mid,w.platform_mid,w.request
        FROM chat_history_index h
        LEFT JOIN platform_events p ON h.direction='in' AND p.mid=h.mid
        LEFT JOIN view_attempt v ON h.direction='out' AND v.seq=h.source_seq
        LEFT JOIN view_writes w ON w.seq=v.seq ORDER BY h.id"""):
        mid,session,turn = row["mid"],row["session"],row["turn"]
        if row["direction"]=="in":
            receipt = json.loads(row["receipt"] or "{}")
            if not (row["state"]=="received" and receipt.get("delivery_id")=="chat-"+mid
                    and receipt.get("session")==session and receipt_turn(receipt)==turn):
                raise ValueError("Feishu chat history no longer matches the original receipt")
            body = json.loads(row["body"])
            text,_ = parts(body["kind"],json.loads(body["content"]))
        else:
            if (session,turn) not in private_turns:
                continue
            event = json.loads(row["event"] or "{}")
            request = json.loads(row["request"] or "{}")
            if not (row["platform_mid"]==mid and row["committed_mid"] in (None,mid)
                    and (event.get("kind"),event.get("session"),event.get("turn_id"))==("final",session,turn)
                    and isinstance(event.get("text"),str) and request.get("method")=="create"):
                raise ValueError("Feishu reply no longer matches the original platform receipt")
            text = request["body"]["elements"][0]["text"]["content"]
            if text!=event["text"]:
                raise ValueError("Feishu reply differs from its recorded card")
        yield row["id"],worker,row["direction"],text,session,row["seen_at"],mid

def private_turns(db,agent_paths):
    from contextlib import closing

    from .cutover import readonly

    inputs = {(row["session"],row["turn"],row["mid"]):row["receipt"] for row in db.execute("""
        SELECT h.session,h.turn,h.mid,p.receipt FROM chat_history_index h JOIN platform_events p USING(mid)
        WHERE h.direction='in' AND h.turn IS NOT NULL""")}
    wanted = {(session,turn) for session,turn,_mid in inputs}
    proof_count = {}
    eligible = set()
    for path in agent_paths:
        with closing(readonly(path)) as agent:
            for row in agent.execute("SELECT session,delivery_id,payload,receipt FROM intents WHERE receipt IS NOT NULL"):
                receipt = json.loads(row["receipt"])
                session,turn = row["session"],receipt.get("turn_id")
                key = (session,turn)
                if key not in wanted:
                    continue
                proof_count[key] = proof_count.get(key,0)+1
                payload = json.loads(row["payload"])
                mid = payload.get("message_id")
                if (payload.get("kind")=="chat" and isinstance(mid,str) and row["delivery_id"]=="chat-"+mid
                        and row["receipt"]==inputs.get((session,turn,mid))):
                    eligible.add(key)
    return {key for key in eligible if proof_count[key]==1}


def prepare(snapshot,manifest_sha256,destination,worker):
    from contextlib import closing

    from .cutover import digest, output_directory, readonly
    from .legacy_chat import NAME, check_destination, write_archive
    from .state_bundle import agent_ledger_paths, filename, verify
    from .storage import encode

    check_destination(destination,worker)
    manifest = verify(snapshot,manifest_sha256)
    label = "feishu:"+worker
    if label not in manifest["files"]:
        raise ValueError("no Feishu state belongs to the selected Worker")
    path = snapshot/filename(label)
    sha = manifest["files"][label]["sha256"]
    agents = dict(agent_ledger_paths(snapshot,manifest))[worker]
    before = {item:digest(item) for item in agents}
    with output_directory(destination) as stage, closing(readonly(path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        indexed = "chat_history_index" in tables
        turns = private_turns(db,agents) if indexed else set()
        rows = export_rows(db,worker,turns) if indexed else ()
        result = write_archive(stage,worker,sha,rows)
        with closing(readonly(stage/NAME)) as archive:
            indexed_inputs = archive.execute("SELECT COUNT(*) FROM messages WHERE direction='in'").fetchone()[0]
        indexed_outputs = result["count"]-indexed_inputs
        received_chats = db.execute("""SELECT COUNT(*) FROM platform_events
            WHERE state='received' AND json_extract(receipt,'$.delivery_id')='chat-'||mid""").fetchone()[0]
        if "view_writes" in tables:
            confirmed_finals = db.execute("""SELECT COUNT(*) FROM view_attempt a LEFT JOIN view_writes w USING(seq)
                WHERE coalesce(w.platform_mid,a.mid) IS NOT NULL AND json_extract(a.event,'$.kind')='final'""").fetchone()[0]
        else:
            confirmed_finals = db.execute("""SELECT COUNT(*) FROM view_attempt WHERE mid IS NOT NULL
                AND json_extract(event,'$.kind')='final'""").fetchone()[0]
        result.update(source="feishu",bundle_manifest_sha256=manifest_sha256,
                      inbound=indexed_inputs,outbound=indexed_outputs,
                      inbound_without_index=received_chats-indexed_inputs,
                      finals_without_chat_proof=confirmed_finals-indexed_outputs)
        if digest(path)!=sha or any(digest(item)!=expected for item,expected in before.items()):
            raise ValueError("snapshot changed during Feishu conversation export")
        (stage/"manifest.json").write_text(encode(result),encoding="utf-8")
    return result
