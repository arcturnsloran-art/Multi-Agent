/* 策略版本输入和旧讨论投影接口；页面的逐轮输出由 rounds.js 渲染。 */
(function(){
  function projectJourney(task,events){
    const requests=events.filter(e=>e.kind==='agent_request').map(e=>e.body.request);
    const messages=events.filter(e=>e.kind==='collaboration_message').map(e=>e.body.message);
    const ids=[...new Set(task.steps.filter(s=>s.capability!=='aggregation').map(s=>s.agent_id||s.capability))];
    return ids.map(id=>{
      const steps=task.steps.filter(s=>(s.agent_id||s.capability)===id&&s.capability!=='aggregation').sort((a,b)=>a.round_no-b.round_no);
      const done=steps.filter(s=>s.result),first=done[0],last=done[done.length-1];
      return {id,firstPlan:first?.result?.summary||'等待首轮方案',version:last?.response?.strategy_version||task.request.strategy_versions?.[id]||'baseline',
        challenges:messages.filter(m=>m.type==='challenge'&&m.target_agent_id===id).map(m=>({...m,
          receivedRounds:requests.filter(r=>r.message.recipient===id&&r.collaboration_messages.some(x=>x.id===m.id)).map(r=>r.message.round_no)})),
        revisions:done.flatMap(s=>(s.response?.discussion||[]).filter(d=>d.type==='revision').map(d=>({...d,round_no:s.round_no,
          replies:messages.filter(m=>(d.reply_to||[]).includes(m.id))}))),
        finalDecision:last?.response?.round_decision||last?.response?.final_decision||null,lastPlan:last?.result?.summary||'尚无成功结果',
        complete:task.status==='succeeded',notes:[...new Set(done.flatMap(s=>s.response?.compatibility_notes||[]))]};
    });
  }
  if(typeof module!=='undefined')module.exports={projectJourney};
  if(typeof document==='undefined')return;
  const {el,$}=window.workbench;
  const labels={marketing:'营销 Agent',hesitant:'摇摆户 Agent',complaints:'客户投诉 Agent'};
  // 版本仅用于实验标识与外部 Agent 输入，本地 Demo 不按它加载或优化 Prompt。
  for(const id of Object.keys(labels)){
    const label=el('label',labels[id]+' · 策略版本标识'),input=el('input');input.id='strategy-'+id;input.value='baseline';input.maxLength=100;label.append(input);$('strategy-controls').append(label);
  }
  window.collaborationView={
    request:()=>({strategy_versions:$('workflow').value==='business'?Object.fromEntries(Object.keys(labels).map(id=>[id,$('strategy-'+id).value.trim()||'baseline'])):{}}),
  };
})();
