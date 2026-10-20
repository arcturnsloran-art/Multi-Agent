"""小型同步 SQLite 存储；每次变更在同一事务写入快照与事件。"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.models import EvolutionEvent, TaskRecord, now


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
                CREATE INDEX IF NOT EXISTS events_kind_seq ON events(kind, seq);
            """)

    @contextmanager
    def connect(self):
        """SQLite 的事务上下文不会关闭连接，必须显式 finally 释放句柄。"""
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, task: TaskRecord, kind: str, **body):
        """无 await 的短事务：同一进程事件循环内不会交错修改快照。"""
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (task.id, task.model_dump_json()))
            written = db.execute("INSERT INTO events(task_id, created_at, kind, body) VALUES (?, ?, ?, ?)",
                                 (task.id, now(), kind, json.dumps(body, ensure_ascii=False)))
            # 终态快照、轨迹边界与进化事件同一事务落库，避免进程退出后丢失导出通知。
            terminal = task.status in ("succeeded", "failed") and (
                kind == "interrupted" or (kind == "state_changed" and body.get("step_id") is None))
            if terminal and not db.execute(
                    "SELECT 1 FROM events WHERE task_id=? AND kind='evolution_event'", (task.id,)).fetchone():
                event = EvolutionEvent(id=f"{task.id}:terminal:1", task_id=task.id,
                    type="task_completed" if task.status == "succeeded" else "task_failed",
                    trace_until_seq=written.lastrowid, task_snapshot=task,
                    strategy_versions={**task.request.strategy_versions,
                        **{s.agent_id: s.response.strategy_version for s in task.steps if s.response}},
                    proposed_strategy_versions={s.agent_id: s.response.proposed_strategy_version
                        for s in task.steps if s.response and s.response.proposed_strategy_version})
                db.execute("INSERT INTO events(task_id, created_at, kind, body) VALUES (?, ?, ?, ?)",
                           (task.id, event.created_at, "evolution_event", event.model_dump_json()))

    def evolution_events(self, after_seq: int = 0, limit: int = 50):
        """进化模块自行保存消费游标；重复拉取可按稳定 event.id 幂等处理，不触发学习。"""
        with self.connect() as db:
            rows = db.execute("SELECT seq, body FROM events WHERE kind='evolution_event' AND seq>? ORDER BY seq LIMIT ?",
                              (after_seq, limit)).fetchall()
        return [{"seq": row[0], "event": json.loads(row[1])} for row in rows]

    def get(self, task_id: str) -> TaskRecord | None:
        with self.connect() as db:
            row = db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        return TaskRecord.model_validate_json(row[0]) if row else None

    def list_tasks(self):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM tasks ORDER BY rowid DESC LIMIT 50").fetchall()
        return [TaskRecord.model_validate_json(row[0]) for row in rows]

    def events(self, task_id: str):
        with self.connect() as db:
            rows = db.execute("SELECT seq, created_at, kind, body FROM events WHERE task_id=? ORDER BY seq",
                              (task_id,)).fetchall()
        return [dict(seq=r[0], created_at=r[1], kind=r[2], body=json.loads(r[3])) for r in rows]

    def recover_interrupted(self):
        """进程重启不假装自动续跑；把失去执行者的任务明确标为失败。"""
        with self.connect() as db:
            rows = db.execute("SELECT body FROM tasks").fetchall()
        for row in rows:
            task = TaskRecord.model_validate_json(row[0])
            if task.status in ("pending", "running"):
                task.status, task.error = "failed", "服务中断，任务未自动恢复，请重新提交"
                for step in task.steps:
                    if step.status in ("running", "retrying"):
                        step.status, step.error = "failed", task.error
                    elif step.status == "pending":
                        step.status = "skipped"
                self.save(task, "interrupted", error=task.error)
