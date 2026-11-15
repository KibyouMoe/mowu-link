const modelId = selection => `${selection.provider}/${selection.model}`;

export async function dshModelCatalog(transport) {
  const controller = transport.ctx.get("sessionController");
  if (typeof controller?.modelCatalog !== "function") throw new Error("DSH model catalog is unavailable");
  const catalog = await controller.modelCatalog();
  return { models: catalog.groups.flatMap(group => group.models.map(model => ({
    id: modelId({ provider: group.id, model: model.id }), name: model.name,
    provider: group.id, nativeModel: model.id,
    efforts: (model.reasoning?.efforts ?? []).map(effort => ({ id: effort.id, name: effort.name })),
    defaultEffort: model.reasoning?.defaultEffort ?? null,
  }))), defaultSelection: { model: modelId(catalog.default), effort: catalog.default.reasoningEffort ?? null } };
}

export async function dshSelection(transport, modelId, effort) {
  const { models } = await dshModelCatalog(transport);
  const model = models.find(model => model.id === modelId);
  if (!model || effort != null && !model.efforts.some(item => item.id === effort)) throw new Error("DSH model selection is unavailable");
  return { provider: model.provider, model: model.nativeModel, ...(effort == null ? {} : { reasoningEffort: effort }) };
}

export async function dshModelSettings(transport, args) {
  const thread = transport.requireThread("model_settings", args);
  const agent = await transport.ensureAgent(thread);
  const catalog = await dshModelCatalog(transport);
  if (args.action === "update") {
    const selection = await dshSelection(transport, args.model, args.effort);
    const llm = agent.ctx?.get("llm") ?? transport.ctx.get("llm");
    await llm.resolveCallConfig(selection);
    agent.model.select(selection);
    thread.modelSelection = selection;
    await transport.ctx.get("sessions")?.flush?.(agent.session);
    await transport.save({ required: true });
  } else if (args.action !== "read") throw new Error("Invalid DSH model action");
  const current = agent.model.current();
  if (!current) throw new Error("DSH current model is unavailable");
  return { ...catalog, current: { model: modelId(current), effort: current.reasoningEffort ?? null } };
}
