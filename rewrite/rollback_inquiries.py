"""Verify progress and cancellation retries against the original Worker observations."""
from .storage import timestamp


def source(latest_query, actor, tid, aid):
    if actor not in latest_query:
        raise ValueError("inquiry was not preceded by a worker status query")
    return latest_query[actor],actor,tid,aid


def bind(delivery_id, sources, samples, acknowledged):
    query_id,actor,tid,aid = sources[delivery_id]
    observation = samples.get(query_id)
    if observation:
        body = observation["body"]
        if (observation["actor"]==actor and body.get("busy") is False
                and body.get("held_task_id")==tid and body.get("held_assignment_id")==aid
                and set(observation["reply"])=={"observed_at"}
                and isinstance(observation["reply"]["observed_at"],str)
                and observation["seq"]<acknowledged.get(delivery_id,0)):
            return observation
    raise ValueError("inquiry is missing its original idle worker observation")


def verify_cancel_attempt(attempt, sources, samples, acknowledged, *, before=None):
    initial,*retries = attempt["delivery_ids"]
    position = attempt["action_seq"]
    if acknowledged.get(initial,0)<=position:
        raise ValueError("cancellation has no original delivery receipt")
    retry_proofs = []
    for did in retries:
        sample = bind(did,sources,samples,acknowledged)
        if sample["seq"]<=position or before is not None and sample["seq"]>=before:
            raise ValueError("cancellation retry is outside its control attempt")
        position = sample["seq"]
        retry_proofs.append({"delivery_id":did,"presence_key":sample["key"]})
    attempt["retries"] = retry_proofs
    return max(position,attempt.get("working_end_seq",0))


def verify_cleanup_receipt(cleanup, acknowledged, *, after=0):
    successor = cleanup["delivery_ids"][0] if cleanup["kind"]=="task.cancel" else cleanup["delivery_id"]
    if not after<cleanup["action_seq"]<acknowledged.get(successor,0)<cleanup["report_seq"]:
        raise ValueError("the last cleanup lacks its own receipt")


def verify_cancellations(step, sources, samples, acknowledged):
    earlier = step.get("superseded_cancels",[])
    expired = step.get("expired_cancel")
    cleanup = step["cleanup"]
    for attempt, successor in zip(earlier,[*earlier[1:],expired or cleanup]):
        verify_cancel_attempt(attempt,sources,samples,acknowledged,before=successor["action_seq"])
        attempt["superseded_by"] = successor["action_key"]
    if cleanup["kind"]=="task.cancel":
        verify_cancel_attempt(cleanup,sources,samples,acknowledged,before=cleanup["report_seq"])
    if not expired:
        verify_cleanup_receipt(cleanup,acknowledged)
        return
    prior = verify_cancel_attempt(expired,sources,samples,acknowledged,before=expired["pool_action_seq"])
    if shelved := step.get("shelved_delivery_id"):
        shelve = bind(shelved,sources,samples,acknowledged)
        if (not prior<shelve["seq"]<expired["pool_action_seq"]
                or shelve["reply"]!={"observed_at":timestamp(step["pool"]["entered"])}):
            raise ValueError("exhausted cancellation was not shelved by its original observation")
        prior = shelve["seq"]
        step["shelved_presence_key"] = shelve["key"]
    else:
        # An offline timeout has no shelving delivery. Require the first cycle's
        # receipts before any pooled action can be used to explain the next cycle.
        last_receipt = max(acknowledged[did] for did in expired["delivery_ids"])
        prior = max(prior,last_receipt)
        if prior>=expired["pool_action_seq"]:
            raise ValueError("offline cancellation receipt is ambiguous after pooling")
    verify_cleanup_receipt(cleanup,acknowledged,after=prior)


def verify_rounds(entries, sources, samples, continued, windows, acknowledged):
    for tid,entry in entries.items():
        for step in entry["assignment_history"]:
            aid = step["assignment_id"]
            probes = step.get("probe_delivery_ids",[])
            shelved = step.get("shelved_delivery_id")
            if step.get("expired_cancel") or step.get("superseded_cancels"):
                verify_cancellations(step,sources,samples,acknowledged)
            if step.get("expired_cancel"):
                shelved = None  # The cancellation timer is independent of progress inquiries.
            reports = continued.pop(aid,[])
            if not (probes or shelved or reports):
                continue
            start,end = windows.get(aid,(0,0))
            if shelved:
                observation = bind(shelved,sources,samples,acknowledged)
                if (not start<observation["seq"]<end
                        or observation["reply"]!={"observed_at":timestamp(step["pool"]["entered"])}):
                    raise ValueError("unanswered inquiry was not shelved by its original observation")
                end = observation["seq"]
                step["shelved_presence_key"] = observation["key"]
            events = []
            previous = start
            for did in probes:
                observation = bind(did,sources,samples,acknowledged)
                position = observation["seq"]
                if not previous<position<end:
                    raise ValueError("progress inquiry observation is outside its assignment")
                previous = position
                events.append((position,"probe",{"delivery_id":did,"presence_key":observation["key"]}))
            for record in reports:
                body = dict(record["body"])
                body.pop("session_id",None)
                artifact_ids = body.pop("artifact_ids",[])
                summary = body.get("summary")
                if (record["task_id"]!=tid or record["actor"]!="worker/"+step["worker_id"]
                        or "summary" in body and (not isinstance(summary,str) or not summary.strip())
                        or artifact_ids!=[] or set(body)-{"assignment_id","outcome","summary"}
                        or body.get("assignment_id")!=aid or body.get("outcome")!="continue_requested"
                        or record["reply"]!={"recorded":True,"task_state":"working"}
                        or not start<record["seq"]<end):
                    raise ValueError("continue receipt does not match its original assignment")
                events.append((record["seq"],"continue",{"key":record["key"],"summary":summary,"receipt":record["reply"]}))
            rounds = [{"probes":[]}]
            for _,kind,proof in sorted(events):
                current = rounds[-1]
                if kind=="probe":
                    current["probes"].append(proof)
                    if len(current["probes"])>4:
                        raise ValueError("progress inquiry round exceeds its original retry limit")
                else:
                    current["continue"] = proof
                    rounds.append({"probes":[]})
            if shelved and len(rounds[-1]["probes"])!=4:
                raise ValueError("last inquiry round was shelved before its retries were exhausted")
            step["inquiry_rounds"] = rounds
    if continued:
        raise ValueError("trial contains a continue report outside the reviewed assignments")
