import {openPrivateState} from './local-state.mjs';

export async function holdWorkspaceWriter(directory) {
  let db;
  try {
    db = await openPrivateState(directory,'dsh-workspace.writer','dsh_workspace_writer_unconfirmed');
    db.exec('BEGIN IMMEDIATE');
  } catch (error) {
    db?.close();
    if ([5,6].includes(error.errcode & 255)) throw new Error('dsh_workspace_writer_already_running');
    throw error;
  }

  let closing = false;
  const operations = new Set();
  return {
    async run(operation) {
      if (closing) throw new Error('assignment_workspace_unconfirmed');
      const work = Promise.resolve().then(operation);
      operations.add(work);
      try {
        return await work;
      } finally {
        operations.delete(work);
      }
    },
    async close() {
      closing = true;
      await Promise.allSettled(operations);
      db.close();
    },
  };
}
