const test=require('node:test');const assert=require('node:assert/strict');
const {projectResult,compareResults}=require('../app/static/workbench.js');
test('旧任务字段投影为可读发现与建议',()=>{const p=projectResult({summary:'原结论',data:{risks:['风险'],mitigations:['建议']}});assert.deepEqual(p.findings,['风险']);assert.deepEqual(p.recommendations,['建议']);assert.deepEqual(p.references,[])});
test('变化比较不把可见观点冒充采纳观点',()=>{const old={summary:'旧',data:{actions:['甲']}};const current={summary:'新',data:{actions:['乙']}};assert.deepEqual(compareResults(current,old),{summaryChanged:true,added:['乙'],removed:['甲']});assert.deepEqual(projectResult(current).references,[])});
test('缺失结果和未变化的旧任务',()=>{assert.equal(projectResult(null).summary,'');assert.equal(compareResults(null,null),null);const r={summary:'相同',data:{actions:['甲']}};assert.deepEqual(compareResults(r,r),{summaryChanged:false,added:[],removed:[]})});

const {projectJourney}=require('../app/static/collaboration.js');
test('公开质疑只有进入实际请求后才标记为已收到',()=>{
  const challenge={id:'c1',type:'challenge',sender:'complaints',target_agent_id:'marketing',content:'如何验证？',source_message_ids:['r1'],round_no:2};
  const task={status:'succeeded',request:{},steps:[{agent_id:'marketing',capability:'marketing',id:'r1',round_no:1,result:{summary:'初稿'}},{agent_id:'marketing',capability:'marketing',id:'r2',round_no:2,result:{summary:'修改稿'},response:{strategy_version:'v1',discussion:[{type:'revision',content:'先验证',reason:'证据不足'}],final_decision:'先验证'}}]};
  const events=[{kind:'collaboration_message',body:{message:challenge}}];
  let row=projectJourney(task,events)[0];assert.deepEqual(row.challenges[0].receivedRounds,[]);assert.equal(row.firstPlan,'初稿');assert.equal(row.finalDecision,'先验证');assert.equal(row.revisions[0].reason,'证据不足');
  events.push({kind:'agent_request',body:{request:{message:{recipient:'marketing',round_no:3},collaboration_messages:[challenge]}}});
  assert.deepEqual(projectJourney(task,events)[0].challenges[0].receivedRounds,[3]);
  task.steps[1].round_no=3;
  task.steps[1].response.discussion[0].reply_to=['c1'];
  assert.equal(projectJourney(task,events)[0].revisions[0].replies[0].content,'如何验证？');
});
test('旧任务只展示已保存方案，不编造质疑、修订或最终决策',()=>{
  const row=projectJourney({request:{},status:'failed',steps:[{id:'r1',capability:'marketing',round_no:1,result:{summary:'旧方案'}}]},[])[0];
  assert.deepEqual(row.challenges,[]);assert.deepEqual(row.revisions,[]);assert.equal(row.finalDecision,null);assert.equal(row.lastPlan,'旧方案');assert.equal(row.complete,false);
});

const {projectRound,lines}=require('../app/static/rounds.js');
test('按轮次保留各种实际输出，嵌套业务字段展开为逐项文字',()=>{
  const step={id:'r2.marketing',agent_id:'marketing',capability:'marketing',round_no:2,result:{summary:'本轮方案',data:{findings:['发现甲','发现乙'],recommendations:['先验证'],validation_plan:[{metric:'转化率',method:'对照实验'}],custom_output:{segment:['甲类','乙类']}}},response:{discussion:[{type:'challenge',target_agent_id:'complaints',content:'是否有明细？'},{type:'evidence',content:'来自案例的线索'},{type:'revision',content:'新增验证步骤',reason:'证据不足'},{type:'feedback',content:'建议确认口径'}],final_decision:'先做对照验证'}};
  const groups=projectRound(step,[]).groups;
  const group=title=>groups.find(g=>g.title===title).items;
  assert.deepEqual(group('分析发现'),['发现甲','发现乙']);
  assert.deepEqual(group('验证与迭代计划'),['指标：转化率','验证方法：对照实验']);
  assert.deepEqual(group('custom_output'),['segment：甲类','segment：乙类']);
  assert.deepEqual(group('提出的质疑'),['对 客户投诉 Agent：是否有明细？']);
  assert.deepEqual(group('本轮修订'),['新增验证步骤\n原因：证据不足']);
  assert.deepEqual(group('反馈意见'),['建议确认口径']);
  assert.deepEqual(group('本轮决策'),['先做对照验证']);
  assert.ok(!groups.flatMap(g=>g.items).some(s=>s.includes('[object Object]')));
});
test('本轮收到的质疑严格取自本轮请求，不混入后来公开的质疑',()=>{
  const request={message:{step_id:'r2.marketing',payload:{inputs:[]}},collaboration_messages:[{type:'challenge',target_agent_id:'marketing',sender:'complaints',round_no:1,content:'已收到的问题'}]};
  const step={id:'r2.marketing',agent_id:'marketing',result:{summary:'方案',data:{}}};
  const events=[{kind:'agent_request',body:{request}},{kind:'collaboration_message',body:{message:{step_id:'r2.complaints',type:'challenge',target_agent_id:'marketing',content:'尚未收到的问题'}}}];
  const groups=projectRound(step,events).groups;
  assert.deepEqual(groups.find(g=>g.title==='本轮收到的质疑').items,['客户投诉 Agent（第 1 轮）：已收到的问题']);
  assert.ok(!groups.flatMap(g=>g.items).some(s=>s.includes('尚未收到的问题')));
});
test('旧结果、缺失结果和相同输出不会产生虚构字段或重复区块',()=>{
  const old={id:'old',capability:'marketing',result:{summary:'[Mock 预设示例] 原方案',data:{actions:['行动甲'],recommendations:['行动甲'],discussion:[{type:'initial_view',content:'原方案'}],final_decision:'原方案'}}};
  const groups=projectRound(old,[]).groups;
  assert.equal(groups.filter(g=>g.items.includes('行动甲')).length,1);
  assert.equal(groups.filter(g=>g.items.some(s=>s.includes('原方案'))).length,1);
  assert.ok(groups.find(g=>g.items[0].includes('原方案')).title.includes('初始观点'));
  assert.ok(groups.find(g=>g.items[0].includes('原方案')).title.includes('本轮决策'));
  assert.deepEqual(projectRound({id:'failed',capability:'marketing'},[]).groups,[]);
  assert.deepEqual(lines({enabled:false,count:0}),['enabled：否','count：0']);
});
