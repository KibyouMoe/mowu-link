"""Offline cross-checks for a Worker database joined to its Feishu ledger."""
import json
import re

from . import feishu_receipt_review
from .feishu_output import write_key


def document(raw):
    try:
        return json.loads(raw)
    except (TypeError,ValueError):
        return None


def message_id(value):
    return isinstance(value,str) and re.fullmatch(r"om_[A-Za-z0-9_-]{1,197}",value) is not None


def input_proof(db):
    confirmed = waiting = 0
    issues = []
    rows = db.execute("""SELECT e.mid,e.state,e.receipt,c.receipt AS native_receipt,
                        k.receipt AS command_receipt,h.message_id AS chat_id,c.session,
                        k.message_id AS command_id,d.seq AS command_seq
        FROM feishu.platform_events e LEFT JOIN main.chats h ON h.message_id=e.mid
        LEFT JOIN main.chat_deliveries c ON c.message_id=h.message_id
        LEFT JOIN main.platform_commands k ON k.message_id=e.mid
        LEFT JOIN main.display_events d ON d.event_id='command-'||e.mid ORDER BY e.rowid""")
    for row in rows:
        mid = row["mid"]
        saved = document(row["receipt"])
        native = document(row["native_receipt"])
        command = document(row["command_receipt"])
        if isinstance(native,dict) and row["command_id"] is None:
            native_ok = (native.get("delivery_id")=="chat-"+mid and native.get("session")==row["session"]
                         and bool(row["session"]) and isinstance(native.get("native_id"),str)
                         and bool(native["native_id"]))
        else:
            native_ok = False
        command_ok = (row["chat_id"] is None and isinstance(command,dict) and command.get("message_id")==mid
                      and isinstance(command.get("output"),dict) and row["command_seq"] is not None
                      and command["output"]=={"event_id":"command-"+mid,"seq":row["command_seq"]})
        worker_saved = row["native_receipt"] is not None or row["command_receipt"] is not None
        if row["state"]=="received" and (native_ok and native==saved or command_ok and command==saved):
            confirmed += 1
        elif row["state"]=="pending" and row["receipt"] is None and (not worker_saved or native_ok or command_ok):
            waiting += int(native_ok or command_ok)
        elif row["state"]!="rejected" or any(row[key] is not None for key in ("receipt","native_receipt","command_receipt")):
            issues.append({"code":"platform_input_receipt_mismatch","message_id":mid})
    return confirmed,waiting,issues


def valid_write(request,event,chat,receipt,image_key,patch_target):
    if not isinstance(event,dict) or not isinstance(event.get("event_id"),str) or not isinstance(request,dict):
        return False
    if request.get("key")!=write_key(chat,event["event_id"]) or not isinstance(request.get("body"),dict):
        return False
    kind,method,target = event.get("kind"),request.get("method"),request.get("target")
    if kind=="image":
        return (method=="image" and target==chat and isinstance(image_key,str) and image_key.startswith("img_")
                and request["body"]=={"image_key":image_key})
    if patch_target:
        return method=="patch" and target==patch_target and receipt in (None,target)
    if kind in ("question_answered","question_closed"):
        return False
    if method!="create" or target!=chat:
        return False
    if kind=="final":
        try:
            return request["body"]["elements"][0]["text"]["content"]==event.get("text")
        except (KeyError,IndexError,TypeError):
            return False
    return True


def output_proof(db,chat,cursor):
    settled = waiting = 0
    issues = []
    segments,questions = {},{}
    review_table = feishu_receipt_review.present(db,"feishu")
    review_fields = (",h.root_seq,h.scope,h.held_at" if review_table
                     else ",NULL AS root_seq,NULL AS scope,NULL AS held_at")
    review_join = " LEFT JOIN feishu.view_receipt_review h USING(seq)" if review_table else ""
    roots = {}
    rows = db.execute(f"""SELECT a.seq,a.event,a.mid,w.request,w.platform_mid,w.prepared_at,i.image_key{review_fields}
        FROM feishu.view_attempt a LEFT JOIN feishu.view_writes w USING(seq)
        LEFT JOIN feishu.view_images i USING(seq){review_join} ORDER BY a.seq""")
    for row in rows:
        seq,mid = row["seq"],row["platform_mid"]
        event = document(row["event"])
        event = event if isinstance(event,dict) else {}
        kind = event.get("kind")
        turn = (event.get("session",""),event.get("turn_id",""))
        question = (event.get("recipient",""),event.get("question_id",""))
        if not all(isinstance(part,str) for part in (*turn,*question)):
            issues.append({"code":"platform_output_receipt_mismatch","display_seq":seq})
            continue
        patch_target = None
        if kind=="progress":
            patch_target = segments.get(turn)
        elif kind in ("question_answered","question_closed"):
            patch_target = questions.get(question)
        prepared,request = row["prepared_at"],row["request"]
        root = row["root_seq"]
        if root is not None:
            key = feishu_receipt_review.scope(event)
            review_time = row["held_at"]
            recorded = (seq<=cursor and row["mid"] is None and mid is None and row["scope"]==key
                        and type(review_time) in (int,float))
            if root==seq:
                raw = document(request)
                recorded = (recorded and isinstance(raw,dict) and raw.get("method") in ("create","image")
                            and valid_write(raw,event,chat,None,row["image_key"],patch_target)
                            and type(prepared) in (int,float)
                            and review_time-prepared>=feishu_receipt_review.RETRY_SECONDS)
                if recorded:
                    roots[seq] = key
            else:
                original = roots.get(root)
                recorded = (recorded and key is not None and original==key
                            and request is None and prepared is None)
            issues.append({"code":"platform_receipt_held" if recorded else "platform_output_receipt_mismatch",
                           "display_seq":seq,"root_seq":root})
            continue
        bad_receipt = mid is not None and not message_id(mid)
        bad_commit = seq<=cursor and (mid is None or mid!=row["mid"])
        if request is None:
            bad_request = mid is not None or prepared is not None
        else:
            bad_request = (type(prepared) not in (int,float)
                           or not valid_write(document(request),event,chat,mid,row["image_key"],patch_target))
        if bad_receipt or bad_commit or bad_request:
            issues.append({"code":"platform_output_receipt_mismatch","display_seq":seq})
        elif mid is not None:
            settled += int(seq<=cursor)
            waiting += int(seq>cursor)
        if seq<=cursor:
            # These are the same projections used when preparing the next card.
            if kind=="progress":
                segments[turn] = row["mid"]
            else:
                segments.pop(turn,None)
            if kind=="question_open":
                questions[question] = row["mid"]
    for row in db.execute("""SELECT w.seq FROM feishu.view_writes w
        LEFT JOIN feishu.view_attempt a USING(seq) WHERE a.seq IS NULL ORDER BY w.seq"""):
        issues.append({"code":"platform_output_receipt_mismatch","display_seq":row["seq"]})
    if review_table:
        for (seq,) in db.execute("""SELECT h.seq FROM feishu.view_receipt_review h
            LEFT JOIN feishu.view_attempt a USING(seq) WHERE a.seq IS NULL ORDER BY h.seq"""):
            issues.append({"code":"platform_output_receipt_mismatch","display_seq":seq})
    return settled,waiting,issues


def menu_proof(db,cursor):
    issues = []
    for table,flag in (("platform_model_menu","model_menu"),("resume_catalog_cards","session_menu")):
        rows = db.execute(f"""SELECT m.display_seq,m.platform_mid,w.platform_mid AS provider_mid,
                                   CASE WHEN json_valid(e.payload) THEN json_extract(e.payload,'$.{flag}') END AS flagged
            FROM main.{table} m
            LEFT JOIN feishu.view_writes w ON w.seq=m.display_seq
            LEFT JOIN main.display_events e ON e.seq=m.display_seq ORDER BY m.display_seq""")
        for row in rows:
            seq,mid,provider = row["display_seq"],row["platform_mid"],row["provider_mid"]
            if row["flagged"]!=1 or mid!=provider and (mid is not None or seq<=cursor):
                issues.append({"code":"platform_worker_menu_receipt_mismatch","display_seq":seq})
    return issues


def reconcile(db):
    tables = {row[0] for row in db.execute("SELECT name FROM feishu.sqlite_schema WHERE type='table'")}
    worker_tables = {row[0] for row in db.execute("SELECT name FROM main.sqlite_schema WHERE type='table'")}
    if (not {"view_writes","view_images"}<=tables
            or not {"chat_deliveries","platform_commands","platform_model_menu","resume_catalog_cards"}<=worker_tables):
        return {"issues":[{"code":"platform_receipt_ledger_unconfirmed"}]}
    views = db.execute("SELECT chat,cursor FROM feishu.view_cursor WHERE slot=1").fetchone()
    bindings = db.execute("SELECT slot,value FROM feishu.binding LIMIT 2").fetchall()
    binding = document(bindings[0]["value"]) if len(bindings)==1 and bindings[0]["slot"]==1 else None
    chat,cursor = views["chat"],views["cursor"]
    if not isinstance(binding,list) or len(binding)!=3 or any(not isinstance(value,str) or not value for value in binding) or binding[2]!=chat:
        return {"issues":[{"code":"platform_receipt_binding_mismatch"}]}
    inputs,input_waiting,input_issues = input_proof(db)
    outputs,output_waiting,output_issues = output_proof(db,chat,cursor)
    return {"confirmed_inputs":inputs,"worker_inputs_awaiting_platform":input_waiting,
            "confirmed_outputs":outputs,"provider_outputs_awaiting_commit":output_waiting,
            "issues":input_issues+output_issues+menu_proof(db,cursor)}
