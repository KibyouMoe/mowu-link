"""Send already authenticated assignment bytes to Worker over its existing media routes."""
import hashlib

from . import peer_files
from .messages import valid_guid
from .storage import encode, require


def verify_state(worker,scope):
    result = worker.request("POST","worker/workspace-state",scope)
    require(isinstance(result,dict),409,"assignment_workspace_unconfirmed")
    state = result.get("state")
    require(state in ("active","inactive","released") and result=={**scope,"state":state},
            409,"assignment_workspace_unconfirmed")
    return state


def stage(worker,scope,turn,call,data,metadata,*,key_prefix):
    require(verify_state(worker,scope)=="active",409,"assignment_workspace_inactive")
    meta = peer_files.metadata(metadata)
    size = meta["size_bytes"]
    require(len(data)==size and hashlib.sha256(data).hexdigest()==meta["sha256"],422,"digest_mismatch")
    key = key_prefix+hashlib.sha256(encode([scope,meta]).encode()).hexdigest()
    result = worker.request("POST","worker/media/uploads",meta,key)
    route = "worker/media/uploads/"+result["upload_id"]
    while not result["complete"]:
        offset = result["next_offset"]
        require(type(offset) is int and 0<=offset<size,409,"invalid_upload_offset")
        chunk = data[offset:offset+peer_files.CHUNK]
        result = worker.request("PATCH",route,chunk,headers={"Upload-Offset":str(offset)})
        require(result["next_offset"]==offset+len(chunk),409,"invalid_upload_ack")
    require(result["next_offset"]==size and valid_guid(result.get("attachment_id")),409,"unconfirmed_workspace_upload")
    return worker.request("POST","worker/native-tool",{"session":scope["session"],"turn_id":turn,"call_id":call,
        "tool":"worker_stage_artifacts","arguments":{"attachment_ids":[result["attachment_id"]]}})
