'use strict';
const $ = id => document.getElementById(id);
const timeKeys = ['pre_trigger_seconds','post_trigger_seconds','history_retention_seconds','trigger_cooldown_seconds','max_event_seconds','dedup_seconds'];
const byteKeys = ['history_bytes','input_bytes','writer_bytes','context_bytes','minimum_free_bytes'];
let token = sessionStorage.getItem('recorderToken') || '';
let loaded = null, dirty = false, latest = null, triggering = false, rulesKey = '', polling = false;
const reasonNames = {busy:'正在录制，当前请求已拒绝', cooldown:'处于冷却期，请稍后重新触发', finalizing:'上一事件仍在写盘', duplicate_id:'重复的请求 ID', input_queue_full:'输入队列已满', manager_error:'事件管理器处理失败', dedup_capacity:'去重缓存已满'};
function el(tag, cls, text) { const node=document.createElement(tag); if(cls)node.className=cls; if(text!==undefined)node.textContent=text; return node; }
function notice(text, kind='') { const n=$('notice'); n.className='notice'+(kind?' '+kind:''); n.textContent=text; }
function markDirty() { dirty=true; $('config-dirty').textContent='有未保存的修改'; $('save-state').textContent='修改尚未保存'; }
function showTab(name) { document.querySelectorAll('.page').forEach(p=>p.classList.toggle('hidden',p.id!==name)); document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===name)); $('page-name').textContent={overview:'录制控制台',configuration:'机器人与配置',events:'事件记录'}[name]; if(name==='events')loadEvents(); }
async function api(path, data) {
  const headers={}; if(token)headers.Authorization='Bearer '+token;
  if(data!==undefined)headers['Content-Type']='application/json';
  const response=await fetch(path,{method:data===undefined?'GET':'POST',headers,body:data===undefined?undefined:JSON.stringify(data),cache:'no-store'});
  const result=await response.json();
  if(!response.ok) { if(response.status===401&&!$('auth-dialog').open)$('auth-dialog').showModal(); throw new Error(result.error||'请求失败'); }
  return result;
}
function size(bytes) { if(bytes===undefined||bytes===null)return '—'; const mib=bytes/1048576; return mib>=1024?(mib/1024).toFixed(2)+' GiB':mib.toFixed(1)+' MiB'; }
function renderState(snapshot) {
  latest=snapshot; const s=snapshot.recorder||{}, online=snapshot.online, state=online?s.state:'OFFLINE';
  $('connection').className='connection'+(online?'':' offline'); $('connection').textContent=online?'录制器已连接':'录制器离线';
  $('status-panel').dataset.state=state;
  const labels={READY:['可接受触发','准备就绪','历史缓存持续更新，选择下方机器人类型开始录制。'],CAPTURING:['正在采集后置窗口','录制中','新的触发请求将被拒绝，当前窗口不会延长。'],FINALIZING:['正在关闭 bag','写盘中','等待数据写盘完成后，才能接受下一次触发。'],COOLDOWN:['窗口已结束','冷却中','冷却期内的新请求会直接拒绝，不会自动重试。'],OFFLINE:['暂无实时状态','未连接','请启动 recorder，或检查 ROS domain 和服务连接。']};
  const label=labels[state]||labels.OFFLINE;
  $('state-label').textContent=label[0]; $('state-title').textContent=label[1]; $('state-description').textContent=label[2];
  $('active-event').textContent=online&&s.active_rule?'机器人 '+s.active_rule+' · '+s.active_event_id:'';
  $('countdown').textContent=!online?'—':state==='CAPTURING'?s.remaining_seconds.toFixed(1)+' s':state==='COOLDOWN'?s.cooldown_remaining_seconds.toFixed(1)+' s':state==='READY'?'READY':'…';
  $('timer-label').textContent=state==='CAPTURING'?'后置窗口剩余':state==='COOLDOWN'?'冷却剩余':'录制状态';
  const cfg=s.configuration||{};
  $('window-summary').textContent=online?'前置 '+cfg.pre_trigger_seconds+' 秒 / 后置 '+cfg.post_trigger_seconds+' 秒':'等待实时配置';
  $('storage-badge').textContent=online?(s.storage_id||'').toUpperCase():'OFFLINE';
  $('history-size').textContent=online?size(s.history_bytes):'—';
  $('history-progress').value=online&&s.history_limit_bytes?Math.min(100,100*s.history_bytes/s.history_limit_bytes):0;
  $('history-detail').textContent=online?'上限 '+size(s.history_limit_bytes)+' · 最旧消息距今 '+s.history_span_seconds.toFixed(1)+' s':'等待数据';
  $('writer-size').textContent=online?size(s.writer_queue_bytes):'—'; $('writer-detail').textContent=online?s.writer_queue_items+' 个待处理任务':'—';
  $('input-drops').textContent=online?s.input_drops:'—'; $('trigger-rejections').textContent=online?s.trigger_rejections:'—';
  $('restart-notice').classList.toggle('hidden',!snapshot.needs_restart);
  const rules=s.rules||{}, key=JSON.stringify(rules);
  if(key!==rulesKey) {
    rulesKey=key; $('trigger-cards').replaceChildren(); $('rule-count').textContent=Object.keys(rules).length;
    for(const [name,topics] of Object.entries(rules)) {
      const card=el('article','trigger-card'),body=el('div','card-body'),title=el('div','card-title');
      title.append(el('span','rule-icon','⌁'),el('h3','',name)); body.append(title,el('p','',topics.length+' 个录制 topics'));
      const chips=el('div','topic-chips');topics.forEach(t=>chips.append(el('span','topic-chip',t)));body.append(chips);
      const bottom=el('div','card-footer'),hint=el('small','rule-action-hint','');
      const button=el('button','button primary trigger-button','触发录制 →');button.dataset.rule=name;button.onclick=()=>triggerRule(name);
      bottom.append(hint,button);card.append(body,bottom);$('trigger-cards').append(card);
    }
    if(!Object.keys(rules).length)$('trigger-cards').append(el('div','panel empty','等待运行中的 trigger list…'));
  }
  document.querySelectorAll('.trigger-button').forEach(b=>{b.disabled=!online||state!=='READY'||triggering;});
  document.querySelectorAll('.rule-action-hint').forEach(h=>h.textContent=!online?'未连接':state==='READY'?(s.prehistory_ready?'可接受触发':'启动预热中 · 可触发但历史不足'):'暂不可触发');
  if(s.last_event) {
    $('last-event').className='panel';$('last-event').replaceChildren(eventCard(s.last_event,false));
  }
}
async function poll() { if(polling)return;polling=true;try {renderState(await api('/api/state'));}catch(e){renderState({online:false,recorder:latest?.recorder});}finally{polling=false;} }
async function triggerRule(rule) {
  if(triggering)return;triggering=true;if(latest)renderState(latest);
  try {const result=await api('/api/trigger',{rule});notice(result.accepted?'已接受 '+rule+'，事件 '+result.event_id:(reasonNames[result.reason]||result.reason||'触发已拒绝'),result.accepted?'':'warning');}
  catch(e){notice(e.message,'error');}
  finally{triggering=false;await poll();}
}
function textInput(value, cls, placeholder='') {const input=el('input',cls);input.type='text';input.value=value||'';input.placeholder=placeholder;return input;}
function select(values,value,cls) {const box=el('select',cls);for(const item of values){const option=el('option','',item);option.value=item;box.append(option);}box.value=value;return box;}
function topicRows() { return [...$('topic-rows').children].map(row=>({name:row.querySelector('.topic-name').value.trim(),type:row.querySelector('.topic-type').value.trim(),context:row.querySelector('.topic-context').checked,reliability:row.querySelector('.topic-reliability').value,durability:row.querySelector('.topic-durability').value})); }
function addTopic(data={}) {
  const row=el('tr'),name=textInput(data.name,'topic-name','/camera/image_raw'),type=textInput(data.type,'topic-type','sensor_msgs/msg/Image');
  name.setAttribute('aria-label','Topic 名称');type.setAttribute('aria-label','消息类型');name.required=true;type.required=true;
  const rel=select(['best_effort','reliable'],data.reliability||'best_effort','topic-reliability');rel.setAttribute('aria-label','可靠性');
  const dur=select(['volatile','transient_local'],data.durability||'volatile','topic-durability');dur.setAttribute('aria-label','持久性');
  const context=el('input','topic-context');context.type='checkbox';context.checked=!!data.context;context.setAttribute('aria-label','上下文 topic');
  const remove=el('button','remove-button','×');remove.type='button';remove.setAttribute('aria-label','删除 topic');remove.onclick=()=>{row.remove();refreshRuleTopics();markDirty();};
  for(const child of [name,type,rel,dur,context,remove]){const td=el('td');td.append(child);row.append(td);}
  function applyContext(){rel.disabled=context.checked;dur.disabled=context.checked;if(context.checked){rel.value='reliable';dur.value='transient_local';}}
  context.onchange=()=>{applyContext();markDirty();};name.onchange=refreshRuleTopics;
  applyContext();$('topic-rows').append(row);refreshRuleTopics();
}
function ruleTopics(row){return [...row.querySelectorAll('.rule-topic-options input:checked')].map(c=>c.value);}
function refreshRuleTopics() {
  const topics=topicRows().map(t=>t.name).filter(Boolean);
  for(const row of $('rule-rows').children) {
    const chosen=new Set(ruleTopics(row));const target=row.querySelector('.rule-topic-options');target.replaceChildren();
    for(const topic of [...new Set(topics)]) {const label=el('label'),check=el('input');check.type='checkbox';check.value=topic;check.checked=chosen.has(topic);label.append(check,document.createTextNode(topic));target.append(label);}
    if(!topics.length)target.append(el('span','subtle','先添加数据 topic，再选择录制内容。'));
  }
}
function addRule(name='',topics=[],isDefault=false) {
  const row=el('div','rule-editor'),header=el('div','rule-editor-header'),input=textInput(name,'rule-name','robot_type');input.required=true;input.pattern='[A-Za-z][A-Za-z0-9_]*';input.setAttribute('aria-label','机器人类型名称');
  const label=el('label','checkbox-line'),radio=el('input','default-rule');radio.type='radio';radio.name='default-rule';radio.checked=isDefault;label.append(radio,document.createTextNode('默认类型'));
  const remove=el('button','remove-button','×');remove.type='button';remove.setAttribute('aria-label','删除机器人类型');remove.onclick=()=>{row.remove();markDirty();};
  header.append(input,label,remove);const options=el('div','rule-topic-options');row.append(header,options);$('rule-rows').append(row);refreshRuleTopics();
  row.querySelectorAll('.rule-topic-options input').forEach(c=>c.checked=topics.includes(c.value));
}
function fillConfig(config) {
  loaded=config;const p=config.parameters;
  for(const key of timeKeys)$ (key).value=p[key];
  for(const key of byteKeys)$(key).value=p[key]/1048576;
  $('storage_id').value=p.storage_id;$('output_directory').value=p.output_directory;$('queue_items').value=p.queue_items;$('use_sim_time').checked=!!p.use_sim_time;
  $('rule-rows').replaceChildren();$('topic-rows').replaceChildren();
  p.topics.forEach((name,i)=>addTopic({name,type:p.types[i],reliability:p.reliability[i],durability:p.durability[i]}));
  p.context_topics.forEach((name,i)=>addTopic({name,type:p.context_types[i],context:true}));
  const names=p.trigger_list.length?p.trigger_list:['default'];
  names.forEach(name=>addRule(name,p['triggers.'+name+'.topics']||[...p.topics,...p.context_topics],name===p.default_trigger));
  $('config-path').textContent=config.path;dirty=false;$('config-dirty').textContent='已加载';$('save-state').textContent='配置已加载';
}
function collectConfig() {
  const p=structuredClone(loaded.parameters);
  for(const key of Object.keys(p))if(key.startsWith('triggers.'))delete p[key];
  for(const key of timeKeys)p[key]=Number($(key).value);
  for(const key of byteKeys)p[key]=Math.round(Number($(key).value)*1048576);
  p.storage_id=$('storage_id').value;p.output_directory=$('output_directory').value.trim();p.queue_items=Number($('queue_items').value);p.use_sim_time=$('use_sim_time').checked;
  const all=topicRows(),normal=all.filter(t=>!t.context),context=all.filter(t=>t.context);
  p.topics=normal.map(t=>t.name);p.types=normal.map(t=>t.type);p.reliability=normal.map(t=>t.reliability);p.durability=normal.map(t=>t.durability);p.context_topics=context.map(t=>t.name);p.context_types=context.map(t=>t.type);
  p.trigger_list=[];p.default_trigger='';
  for(const row of $('rule-rows').children){const name=row.querySelector('.rule-name').value.trim();p.trigger_list.push(name);p['triggers.'+name+'.topics']=ruleTopics(row);if(row.querySelector('.default-rule').checked)p.default_trigger=name;}
  if(!p.trigger_list.length)throw new Error('至少需要一种机器人类型');
  if(!p.default_trigger)p.default_trigger=p.trigger_list[0];
  return p;
}
async function saveConfig(event){event.preventDefault();if(!loaded)return;const button=$('save-config');button.disabled=true;try{const saved=await api('/api/config',{parameters:collectConfig(),revision:loaded.revision});fillConfig(saved);$('save-state').textContent='保存成功 · 重启 recorder 后生效';notice('配置已保存，上一版本保留为 .bak。重启 recorder 后应用新配置。');await poll();}catch(e){notice(e.message,'error');}finally{button.disabled=false;}}
function eventCard(event,details=true){const box=el('div',details?'event-card':''),head=el('div','event-head'),title=el('div','event-title');title.append(el('strong','',event.rule||event.trigger_rule||'未标注机器人类型'),el('span','badge '+event.status,event.status||'UNKNOWN'));head.append(title);if(event.modified_at)head.append(el('span','subtle',new Date(event.modified_at*1000).toLocaleString()));box.append(head);const meta=el('div','event-meta');if(event.message_count!==undefined)meta.append(el('span','',event.message_count.toLocaleString()+' 条消息'));meta.append(el('span','mono',event.event_id||''));box.append(meta);if(details&&event.bag_path)box.append(el('div','event-path mono',event.bag_path));if(event.errors?.length)box.append(el('div','event-errors',event.errors.join(' · ')));return box;}
async function loadEvents(){try{const result=await api('/api/events');$('event-directory').textContent=result.directory;$('event-list').replaceChildren(...result.events.map(e=>eventCard(e)));if(!result.events.length)$('event-list').append(el('div','panel empty','暂无事件。触发录制后，录制结果会出现在这里。'));}catch(e){notice(e.message,'error');}}
async function initialize(){try{fillConfig(await api('/api/config'));if($('auth-dialog').open)$('auth-dialog').close();await poll();}catch(e){notice(e.message,'error');}}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>showTab(b.dataset.open));
$('config-form').addEventListener('input',markDirty);$('config-form').addEventListener('change',markDirty);$('config-form').addEventListener('submit',saveConfig);
$('add-topic').onclick=()=>{addTopic();markDirty();};$('add-rule').onclick=()=>{addRule();markDirty();};
$('reload-config').onclick=async()=>{if(dirty&&!confirm('重新加载会丢弃页面中未保存的修改，继续吗？'))return;try{fillConfig(await api('/api/config'));}catch(e){notice(e.message,'error');}};
$('discover').onclick=async()=>{try{const result=await api('/api/topics');$('discovered').replaceChildren(el('option','','选择运行中的 topic…'));for(const topic of result.topics){if(topic.name.startsWith('/event_recorder/')||['/rosout','/parameter_events'].includes(topic.name))continue;for(const type of topic.types){const option=el('option','',topic.name+' · '+type);option.value=JSON.stringify({name:topic.name,type});$('discovered').append(option);}}}catch(e){notice(e.message,'error');}};
$('import-topic').onclick=()=>{const value=$('discovered').value;if(!value||!value.startsWith('{'))return;const topic=JSON.parse(value);if(topicRows().some(t=>t.name===topic.name)){notice('该 topic 已在配置列表中','warning');return;}addTopic(topic);markDirty();};
$('refresh-events').onclick=loadEvents;
$('auth-form').onsubmit=async event=>{event.preventDefault();token=$('access-token').value;sessionStorage.setItem('recorderToken',token);$('auth-dialog').close();await initialize();};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
initialize();setInterval(poll,500);setInterval(()=>{if(!$('events').classList.contains('hidden'))loadEvents();},3000);
