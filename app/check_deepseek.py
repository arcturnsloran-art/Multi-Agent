"""单次真实调用入口：python -m app.check_deepseek。

先运行此入口验证模型适配器，再验证协作。只发送固定虚构案例，不读取数据库。
"""
import asyncio
import json
import sys

import httpx

from app.config import ROOT, load_settings
from app.deepseek import DeepSeekAdapter, JSON_CONTRACT, ModelError
from app.models import now

FICTIONAL_CASE = (
    "这是完全虚构的测试。星芽文具店准备推出 39 元笔记本礼盒，预算 1000 元。"
    "客户小林担心纸张厚度；另一位虚构客户反映礼盒运输挤压。请给出一条营销建议。"
)


async def main() -> int:
    settings = load_settings()
    async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as client:
        adapter = DeepSeekAdapter(settings, client)
        try:
            result, metadata = await adapter.complete([
                {"role": "system", "content": "你是营销测试助手，只根据虚构材料给出建议。" + JSON_CONTRACT},
                {"role": "user", "content": FICTIONAL_CASE},
            ])
        except ModelError as exc:
            print(f"单次真实调用未通过：{exc}")
            return 1
    report = {"checked_at": now(), "success": True, "fictional_input": FICTIONAL_CASE,
              "result": result.model_dump(), **metadata}
    path = ROOT / "data/deepseek-check.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
