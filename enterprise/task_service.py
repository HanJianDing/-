"""One task submission transaction for the Web form and assistant tool."""
from typing import Literal
from fastapi import HTTPException
from pydantic import Field, model_validator
from .sales import SalesQuery
from .auth import require_role


class TaskRequest(SalesQuery):
    analysis_type: Literal['sales', 'inventory', 'combined', 'auto', 'procurement'] = 'sales'
    question: str = Field(default='', max_length=2000)

    @model_validator(mode='after')
    def auto_needs_question(self):
        if self.analysis_type == 'auto' and not self.question.strip():
            raise ValueError('自动分析需要填写问题')
        return self


def submit_task(store, settings, owner_id, body, turn_id=None):
    settings.require_execution()
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        user = db.execute('SELECT * FROM users WHERE id=? AND active=1 AND must_change=0', (owner_id,)).fetchone()
        if not user:
            raise HTTPException(403, '账号已失效，请重新登录')
        require_role(user, 'admin', 'analyst')
        from .procurement import Procurement
        # Explicitly provisioned sales employees may run read-only analysis. Legacy
        # analysts retain their existing analysis-task approval gate.
        sales_employee=Procurement(store).member(db,owner_id,'sales','employee')
        if body.analysis_type=='procurement' and not sales_employee:
            raise HTTPException(403,'当前账号没有发起采购需求分析的业务权限')
        if turn_id:
            turn = db.execute('SELECT t.* FROM chat_turns t JOIN conversations c ON c.id=t.conversation_id WHERE t.id=? AND c.owner_id=? AND c.deleted_at IS NULL AND t.status=\'running\'', (turn_id, owner_id)).fetchone()
            if not turn:
                raise HTTPException(409, '会话已删除或当前轮次已结束')
            # One durable task per user turn, including HTTP retries and model retries.
            if turn['task_id']:
                return store.get(turn['task_id'])
        tid = store.create_in_transaction(db, body.model_dump(mode='json', exclude={'analysis_type', 'question'}),
                                         settings.mode, body.analysis_type, body.question, owner_id,
                                         'pending' if user['role'] == 'analyst' and not sales_employee else 'approved', 'assistant' if turn_id else 'manual')
        if turn_id:
            db.execute('UPDATE chat_turns SET task_id=? WHERE id=?', (tid, turn_id))
            store._event(db, tid, 'assistant.submitted', 'AI助手已提交任务', {'source': 'assistant'})
    return store.get(tid)
