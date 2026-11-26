import {closeSync, lstatSync, openSync} from 'node:fs';
import path from 'node:path';
import {isDeepStrictEqual} from 'node:util';

const filename = 'dsh-admissions.sqlite3';

function check(value) {
  if (!value) throw new Error('native_cleanup_unconfirmed');
}

export async function sqliteAdmissions(directory) {
  const databasePath = path.join(directory,filename);
  try {
    closeSync(openSync(databasePath,'wx',0o600));
  } catch (error) {
    if (error.code!=='EEXIST') throw error;
  }
  const info = lstatSync(databasePath);
  check(info.isFile() && (process.platform==='win32' ||
    info.uid===process.getuid() && !(info.mode & 0o077)));

  const {DatabaseSync} = await import('node:sqlite');
  const db = new DatabaseSync(databasePath,{timeout:500});
  try {
    const journal = db.prepare('PRAGMA journal_mode=DELETE').get();
    check(journal.journal_mode==='delete');
    db.exec(`PRAGMA synchronous=EXTRA;
      PRAGMA trusted_schema=OFF;
      CREATE TABLE IF NOT EXISTS admissions (
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
