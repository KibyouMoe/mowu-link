import {createHash} from 'node:crypto';
import {constants} from 'node:fs';
import {lstat, open} from 'node:fs/promises';
import path from 'node:path';

function requireValue(value, code) {
  if (!value) throw new Error(code);
}

function mediaType(bytes) {
  if (bytes.subarray(0,8).equals(Buffer.from('89504e470d0a1a0a','hex'))) return 'image/png';
  if (bytes.subarray(0,3).equals(Buffer.from('ffd8ff','hex'))) return 'image/jpeg';
  if (['GIF87a','GIF89a'].includes(bytes.toString('ascii',0,6))) return 'image/gif';
  if (bytes.toString('ascii',0,4)==='RIFF' && bytes.toString('ascii',8,12)==='WEBP') return 'image/webp';
  return null;
}

export async function admitMedia(ctx, agent, directory, items, content, forDelivery) {
  const store = ctx.get('attachments');
  requireValue(store && Array.isArray(items) && items.length <= 10, 'dsh_attachments_unavailable');
  const cache = path.join(directory,'media');
  const info = await lstat(cache);
  requireValue(info.isDirectory() && info.uid === process.getuid() && !(info.mode & 0o077),'invalid_media_cache');
  let total = 0;
  const parts = [...content];
  for (const {sha256, size_bytes, name, kind} of items) {
    total += size_bytes;
    requireValue(typeof sha256==='string' && /^[0-9a-f]{64}$/.test(sha256)
      && Number.isSafeInteger(size_bytes) && size_bytes>=0 && total<=100000000
      && typeof name==='string' && name.length && !/[\/\\\x00-\x1f]/u.test(name)
      && name!=='.' && name!=='..' && [undefined,'image','file'].includes(kind),'invalid_media_metadata');
    const file = await open(path.join(cache,sha256),constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    let data;
    try {
      const stat = await file.stat();
      requireValue(stat.isFile() && stat.size===size_bytes && stat.uid===process.getuid() && !(stat.mode & 0o077),
                   'invalid_media_object');
      data = await file.readFile();
    } finally {
      await file.close();
    }
    requireValue(data.length===size_bytes && createHash('sha256').update(data).digest('hex')===sha256,
                 'media_digest_mismatch');
    const type = kind==='file' ? null : mediaType(data);
    requireValue(kind!=='image' || type,'invalid_image_format');
    if (type) {
      requireValue(size_bytes<=10*1024*1024,'image_too_large');
      parts.push({type:'image',mediaType:type,data:data.toString('base64'),name});
    } else {
      parts.push({type:'file',attachment:await store.saveFile({data,name})});
    }
  }
  if (forDelivery && parts.some(p=>p.type==='image')) {
    const current = agent.model.current();
    if (current !== undefined) {
      const model = await ctx.get('llm').resolveModelInfo(current.provider,current.model);
      requireValue(model.inputModalities===undefined || model.inputModalities.includes('image'),
                   'model_does_not_support_images');
    }
  }
  try {
    return await store.admitPromptContent(parts);
  } catch (error) {
    if (typeof error.code==='string' && /^[A-Z][A-Z0-9_]{1,70}$/.test(error.code)) {
      throw new Error('dsh_'+error.code.toLowerCase(),{cause:error});
    }
    throw error;
  }
}
