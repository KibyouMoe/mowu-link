import {isDeepStrictEqual} from 'node:util';
import {openPrivateState} from './local-state.mjs';

function check(value) {
  if (!value) throw new Error('native_cleanup_unconfirmed');
}

export async function sqliteAdmissions(directory) {
  const db = await openPrivateState(directory,'dsh-admissions.sqlite3','native_cleanup_unconfirmed');
  try {
    db.exec(`CREATE TABLE IF NOT EXISTS admissions (
        native_id TEXT PRIMARY KEY,
        envelope TEXT NOT NULL CHECK(length(envelope) BETWEEN 1 AND 4096)
      ) STRICT, WITHOUT ROWID`);
    const find = db.prepare('SELECT envelope FROM admissions WHERE native_id=?');
    const insert = db.prepare('INSERT INTO admissions VALUES (?,?) ON CONFLICT(native_id) DO NOTHING');
    function read(id) {
      const row = find.get(id);
      check(row);
      return JSON.parse(row.envelope);
    }
    return {
      close: () => db.close(),
      read,
      save(entry) {
        if (insert.run(entry.native_id,JSON.stringify(entry)).changes===0) {
          check(isDeepStrictEqual(read(entry.native_id),entry));
        }
      },
    };
  } catch (error) {
    db.close();
    throw error;
  }
}
