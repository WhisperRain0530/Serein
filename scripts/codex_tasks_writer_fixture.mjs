// Synthetic acceptance helper: existing prompt builder and validator, no provider calls.
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {buildNarrativeTaskPrompt,normalizeNarrativeWriterResult} from '../web/server/narrativeCodexRunner.mjs';

let input='';
for await (const chunk of process.stdin){
  input+=chunk;
  if(Buffer.byteLength(input)>2_000_000)throw new Error('synthetic_input_too_large');
}
const request=JSON.parse(input);
if(request.action==='prepare'){
  const roleRules=readFileSync(fileURLToPath(new URL('../web/codex_agents/narrative_writer/AGENTS.md',import.meta.url)),'utf8');
  const output_schema=JSON.parse(readFileSync(fileURLToPath(new URL('../web/codex_agents/narrative_writer/output.schema.json',import.meta.url)),'utf8'));
  process.stdout.write(JSON.stringify({prompt:buildNarrativeTaskPrompt({...request.input,roleRules}),output_schema}));
}else if(request.action==='validate'){
  process.stdout.write(JSON.stringify(normalizeNarrativeWriterResult(request.output)));
}else throw new Error('unsupported_synthetic_action');
