/* 每轮只展示一次 Agent 输出；投影与 DOM 渲染分开，兼容历史结果与新接口。 */
(function(){
  const names={marketing:'营销 Agent',hesitant:'摇摆户 Agent',complaints:'客户投诉 Agent',analyst:'需求分析 Agent',reviewer:'风险分析 Agent',writer:'方案汇总 Agent',aggregation:'策略汇总'};
  const fields={analysis_basis:'数据与分析依据',findings:'分析发现',recommendations:'策略建议',actions:'行动方案',validation_plan:'验证与迭代计划',dependencies:'协作需求',disagreements:'分歧与待确认事项',revisions:'方案变化',target_audience:'目标客群',selling_points:'营销要点',concerns:'客户顾虑',decision_conditions:'决策条件',questions:'待确认问题',issues:'问题识别',priority:'优先级',reply_draft:'回复草稿',risks:'风险',mitigations:'应对措施',observations:'观察结果',items:'分析条目',sources:'结果来源',report:'分析报告',text:'内容',content:'内容',reason:'原因',owner:'负责人',source_id:'来源',quote:'引用',source:'来源',metric:'指标',method:'验证方法',condition:'条件',action:'行动',summary:'结论'};
  const types={initial_view:'初始观点',challenge:'提出的质疑',evidence:'证据与线索',revision:'本轮修订',feedback:'反馈意见'};
  const label=key=>fields[key]||names[key]||key;
  function lines(value){
    // 将任意字段递归展开成可读条目，保留所有值，不输出对象字符串或 JSON。
    if(value===null||value===undefined)return [];
    if(Array.isArray(value))return value.flatMap(lines);
    if(typeof value==='object')return Object.entries(value).flatMap(([key,v])=>lines(v).map(s=>`${label(key)}：${s}`));
    return [typeof value==='boolean'?(value?'是':'否'):String(value)].filter(s=>s.trim());
  }
  function projectRound(step,events){
    const response=step.response||{},data=step.result?.data||{},agentId=step.agent_id||step.capability;
    const attempts=events.filter(e=>e.kind==='agent_request'&&e.body.request.message.step_id===step.id);
    const request=attempts.at(-1)?.body.request;
    const oldRequests=events.filter(e=>e.kind==='message'&&e.body.message.step_id===step.id&&e.body.message.kind==='request');
    const peers=request?.message.payload.inputs||oldRequests.at(-1)?.body.message.payload.inputs||[];
    const discussions=response.discussion??data.discussion??events.filter(e=>e.kind==='collaboration_message'&&e.body.message.step_id===step.id).map(e=>e.body.message);
    const groups=[],seen=new Map();
    // 相同内容只列一次，但合并字段名，保留“初始观点/决策”等输出类型的区别。
    function add(title,items){const unique=[...new Set(items)].filter(Boolean);if(!unique.length)return;const signature=unique.map(s=>s.replace(/^\[Mock 预设示例\]\s*/, '').trim()).join('\n');const previous=seen.get(signature);if(previous){if(!previous.title.split(' / ').includes(title))previous.title+=' / '+title;return;}const group={title,items:unique};seen.set(signature,group);groups.push(group);}
    const decision=response.round_decision||response.final_decision||data.round_decision||data.final_decision;
    if(step.result?.summary)add('本轮结论',[step.result.summary]);
    for(const [key,value] of Object.entries(data)){
      if(['discussion','round_decision','final_decision','references','protocol_observation'].includes(key))continue;
      if(key==='revisions'&&discussions.some(d=>d.type==='revision'))continue;
      add(label(key),lines(value));
    }
    for(const [type,title] of Object.entries(types)){
      add(title,discussions.filter(d=>d.type===type).flatMap(d=>{
        const content=d.target_agent_id?`对 ${names[d.target_agent_id]||d.target_agent_id}：${d.content}`:d.content;
        return [content+(d.reason?`\n原因：${d.reason}`:'')];
      }));
    }
    // 只展示该次请求真正收到的质疑，不把之后公开的质疑补进本轮上下文。
    const incoming=request?.collaboration_messages||peers.flatMap(p=>p.discussion||[]);
    add('本轮收到的质疑',incoming.filter(d=>d.type==='challenge'&&d.target_agent_id===agentId).map(d=>`${names[d.sender]||d.sender}（第 ${d.round_no} 轮）：${d.content}`));
    add('参考的观点',(Array.isArray(data.references)?data.references:[]).flatMap(r=>{
      if(typeof r==='string')return [r];const peer=peers.find(p=>p.id===r.message_id);
      return [`${names[peer?.sender]||peer?.sender||'来源'}：${r.viewpoint||''}${r.quote?`；原观点：${r.quote}`:''}`];
    }));
    if(decision)add('本轮决策',[decision]);
    // 首屏仅保留讨论主线，完整分析与引用仍可按条目展开，不丢失历史字段。
    const coreTitles=['本轮结论','初始观点','提出的质疑','本轮收到的质疑','本轮修订','方案变化','本轮决策'];
    const coreGroups=groups.filter(g=>g.title.split(' / ').some(t=>coreTitles.includes(t)&&!(t==='初始观点'&&step.result?.summary)));
    const detailGroups=groups.filter(g=>!coreGroups.includes(g));
    return {groups,coreGroups,detailGroups,peers,version:response.strategy_version||request?.strategy_version||null,notes:response.compatibility_notes||[]};
  }
  function draw(task,events,helpers){
    const {el,badge,raw,preserve}=helpers,container=document.getElementById('round-view');
    const rounds=[...new Set(task.steps.filter(s=>s.capability!=='aggregation').map(s=>s.round_no||1))].sort((a,b)=>a-b);
    if(!rounds.length){container.replaceChildren(el('p','正在生成执行计划…','hint'));return;}
    // 轮次选择按任务保留，轮询时不会把用户正在阅读的轮次切走。
    const selected=container.dataset.task===task.id?Number(container.dataset.round)||rounds[0]:rounds[0];
    container.dataset.task=task.id;container.dataset.round=String(selected);
    preserve(container,()=>{
      const tabs=el('nav','','round-tabs');tabs.setAttribute('aria-label','选择查看轮次');
      for(const round of rounds){const button=el('button',`第 ${round} 轮`,'round-tab'+(round===selected?' active':''));button.type='button';button.setAttribute('aria-pressed',String(round===selected));button.onclick=()=>{container.dataset.round=String(round);draw(task,events,helpers);};tabs.append(button);}
      const steps=task.steps.filter(s=>(s.round_no||1)===selected&&s.capability!=='aggregation');
      const selectedAgent=steps.some(s=>s.capability===container.dataset.agent)?container.dataset.agent:steps[0]?.capability;
      container.dataset.agent=selectedAgent;
      const agentTabs=el('nav','','agent-tabs');agentTabs.setAttribute('aria-label','选择查看 Agent');
      for(const step of steps){const button=el('button',names[step.capability]||step.agent_id||step.capability,'agent-tab'+(step.capability===selectedAgent?' active':''));button.type='button';button.setAttribute('aria-pressed',String(step.capability===selectedAgent));button.onclick=()=>{container.dataset.agent=step.capability;draw(task,events,helpers);};agentTabs.append(button);}
      const body=el('div','','round-content');
      for(const step of steps.filter(s=>s.capability===selectedAgent)){
        const projected=projectRound(step,events),card=el('article','','round-agent'),header=el('div','','section-title');
        const status={pending:'等待',running:'执行中',retrying:'重试中',succeeded:'已完成',failed:'失败',skipped:'已跳过'};
        header.append(el('h3',names[step.capability]||step.agent_id||step.capability),badge(status[step.status]||step.status,step.status));card.append(header);
        if(!step.result)card.append(el('p',step.error||(step.status==='skipped'?'上游失败，本轮未执行。':'等待本轮输出…'),step.error?'error':'hint'));
        function appendGroups(parent,groups,core=false){for(const group of groups){const row=el('section','','output-row'),list=el('ol');const titles=core?[...new Set(group.title.split(' / ').map(t=>({'本轮结论':'核心观点','初始观点':'核心观点','本轮修订':'修订变化','本轮决策':'阶段方案'}[t]||t)))].join(' / '):group.title;row.append(el('h4',titles));for(const item of group.items)list.append(el('li',item));row.append(list);parent.append(row);}}
        appendGroups(card,projected.coreGroups,true);
        if(projected.detailGroups.length){const extra=el('details','','raw-details');extra.dataset.key=step.id+':extra';extra.append(el('summary','补充分析与引用依据 · '+projected.detailGroups.length+' 类'));appendGroups(extra,projected.detailGroups);card.append(extra);}
        const details=el('details','','raw-details');details.dataset.key=step.id+':technical';details.append(el('summary','上下文、版本与原始记录'));
        details.append(el('p',`调用尝试 ${step.attempts} 次${projected.version?' · 策略标识 '+projected.version:''}`,'hint'));
        if(projected.peers.length){const ul=el('ul');for(const p of projected.peers)ul.append(el('li',`${names[p.sender]||p.sender} · 第 ${p.round_no||1} 轮：${p.payload.summary}`));details.append(el('h4','本轮实际收到的上下文'),ul);}else details.append(el('p','本轮没有其他 Agent 的公开输入。','hint'));
        for(const note of projected.notes)details.append(el('p',note,'hint'));
        for(const event of events.filter(e=>e.kind==='agent_context'&&e.body.step_id===step.id))details.append(raw('Prompt 与会话',event.body,event.body.correlation_id));
        details.append(raw('原始请求与响应',events.filter(e=>['agent_request','agent_response','message'].includes(e.kind)&&(e.body.step_id===step.id||e.body.request?.message.step_id===step.id||e.body.message?.step_id===step.id)),step.id+':raw'));
        if(step.result)details.append(raw('原始结果 JSON',step.result,step.id+':result'));card.append(details);body.append(card);
      }
      container.className='';container.replaceChildren(tabs,agentTabs,body);
    });
  }
  if(typeof module!=='undefined')module.exports={projectRound,lines};
  if(typeof window!=='undefined')window.roundOutputs={projectRound,draw};
})();
