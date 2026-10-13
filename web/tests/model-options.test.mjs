import test from 'node:test';
import assert from 'node:assert/strict';
import {upstreamModels,taskModelOptions,retainTaskAssignments,codexStatusLabel} from '../src/modelOptions.js';

test('model options retain IDs but display aliases and filter assigned auxiliary models',()=>{
  const models=upstreamModels([{name:'Provider',protocol:'openai',models:[
    {id:'opaque-chat',upstream_model:'actual-chat',label:'Chat'},
    {id:'opaque-embed',upstream_model:'actual-embed',label:''},
    {id:'opaque-rerank',upstream_model:'actual-rerank'},
  ]},{name:'Messages',protocol:'anthropic',models:['other-chat']}]);
  assert.deepEqual(models.map(m=>m.label),['Chat','actual-embed','actual-rerank','other-chat']);
  const assignments={embedding:'opaque-embed',reranker:'opaque-rerank'};
  for(const task of ['chat','writer','persona'])assert.deepEqual(taskModelOptions(models,assignments,task).map(m=>m.id),['opaque-chat','other-chat']);
  for(const task of ['embedding','reranker'])assert.deepEqual(taskModelOptions(models,assignments,task).map(m=>m.id),['opaque-chat','opaque-embed','opaque-rerank']);
  assert.equal(taskModelOptions(models,{embedding:'opaque-chat'},'writer').some(m=>m.id==='opaque-embed'),true);
});

test('subscription task model is restricted and survives API catalog edits',()=>{
  const allowed=['writer','persona','dreams','narrative_scout','relations','operit_tagging','arc_linker'];
  const internal={id:'serein-codex-sol',transport:'codex_subscription',allowed_tasks:allowed};
  for(const task of allowed)assert.deepEqual(taskModelOptions([internal],{},task),[internal]);
  for(const task of ['chat','embedding','reranker','image_transcription','track_router','event_curator','event_writer'])
    assert.deepEqual(taskModelOptions([internal],{},task),[]);
  assert.deepEqual(retainTaskAssignments({writer:internal.id,dreams:'removed',persona:'kept'},[{id:'kept'}]),
    {writer:internal.id,dreams:'',persona:'kept'});
});

test('subscription status distinguishes stale data, authentication and execution',()=>{
  assert.match(codexStatusLabel({enabled:true,state:'stale',stale:true,subscription_authenticated:true}),/不能确认在线/);
  assert.equal(codexStatusLabel({enabled:true,state:'idle',subscription_authenticated:false}),'等待订阅登录');
  assert.equal(codexStatusLabel({enabled:true,state:'working',subscription_authenticated:true}),'已登录 · 正在处理');
});
