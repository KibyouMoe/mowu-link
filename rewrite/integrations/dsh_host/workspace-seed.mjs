import {createHash} from 'node:crypto';
import {constants} from 'node:fs';
import {open} from 'node:fs/promises';
import path from 'node:path';

const MAX_SEED_BYTES = 24 * 1024 * 1024;

// Seed extraction runs as the container user before any model command is admitted.
export const seedProject = String.raw`
import fcntl
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile

limit, expected = int(sys.argv[1]), sys.argv[2]
raw = sys.stdin.buffer.read(limit + 1)
if len(raw) > limit or hashlib.sha256(raw).hexdigest() != expected:
    raise ValueError("invalid seed digest")

root = Path("/workspace")
target = root / "project"
partial = root / ".mowulink-seeding"
with (root / ".mowulink-seed-lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    # Only this initializer writes the target while the host ledger is seeding.
    # A previous import might have finished even though the CLI never acknowledged it.
    if target.is_dir():
        sys.exit(0)
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(mode=0o700)
    seen = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for entry in archive:
            name = entry.name
            parts = name.split("/")
            if (len(seen) >= 4096 or name in seen or not 0 < len(name.encode("utf-8")) <= 1024
                or any(not part or part in {".", "..", ".git"} for part in parts)
                or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in name)
                or not (entry.isdir() or entry.type in {tarfile.REGTYPE, tarfile.AREGTYPE})):
                raise ValueError("invalid seed entry")
            seen.add(name)
            destination = partial / PurePosixPath(name)
            if entry.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            total += entry.size
            if entry.size < 0 or total > limit:
                raise ValueError("seed too large")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.extractfile(entry) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output, length=128 * 1024)
            destination.chmod(0o700 if entry.mode & 0o111 else 0o600)
    if not seen:
        raise ValueError("empty seed")
    os.rename(partial, target)
`;

export function workspaceSeed(config,files=null) {
  if (config===undefined) return null;
  if (!config || typeof config.archive!=='string' || !path.isAbsolute(config.archive)
      || typeof config.sha256!=='string' || !/^[a-f0-9]{64}$/.test(config.sha256)
      || Object.keys(config).length!==2) throw new Error('invalid_workspace_seed');
  return {sha256:config.sha256,async bytes() {
    let file;
    try {
      if (files) {
        const data = await files.seed(config.archive);
        if (createHash('sha256').update(data).digest('hex')!==config.sha256) throw new Error();
        return data;
      }
      file = await open(config.archive,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
      const info = await file.stat();
      if (!info.isFile() || info.uid!==process.getuid() || info.mode&0o077 ||
          info.size<=0 || info.size>MAX_SEED_BYTES) throw new Error();
      const data = Buffer.alloc(info.size);
      let position = 0;
      while (position<data.length) {
        const {bytesRead} = await file.read(data,position,data.length-position,position);
        if (!bytesRead) throw new Error();
        position += bytesRead;
      }
      if ((await file.stat()).size!==info.size ||
          createHash('sha256').update(data).digest('hex')!==config.sha256) throw new Error();
      return data;
    } catch {
      throw new Error('workspace_seed_unconfirmed');
    } finally {
      await file?.close();
    }
  }};
}

export const maximumSeedBytes = MAX_SEED_BYTES;
