from fastapi import APIRouter, HTTPException

from app.db import get_connection

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("/{task_id}")
def get_task(task_id: str):
    with get_connection() as conn:
        row = conn.execute('SELECT * FROM "Task" WHERE id = ?', (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "任务不存在")
    return dict(row)
