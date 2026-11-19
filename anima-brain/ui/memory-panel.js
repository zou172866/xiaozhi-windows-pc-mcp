import { applyHostTheme } from './host-theme.js';
import { App } from '@modelcontextprotocol/ext-apps';
const app = new App({name:'Companion Memory',version:'1.0.0'});
const $=id=>document.getElementById(id);
let data, tab='facts', zh=false, busy=false, generation=0, hostContext={};
const t=(en,cn)=>zh?cn:en;
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function button(text,fn,cls){const b=el('button',text,cls);b.type='button';b.disabled=busy;b.onclick=fn;return b;}
async function call(name,args){const r=await app.callServerTool({name,arguments:args});if(r.isError)throw Error('tool');if(r.structuredContent)return r.structuredContent;const text=r.content?.find(c=>c.type==='text')?.text;if(!text)throw Error('tool');return JSON.parse(text);}
function announce(s){$('status').textContent=s;}
async function refresh(offset=data?.offset||0){
 if(!data||busy)return;
 const current=++generation, cid=data.character_id;busy=true;render();
 try{const d=await call('refresh_memory_panel',{character_id:cid,offset});if(current===generation)data=d;}
 catch{if(current===generation)announce(t('Could not refresh. Try again.','刷新失败，请重试。'));}
 finally{if(current===generation){busy=false;render();}}
}
async function save(action,m,text){
 if(busy)return;const cid=data.character_id,current=++generation;busy=true;
 $('content').querySelectorAll('button').forEach(b=>b.disabled=true);
 try{
  await call(action,{character_id:cid,memory_id:m.id,expected_revision:m.revision,...(text===undefined?{}:{text})});
  if(current!==generation)return;
  data=await call('refresh_memory_panel',{character_id:cid,offset:data.offset});
  if(current!==generation)return;
  announce(t('Saved to your memory database.','已保存到记忆数据库。'));
 }catch{if(current===generation)announce(t('Not confirmed. Refresh before retrying; this memory may have changed.','操作未确认。请先刷新，记忆可能已被其他对话修改。'));}
 finally{if(current===generation){busy=false;render();}}
}
function fact(m){
 const a=el('article');a.append(el('h2',m.key),el('small',`${m.category} · Revision ${m.revision}`),el('p',m.text));
 const details=el('details'),summary=el('summary',t('Source quote','来源引文'));details.append(summary,el('blockquote',m.source_quote));a.append(details);
 const actions=el('div',undefined,'actions');
 actions.append(button(t('Edit','修改'),()=>{
   a.replaceChildren(el('h2',t('Edit memory','修改记忆')));
   const label=el('label',t('Your corrected statement','你修正后的内容')),input=el('textarea');input.value=m.text;input.maxLength=1200;label.append(input);
   const buttons=el('div',undefined,'actions');buttons.append(button(t('Cancel','取消'),render),button(t('Save','保存'),()=>{if(input.value.trim())save('edit_memory_from_panel',m,input.value.trim());}));
   a.append(label,el('small',t('Saved as your explicit correction; original conversation stays unchanged.','作为你的明确修正保存，原始对话不会更改。')),buttons);input.focus();
 }),button(t('Delete','删除'),()=>{
   actions.replaceChildren(el('p',t('Delete this fact and its revisions? Original chat remains; cached summaries are cleared.','删除这条事实及其修订记录？原始聊天仍保留，缓存摘要会清除。')),button(t('Cancel','取消'),render),button(t('Confirm deletion','确认删除'),()=>save('delete_memory_from_panel',m), 'danger'));
 },'danger'));a.append(actions);return a;
}
function render(){
 if(!data)return;
 $('title').textContent=`${data.name} · ${t('Memory','记忆')}`;
 $('subtitle').textContent=t('Facts, experiences and the sources behind them.','事实、经历，以及它们的来源。');
 $('refresh').textContent=t('Refresh','刷新');$('refresh').disabled=busy;
 const expanded=hostContext.displayMode==='fullscreen';
 $('expand').hidden=expanded;$('expand').textContent=t('Manage memories','管理记忆');
 $('tabs').hidden=!expanded;
 $('tabs').replaceChildren();
 for(const [key,en,cn] of [['facts','Facts','事实'],['documents','Core & experiences','核心与经历'],['status','Processing','处理状态']]){const b=button(t(en,cn),()=>{tab=key;render();});b.setAttribute('aria-pressed',String(tab===key));$('tabs').append(b);}
 const content=$('content');content.replaceChildren();$('pages').replaceChildren();
 if(!expanded){
   content.append(el('small',`${data.total} ${t('saved facts','条事实记忆')}`));
   for(const m of data.facts.slice(0,3)){const a=el('article');a.append(el('p',m.text));content.append(a);}
   if(!data.facts.length)content.append(el('p',t('No facts saved yet.','尚无事实记忆。'),'muted'));
   if(data.total>3)content.append(el('p',t('Open to view sources, summaries, and edit your memories.','展开查看来源、摘要，以及编辑记忆。'),'muted'));
   return;
 }
 if(tab==='facts'){
  content.append(el('span',`${data.total} ${t('saved','条已保存')}`,'badge'));
  for(const m of data.facts)content.append(fact(m));
  if(!data.facts.length)content.append(el('p',t('No facts on this page.','此页暂无事实记忆。'),'empty'));
  if(data.offset>0)$('pages').append(button(t('Previous','上一页'),()=>refresh(Math.max(0,data.offset-50))));
  if(data.offset+data.facts.length<data.total)$('pages').append(button(t('Next','下一页'),()=>refresh(data.offset+50)));
 }else if(tab==='documents'){
  content.append(el('p',t('Latest 8 summaries. Derived from conversations; current corrections take priority.','最近 8 条摘要，源于对话整理；当前修正优先于旧摘要。'),'muted'));
  for(const d of data.documents){const a=el('article');a.append(el('h2',({core:t('Core memory','核心记忆'),character_core:t('Character background','角色背景'),episode:t('Experience','经历')})[d.kind]||d.kind),el('p',d.text));
   const details=el('details');details.append(el('summary',t('View original sources','查看原始来源')));let loaded=false;
   details.ontoggle=async()=>{if(!details.open||loaded)return;loaded=true;try{const r=await call('expand_evidence',{character_id:data.character_id,evidence_ids:d.source_ids.slice(0,8)});for(const s of r.sources||[])details.append(el('small',s.speaker),el('blockquote',s.text));if(d.source_ids.length>8)details.append(el('small',t('Showing the first 8 citations. Ask your agent for the remaining sources.','显示前 8 条引用，可让助手继续查看剩余来源。')));}catch{loaded=false;details.append(el('p',t('Could not load sources. Reopen to retry.','来源加载失败，重新展开可重试。')));}};
   a.append(details);content.append(a);
  }if(!data.documents.length)content.append(el('p',t('No consolidated memories yet.','尚未整理出核心或事件记忆。'),'empty'));
 }else{
  const s=data.automation;
  if(s.policy.host_summaries)content.append(el('p',t('Summaries are written by your chat host during conversation; no backend summary API.','摘要由当前对话模型在聊天时整理，不调用后台摘要 API。'),'muted'));
  for(const [en,cn,value] of [['Indexed excerpts','已索引原文',`${s.indexed_count} / ${s.evidence_count}`],['Awaiting summary','待整理轮数',s.summary_backlog.turns],['Background embedding','后台向量索引',s.policy.embedding],['Background summaries','后台摘要整理',s.policy.summaries],['Summaries by chat host','由对话模型整理摘要',s.policy.host_summaries],['Semantic retrieval','语义检索',s.policy.semantic_retrieval],['Summary interval (turns)','摘要间隔（轮）',s.policy.summary_turns],['24-hour job cap','24 小时任务上限',s.policy.daily_jobs]])content.append(el('p',`${t(en,cn)}: ${typeof value==='boolean'?(value?t('On','开启'):t('Off','关闭')):value}`));
  for(const issue of s.provider_issues)content.append(el('p',issue==='embedding_provider_missing'?t('Configure an embedding provider in local Provider settings.','请在本地 Provider settings 配置 embedding 服务。'):t('Configure a summary/chat provider in local Provider settings.','请在本地 Provider settings 配置摘要 / 聊天模型。')));
  content.append(el('p',t('Background work requires the engine to remain running. Limits count jobs, not dollars.','后台任务需要 Engine 持续运行。上限按任务次数计，不是美元预算。'),'muted'));
  for(const j of s.jobs){const row=el('article');row.append(el('p',`${j.kind} · ${j.state}`));if(j.state==='review')row.append(el('p',t('Needs review: the previous request may have been billed. Ask your agent to inspect this job before authorizing another attempt.','待检查：前次请求可能已计费。请让助手检查任务后，再决定是否重新尝试。')),el('small',j.id));content.append(row);}
 }
}
function host(ctx){applyHostTheme(ctx);hostContext={...hostContext,...ctx};zh=/^zh/i.test(hostContext.locale||navigator.language);document.documentElement.lang=zh?'zh':'en';document.documentElement.dataset.theme=hostContext.theme||'';render();}
app.ontoolresult=r=>{generation++;busy=false;const d=r.structuredContent;if(d?.character_id&&Array.isArray(d.facts)&&d.automation){data=d;announce('');render();}};
app.onhostcontextchanged=host;
$('refresh').onclick=()=>refresh();
$('expand').onclick=async()=>{try{await app.requestDisplayMode({mode:'fullscreen'});}catch{announce(t('Expanded view unavailable in this client. Ask your agent to inspect more memories.','此客户端不支持展开，可让助手继续查看记忆。'));}};
await app.connect();host(app.getHostContext());
