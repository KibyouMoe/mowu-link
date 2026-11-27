"""Private terminal decisions for saved W05 requests."""


REJECTIONS = {"stale_assignment","question_conflict","idempotency_conflict","task_not_found",
              "invalid_question","invalid_options","invalid_option","invalid_text"}


def has_rejections(db):
    return db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='question_rejections'").fetchone()


def unsettled_clause(db):
    if not has_rejections(db):
        return ""
    return " AND question_id NOT IN (SELECT question_id FROM question_rejections)"


def rejection(db,question_id):
    if has_rejections(db):
        return db.execute("SELECT status,code FROM question_rejections WHERE question_id=?",(question_id,)).fetchone()
    return None


def record(db,question_id,status,code):
    # Keep definitive refusals separate from positive receipts. Previous releases
    # will ask Router using the same key instead of returning a refusal as success.
    db.execute("""CREATE TABLE IF NOT EXISTS question_rejections (
        question_id TEXT PRIMARY KEY,status INTEGER NOT NULL,code TEXT NOT NULL)""")
    db.execute("INSERT OR IGNORE INTO question_rejections VALUES (?,?,?)",(question_id,status,code))
