import { isDeepStrictEqual } from 'node:util';

// Project only selector data. Host configuration, credential references and raw errors stay local.
function selection(route) {
  if (!route?.provider || !route?.model) return null;
  return {provider:route.provider,model:route.model,effort:route.reasoningEffort ?? null};
}

export async function modelView(ctx, agent, kind, events) {
  const session = agent.session.id;
  const current = selection(agent.model.current());
  const header = events.findLast(event => event.type==='request/header')?.data.header;
  const observed = selection(header?.config);
  const llm = ctx.get('llm');
  const base = {session,current,observed,default_effort:null};
  if (kind==='efforts') {
    if (!current) return {...base,items:[],unavailable:0,more:false};
    try {
      const info = await llm.resolveModelInfo(current.provider,current.model);
      const efforts = info.reasoning?.efforts ?? [];
      return {...base,default_effort:info.reasoning?.defaultEffort ?? null,
        items:efforts.slice(0,200).map(({id,name})=>({id,name})),unavailable:0,more:efforts.length>200};
    } catch {
      return {...base,items:[],unavailable:1,more:false};
    }
  }
  const catalogs = await Promise.allSettled(llm.listProviders().map(async ({id})=>{
    const models = await llm.listModels(id);
    return models.map(({id:model,name})=>({provider:id,id:model,name}));
  }));
  const items = catalogs.flatMap(item=>item.status==='fulfilled' ? item.value : []);
  return {...base,items:items.slice(0,200),unavailable:catalogs.filter(item=>item.status==='rejected').length,
    more:items.length>200};
}

export async function changeModel(ctx, agent, kind, value, bound, readEvents) {
  if (agent.status!=='idle') throw new Error('model_change_busy');
  const before = agent.session.seq;
  const llm = agent.ctx?.get('llm') ?? ctx.get('llm');
  let choice;
  if (kind==='efforts') {
    const current = selection(agent.model.current());
    if (!current) throw new Error('model_choice_unavailable');
    choice = {provider:current.provider,model:current.model};
    try {
      const info = await llm.resolveModelInfo(choice.provider,choice.model);
      if (value!=='default') {
        if (!info.reasoning?.efforts.some(effort=>effort.id===value)) throw new Error('unknown_effort');
        choice.reasoningEffort = value;
      }
    } catch {
      throw new Error('model_choice_unavailable');
    }
  } else {
    const slash = value.indexOf('/');
    if (slash<1 || slash===value.length-1) throw new Error('model_choice_needs_route');
    const provider = value.slice(0,slash);
    const model = value.slice(slash+1);
    try {
      if (!llm.listProviders().some(item=>item.id===provider)) throw new Error('unknown_provider');
      if (!(await llm.listModels(provider)).some(item=>item.id===model)) throw new Error('unknown_model');
      const current = selection(agent.model.current());
      choice = {provider,model};
      if (current?.provider===provider && current.model===model && current.effort!==null) {
        choice.reasoningEffort = current.effort;
      }
    } catch {
      throw new Error('model_choice_unavailable');
    }
  }
  try {
    await llm.resolveCallConfig(choice);
  } catch {
    throw new Error('model_choice_unavailable');
  }
  await bound();
  if (agent.status!=='idle' || agent.session.seq!==before) throw new Error('model_change_conflict');
  const selected = selection(choice);
  if (isDeepStrictEqual(selection(agent.model.current()),selected)) {
    return {session:agent.session.id,selected,changed:false};
  }
  try {
    agent.model.select(choice);
    const events = await readEvents();
    const latest = events.findLast(event=>event.type==='model/selection');
    if (latest?.seq!==before || !isDeepStrictEqual(selection(latest.data),selected) ||
        !isDeepStrictEqual(selection(agent.model.current()),selected)) {
      throw new Error('model_change_unconfirmed');
    }
    return {session:agent.session.id,selected,changed:true,event_seq:latest.seq};
  } catch {
    throw new Error('model_change_unconfirmed');
  }
}
