/* 可选功能控件单独放置，保持基础报告展示不依赖模型或路由。 */
(function(){const {el,$}=window.workbench;
 function makeSelect(id,title,options,parent){const label=el('label',title),s=el('select');s.id=id;for(const [value,name] of options){const o=el('option',name);o.value=value;s.append(o);}label.append(s);$(parent).append(label);return s;}
 const aggregation=makeSelect('aggregation','结果整合方式',[['deterministic','本地有来源模板报告'],['deepseek','DeepSeek · 统一策略整合']],'aggregation-controls');
 const routing=makeSelect('routing','协作图路由',[['manual','手动固定 · 对照实验'],['rule','RuleGraphSelector · 规则选择'],['llm','LLMGraphSelector · DeepSeek 选择']],'routing-controls');
 const graph=makeSelect('graph-id','固定模式',[['protocol','使用下方 Independent / Debate / AAD'],['single_marketing','仅营销 Agent'],['single_hesitant','仅摇摆户 Agent'],['single_complaints','仅客户投诉 Agent']],'routing-controls');
 let previousEnabled=false;
 const roleNames={marketing:'营销',hesitant:'摇摆户',complaints:'投诉'};let graphNames={};
 window.workbench.api('/api/graphs').then(rows=>{graphNames=Object.fromEntries(rows.map(g=>[g.id,g.name]));}).catch(()=>{});
 function routingChanged(){const classic=$('workflow').value==='classic',manual=routing.value==='manual',single=manual&&graph.value!=='protocol';graph.disabled=classic||!manual;$('collaboration').disabled=classic||!manual||single;
  if(!manual){$('rounds').disabled=false;$('rounds').min='2';if(Number($('rounds').value)<2)$('rounds').value='2';$('mode').value='parallel';$('mode').disabled=true;$('protocol-note').textContent='自动路由将根据任务与注册能力选择图；下面总轮数用于辩论图，单 Agent 和并行图只运行一轮。';}
  else {if(single)$('collaboration').value='independent';window.workbench.protocolChanged();if(single){$('mode').value='parallel';$('mode').disabled=true;}}
 }
 function providerChanged(){const enabled=$('provider').value==='deepseek'&&$('workflow').value==='business';if(enabled&&!previousEnabled)aggregation.value='deepseek';previousEnabled=enabled;aggregation.querySelector('[value="deepseek"]').disabled=!enabled;aggregation.disabled=$('workflow').value!=='business';if(!enabled)aggregation.value='deterministic';routing.disabled=$('workflow').value!=='business';routing.querySelector('[value="llm"]').disabled=!enabled;if(!enabled&&routing.value==='llm')routing.value='rule';if($('workflow').value==='classic'){routing.value='manual';graph.value='protocol';}routingChanged();}
 routing.onchange=routingChanged;graph.onchange=routingChanged;
 window.workbenchExtension={providerChanged,workflowChanged:providerChanged,
  request:()=>({aggregation:aggregation.value,routing:routing.value,graph_id:routing.value==='manual'&&graph.value!=='protocol'?graph.value:null,...(routing.value!=='manual'?{collaboration:'independent',mode:'parallel'}:{})}),
  draw:(t,events)=>{const choice=t.graph_selection,box=$('selection');box.hidden=!choice;if(!choice)return;box.replaceChildren(el('strong',`${graphNames[choice.graph_id]||choice.graph_id} · ${{manual:'手动固定',rule:'规则选择',llm:'DeepSeek 选择'}[choice.source]}`),el('div',choice.reason));if(choice.fallback_reason)box.append(el('div','回退记录：'+choice.fallback_reason));
   const order=choice.agents.map(id=>roleNames[id]||id);let sequence=choice.collaboration==='independent'?order.join(choice.execution_mode==='serial'?' → ':' ∥ '):order.join(' → ');if(choice.collaboration==='aad')sequence=`R1（${order.join(' ∥ ')}）→ R2 起（${order.join(' → ')}）`;else if(choice.rounds>1)sequence+=` · ${choice.rounds} 轮`;$('plan').textContent=`执行顺序：${sequence} → ${t.request.aggregation==='deepseek'?'DeepSeek 策略汇总':'本地报告'}`;
   const calls=events.filter(e=>e.kind==='model_request');box.append(el('div',`模型请求尝试：图选择 ${calls.filter(e=>e.body.purpose==='graph_selection').length} · 专业分析 ${calls.filter(e=>!e.body.purpose).length} · 策略汇总 ${calls.filter(e=>e.body.purpose==='aggregation').length}`));
  }};providerChanged();
})();
