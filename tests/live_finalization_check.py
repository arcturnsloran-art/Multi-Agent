"""手动联网验证：只向本地服务提交虚构案例，不接触或打印 API Key。"""
import json
import time
from pathlib import Path

import httpx


def main():
    """三轮 AAD 后执行真实 DeepSeek 聚合，核对终止、来源及模型会话。"""
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=30) as client:
        response = client.post("/api/tasks", json={
            "prompt": "【纯虚构案例】星芽文具店投放点击率下降，部分客户反复询问纸张透墨问题，另有运输挤压投诉。具体指标与因果关系未知。营销希望小范围试投，投诉团队希望先验证包装和服务承载。请各领域分析并讨论行动先后；证据不足时保留分歧，不能编造共识或业务事实。",
            "workflow": "business", "provider": "deepseek", "collaboration": "aad",
            "rounds": 3, "aggregation": "deepseek", "timeout_seconds": 120, "max_retries": 1,
        })
        response.raise_for_status()
        task_id = response.json()["id"]
        print("task_id:", task_id, flush=True)
        previous = None
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            response = client.get("/api/tasks/" + task_id)
            response.raise_for_status()
            detail = response.json()
            task, events = detail["task"], detail["events"]
            progress = (task["status"], task["execution_phase"], sum(s["status"] == "succeeded" for s in task["steps"]))
            if progress != previous:
                print("progress:", progress, flush=True)
                previous = progress
            if task["status"] in ("succeeded", "failed"):
                break
            time.sleep(2)
        else:
            raise RuntimeError("验证等待超时；后台任务可通过页面继续查看")
        errors = [e["body"] for e in events if e["kind"] == "attempt_error"]
        summary = {"task_id": task_id, "status": task["status"], "phase": task["execution_phase"],
            "termination_reason": task["termination_reason"], "steps": len(task["steps"]),
            "model_calls": sum(e["kind"] == "model_request" for e in events),
            "aggregation_starts": sum(e["kind"] == "aggregation_started" for e in events),
            "errors": errors, "result": task["result"]}
        Path("data/finalization-check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in summary.items() if k != "result"}, ensure_ascii=False), flush=True)
        assert task["status"] == "succeeded", task["error"]
        assert summary["aggregation_starts"] == 1
        assert task["result"]["data"]["aggregation"] == "deepseek"
        assert all(s.startswith("r3.") for s in task["result"]["data"]["source_steps"])
        assert task["result"]["data"]["consensus_claimed"] is False
        report = task["result"]["data"]["report"]
        assert all(report[k] for k in ("core_issues", "key_findings", "actions", "verification"))
        for event in events:
            if event["kind"] != "model_request" or event["body"].get("purpose") == "aggregation":
                continue
            for message in event["body"]["messages"]:
                if message["role"] == "assistant":
                    for ref in json.loads(message["content"])["data"].get("references", []):
                        assert "message_id" not in ref and "quote" not in ref
        print("verified: finite rounds, one aggregation, evidence-checked final report, single-ID model history", flush=True)


if __name__ == "__main__":
    main()
