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
