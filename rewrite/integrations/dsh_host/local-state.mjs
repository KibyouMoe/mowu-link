import {closeSync,lstatSync,openSync} from 'node:fs';
import path from 'node:path';

export async function openPrivateState(directory,name,unconfirmed) {
  const databasePath = path.join(directory,name);
  try {
    closeSync(openSync(databasePath,'wx',0o600));
  } catch (error) {
    if (error.code!=='EEXIST') throw error;
  }
  const info = lstatSync(databasePath);
  if (!info.isFile() || (process.platform!=='win32' &&
      (info.uid!==process.getuid() || (info.mode & 0o077)))) throw new Error(unconfirmed);

  const {DatabaseSync} = await import('node:sqlite');
  const db = new DatabaseSync(databasePath,{timeout:500});
  try {
    const journal = db.prepare('PRAGMA journal_mode=DELETE').get();
    if (journal.journal_mode!=='delete') throw new Error(unconfirmed);
    db.exec('PRAGMA synchronous=EXTRA; PRAGMA trusted_schema=OFF');
    return db;
  } catch (error) {
    db.close();
    throw error;
  }
}
