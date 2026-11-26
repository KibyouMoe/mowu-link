"""Freeze the native directory for paging and select only from confirmed platform cards."""
import json
import re
import unicodedata
from urllib.error import URLError

from .storage import Fault, encode, require, string


SCHEMA = """
CREATE TABLE IF NOT EXISTS resume_catalog(
    session TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL UNIQUE, query TEXT NOT NULL, page INTEGER NOT NULL,
    entries TEXT NOT NULL, unavailable INTEGER NOT NULL, more INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resume_catalog_cards(
    display_seq INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL, choices TEXT NOT NULL, platform_mid TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS cards_by_resume_snapshot ON resume_catalog_cards(snapshot_id,display_seq);
"""
BROWSE = {"search","page","next","prev"}
NUMBER = re.compile(r"[0-9]{1,9}")


def numbered_selection(text):
    words = text.strip().split()
    return len(words)==2 and words[0]=="/resume" and NUMBER.fullmatch(words[1]) is not None


def read(service,session):
    targets = service.worker.resume_targets
    result = service.worker.adapter.session_catalog() if service.host_session_catalog else {
        "session":session,"items":[],"unavailable":0}
    require(isinstance(result,dict) and result.get("session")==session,409,"wrong_native_session_catalog")
    items,unavailable,more = result.get("items"),result.get("unavailable"),result.get("more",False)
    require(isinstance(items,list) and len(items)<=200 and type(unavailable) is int and 0<=unavailable<=200
            and type(more) is bool,502,"invalid_session_catalog")
    entries,seen = [],set()
    for row in items:
        require(isinstance(row,dict),502,"invalid_session_catalog")
        sid,title = row.get("id"),row.get("title")
        require(isinstance(sid,str) and 0<len(sid)<=200 and sid.isprintable() and sid not in seen
                and isinstance(title,str) and len(title)<=160
                and all(unicodedata.category(ch) not in ("Cc","Cs","Zl","Zp") for ch in title),502,"invalid_session_catalog")
        seen.add(sid)
        entries.append((sid,title))
    entries.extend((sid,"") for sid in targets if sid not in seen)
    return entries,unavailable,more


def render(entries,session,query,page,unavailable,more,targets):
    pages = max(1,(len(entries)+9)//10)
    if not 1<=page<=pages:
        return f"当前目录共 {pages} 页。",None
    lines = [f"会话目录 · 第 {page}/{pages} 页"]
    if query:
        lines.append("搜索 "+query)
    if not entries:
        lines.append("目录中没有符合条件的会话。")
    offset = (page-1)*10
    choices = {}
    for index,(sid,title) in enumerate(entries[offset:offset+10],offset if targets else offset+1):
        marker = "（当前绑定）" if sid==session else ""
        if targets and sid not in targets:
            marker = "（仅可浏览）"
        label = title or ("已配置会话" if sid in targets else "未命名会话")
        lines.append(f"{index}. {label}{marker}\n{sid}")
        choices[str(index)] = sid
    if unavailable:
        lines.append(f"{unavailable} 个会话的记录暂不可读。")
    if more:
        lines.append("检索范围限宿主返回的前 200 个会话及已配置的会话。")
    lines.append("用 /resume 编号、完整标题或会话 ID 切换。翻页保留这次目录的编号。" if targets else "切换会话敬请期待。")
    return "\n".join(lines),choices


def browse(service,mid,text,args):
    from .worker_commands import previous, record

    w,session = service.worker,service.host_session
    with w.db.transaction() as db:
        old = previous(db,mid,text)
        if old:
            return old
        saved = db.execute("SELECT * FROM resume_catalog WHERE session=?",(session,)).fetchone()

    operation = args[0].casefold() if args else ""
    if operation not in ("","search") and not saved:
        with w.db.transaction() as db:
            return record(db,mid,text,"/resume",session,"请先用 /resume 查看会话目录。")
    try:
        if operation in ("","search"):
            query = " ".join(args[1:])
            require(operation=="" or query and len(query)<=100 and query.isprintable(),422,"invalid_catalog_search")
            entries,unavailable,more = read(service,session)
            entries = [row for row in entries if query.casefold() in (" ".join(row)).casefold()]
            snapshot,page = mid,1
        else:
            snapshot,query = saved["snapshot_id"],saved["query"]
            entries,unavailable,more = json.loads(saved["entries"]),saved["unavailable"],bool(saved["more"])
            require((operation=="page" and len(args)==2 and re.fullmatch(r"[1-9][0-9]?",args[1]))
                    or (operation in ("next","prev") and len(args)==1),422,"invalid_catalog_page")
            page = int(args[1]) if operation=="page" else saved["page"]+(1 if operation=="next" else -1)
        answer,choices = render(entries,session,query,page,unavailable,more,w.resume_targets)
    except (Fault,URLError,OSError) as error:
        messages = {"invalid_catalog_search":"用法：/resume search 关键词","invalid_catalog_page":"用法：/resume page 页码"}
        answer = messages.get(getattr(error,"code",None),"会话目录暂时不可读。请稍后重试。")
        choices = None
    with w.db.transaction() as db:
        if choices is not None:
            db.execute("INSERT OR REPLACE INTO resume_catalog VALUES (?,?,?,?,?,?,?)",
                       (session,snapshot,query,page,encode(entries),unavailable,more))
        receipt = record(db,mid,text,"/resume",session,answer,session_menu=choices is not None and bool(w.resume_targets))
        if choices is not None and w.resume_targets:
            db.execute("INSERT INTO resume_catalog_cards VALUES (?,?,?,NULL)",
                       (receipt["output"]["seq"],snapshot,encode(choices)))
        return receipt


def acknowledge(worker,body):
    seq = body.get("seq")
    require(set(body)=={"seq","platform_message_id"} and type(seq) is int and 0<seq<2**63,422,"invalid_session_menu_receipt")
    mid = string(body,"platform_message_id")
    require(re.fullmatch(r"om_[A-Za-z0-9_-]{1,197}",mid),422,"invalid_session_menu_receipt")
    with worker.db.transaction() as db:
        row = db.execute("SELECT platform_mid FROM resume_catalog_cards WHERE display_seq=?",(seq,)).fetchone()
        require(row,404,"session_menu_not_found")
        other = db.execute("SELECT display_seq FROM resume_catalog_cards WHERE platform_mid=?",(mid,)).fetchone()
        require(row[0] in (None,mid) and (not other or other[0]==seq),409,"session_menu_receipt_conflict")
        db.execute("UPDATE resume_catalog_cards SET platform_mid=? WHERE display_seq=?",(mid,seq))
    return {"recorded":True,"seq":seq}


def select_number(db,session,number,reply_to):
    saved = db.execute("SELECT snapshot_id FROM resume_catalog WHERE session=?",(session,)).fetchone()
    if not saved:
        return None,"请先用 /resume 查看当前会话目录。"
    cards = db.execute("""SELECT platform_mid,choices FROM resume_catalog_cards
        WHERE snapshot_id=? ORDER BY display_seq DESC""",(saved[0],)).fetchall()
    if reply_to:
        cards = [card for card in cards if card["platform_mid"]==reply_to]
        if not cards:
            return None,"这张会话目录卡已失效。请用 /resume 刷新后再选。"
    if not cards or not cards[0]["platform_mid"]:
        return None,"会话目录尚未确认送达。请等目录卡送达后再选。"
    for card in cards:
        target = json.loads(card["choices"]).get(str(int(number)))
        if card["platform_mid"] and target:
            return target,None
    return None,"当前目录已显示的页面中没有该编号。请翻到对应页后再选。"


def exact_title(service,session,name):
    entries,_unavailable,_more = read(service,session)
    matches = [sid for sid,title in entries if title and title==name]
    if len(matches)==1:
        return matches[0],None
    if matches:
        return None,"有同名会话。请使用目录中的会话 ID。"
    return None,"这个会话暂未配置切换权限。可用 /resume search 搜索。"
