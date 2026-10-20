"""专业角色的离线闭环示例；不使用真实密钥或业务数据。"""
import unittest

from app.business import BusinessMockAgent, ROLES
from app.models import Message


class ProfessionalMockTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_role_independently_returns_analysis_strategy_and_validation(self):
        for role in ROLES:
            agent = BusinessMockAgent(role, "role-test")
            result = await agent.run(Message(
                task_id="role-test", step_id=role.id, sender="orchestrator",
                recipient=role.id, kind="request",
                payload={"prompt": "虚构经营任务：请给出本领域分析与经营方案；数据明细暂缺。", "inputs": []}))
            for field in ("analysis_basis", "findings", "recommendations", "validation_plan", "dependencies"):
                self.assertTrue(result.data[field], (role.id, field))
            self.assertEqual(result.data["references"], [])
            if role.id == "hesitant":
                self.assertIn("识别标准", result.summary)
                self.assertNotIn("再作购买决定", result.summary)
