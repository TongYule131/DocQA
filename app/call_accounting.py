"""确定未发送才释放预算；失败和不确定请求永远不退回。"""
from datetime import datetime, timezone

TABLES = {'kb_task_calls': ('kb_tasks', 'task_id', 'finished_at'),
          'question_calls': ('question_jobs', 'job_id', 'settled_at'),
          'analysis_calls': ('analysis_jobs', 'job_id', 'settled_at')}


def settle_not_sent(db, table, job_id, sequence):
    jobs, foreign_key, settled = TABLES[table]
    changed = db.execute(f"UPDATE {table} SET status='failed',error_code='not_sent',delivery_status='not_sent',{settled}=? "
                         f"WHERE {foreign_key}=? AND sequence_no=? AND status='intent'",
                         (datetime.now(timezone.utc).isoformat(), job_id, sequence)).rowcount
    if changed:
        db.execute(f'UPDATE {jobs} SET requests_used=MAX(0,requests_used-1) WHERE id=?', (job_id,))
    return bool(changed)
