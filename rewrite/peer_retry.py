"""Receipt, delivery and send retries for individual peer messages."""
from .storage import Fault


SCHEMA = """
CREATE TABLE IF NOT EXISTS peer_retry (
 operation_id TEXT NOT NULL, phase TEXT NOT NULL, message_id TEXT NOT NULL,
 attempts INTEGER NOT NULL, retry_at REAL NOT NULL, failure TEXT NOT NULL,
 PRIMARY KEY(operation_id,phase)
);
CREATE INDEX IF NOT EXISTS peer_retry_message ON peer_retry(message_id);
"""


def attempt(worker, phase, operation_id, mid, operation, *args):
    try:
        if operation(*args):
            return True
        failure = "peer_host_receipt_unconfirmed"
    except Fault as err:
        # Registration and access belong to the link's recovery path.
        if err.code in ("unauthenticated","stale_session","missing_session"):
            raise
        if phase=="send" and not send_retryable(err):
            stop_sending(worker,mid,err.code)
            return False
        failure = err.code
    except OSError:
        failure = "peer_exchange_unconfirmed"
    with worker.db.transaction() as db:
        if phase=="send" and db.execute("SELECT 1 FROM peers WHERE message_id=? AND receipt IS NOT NULL",(mid,)).fetchone():
            return True
        row = db.execute("SELECT attempts FROM peer_retry WHERE operation_id=? AND phase=?",(operation_id,phase)).fetchone()
        count = 1 if row is None else row[0]+1
        delay = min(300,5*2**min(count-1,6))
        db.execute("INSERT OR REPLACE INTO peer_retry VALUES (?,?,?,?,?,?)",
                   (operation_id,phase,mid,count,worker.clock()+delay,failure))
    return False


def send_retryable(error):
    return (error.retryable or error.status>=500 or error.status in (408,429) or
            error.code=="missing_peer_receipt")


def stop_sending(worker,mid,reason):
    with worker.db.transaction() as db:
        db.execute("""UPDATE peer_retry SET phase='send_rejected',failure=?
                      WHERE operation_id=? AND phase='send'""",(reason,mid))


def waiting(worker, mid):
    with worker.db.transaction() as db:
        rows = db.execute("SELECT phase,attempts,retry_at,failure FROM peer_retry WHERE message_id=? ORDER BY phase",
                          (mid,)).fetchall()
    return [dict(r) for r in rows]
