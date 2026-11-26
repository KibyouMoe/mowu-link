function project(goal) {
  if (!goal) return null;
  return {id:goal.id,revision:goal.revision,objective:goal.objective,phase:goal.phase,
    started:goal.roundsStarted,limit:goal.maxGoalRounds,activation:goal.activation};
}

export function goalView(ctx,agent) {
  const service = ctx.get('goals');
  return {session:agent.session.id,supported:!!service,goal:service ? project(service.get(agent)) : null};
}

export async function changeGoal(ctx,agent,operation,readEvents) {
  const service = ctx.get('goals');
  if (!service) throw new Error('native_goal_unavailable');
  const current = service.get(agent);
  if (!current) throw new Error('native_goal_missing');
  const before = agent.session.seq;
  let updated;
  try {
    updated = service[operation](agent,{id:current.id,revision:current.revision});
  } catch (error) {
    if (error.code==='GOAL_INVALID_TRANSITION') throw new Error('native_goal_transition_refused');
    if (error.code==='GOAL_STALE_REVISION') throw new Error('native_goal_changed');
    throw new Error('goal_change_unconfirmed');
  }
  try {
    const events = await readEvents();
    const event = events.find(item=>item.seq===before);
    if (event?.type!=='goal/change' || event.data.operation!==operation ||
        event.data.goal?.id!==updated.id || event.data.goal?.revision!==updated.revision ||
        event.data.goal?.phase!==updated.phase) throw new Error('goal_change_unconfirmed');
    return {session:agent.session.id,operation,goal:project(updated),event_seq:event.seq};
  } catch {
    throw new Error('goal_change_unconfirmed');
  }
}
