export function upstreamModels(upstreams) {
  return upstreams.flatMap(upstream=>{
    const models=(upstream.models || []).map(item=>typeof item==='string'?{id:item,upstream_model:item}:item);
    if(upstream.default_model && !models.some(item=>item.id===upstream.default_model)) {
      models.push({id:upstream.default_model,upstream_model:upstream.default_model});
    }
    return models.filter(item=>item.id).map(item=>({...item,model:item.upstream_model,label:item.label || item.upstream_model,
      protocol:upstream.protocol,upstream_name:upstream.name}));
  });
}

export function taskModelOptions(models, assignments, task) {
  return models.filter(model=>model.transport==='codex_subscription'
    ? model.id==='serein-codex-sol' && Array.isArray(model.allowed_tasks) && model.allowed_tasks.includes(task)
    : ['embedding','reranker'].includes(task)
    ? model.protocol==='openai'
    : ![assignments.embedding,assignments.reranker].includes(model.id));
}

export function retainTaskAssignments(assignments, models) {
  const ids=new Set(models.map(model=>model.id));
  return Object.fromEntries(Object.entries(assignments).map(([task,id])=>
    [task,ids.has(id)||id==='serein-codex-sol'?id:'']));
}

export function codexStatusLabel(status) {
  if (!status?.enabled) return '此部署未开启';
  if (status.stale) return status.state==='stale'?'状态已过期，暂不能确认在线':'尚未收到服务状态';
  if (!status.subscription_authenticated) return '等待订阅登录';
  return {starting:'正在启动',idle:'已登录 · 空闲',working:'已登录 · 正在处理',retrying:'暂时等待重试',stopped:'服务已停止'}[status.state] || '状态暂不可用';
}
