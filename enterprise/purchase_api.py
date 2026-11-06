from typing import Literal
from fastapi import Request, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from .procurement import Procurement, PRODUCTS, WAREHOUSES, DEPARTMENTS


class StrictInput(BaseModel):
    model_config=ConfigDict(extra='forbid')

class PlanEdit(StrictInput):
    revision:int=Field(ge=1)
    items:list[dict]=Field(min_length=1,max_length=100)

class PlanChoice(StrictInput):
    revision:int=Field(ge=1)
    choice:Literal['transfer','external','defer']

class DraftEdit(StrictInput):
    revision:int=Field(ge=1)
    body:dict

class Submit(StrictInput):
    revision:int=Field(ge=1)
    confirm:Literal[True]

class Decision(StrictInput):
    version:int=Field(ge=1)
    decision:Literal['approved','rejected']
    reason:str=Field(min_length=1,max_length=2000)

class AssistantPlanEdit(PlanEdit):
    plan_id:str

class AssistantPlanChoice(PlanChoice):
    plan_id:str


def mount_purchase(app,store):
    service=Procurement(store)

    def scoped_plan(body,request):
        identity=request.state.chat_identity
        with store.connection() as db:
            row=db.execute('''SELECT current.user_text FROM chat_turns current JOIN conversations c ON c.id=current.conversation_id
                JOIN replenishment_plans p ON p.id=? WHERE current.id=? AND p.owner_id=c.owner_id AND
                (p.task_id=c.task_id OR p.task_id IN (SELECT task_id FROM chat_turns WHERE conversation_id=c.id))''',(body.plan_id,identity['turn_id'])).fetchone()
            if not row:raise HTTPException(404,'方案不属于当前会话')
        return identity,row['user_text']

    @app.get('/api/purchase/meta')
    def meta(request:Request):
        return {'products':PRODUCTS,'warehouses':WAREHOUSES,'departments':DEPARTMENTS,'capabilities':service.capabilities(request.state.user['id'])}

    @app.get('/api/purchase/plans/{pid}')
    def plan(pid:str,request:Request):return service.plan(pid,request.state.user['id'])

    @app.patch('/api/purchase/plans/{pid}')
    def edit_plan(pid:str,body:PlanEdit,request:Request):return service.edit_plan(pid,request.state.user['id'],body.revision,body.items)

    @app.post('/api/purchase/plans/{pid}/choice')
    def choose(pid:str,body:PlanChoice,request:Request):return service.choose(pid,request.state.user['id'],body.revision,body.choice)

    @app.get('/api/purchase/requests')
    def requests(request:Request):return {'items':service.list_requests(request.state.user['id'])}

    @app.post('/api/purchase/requests',status_code=201)
    def create_draft(request:Request):return service.new_draft(request.state.user['id'])

    @app.get('/api/purchase/requests/{rid}')
    def get_request(rid:str,request:Request):return service.request(rid,request.state.user['id'])

    @app.patch('/api/purchase/requests/{rid}')
    def edit_request(rid:str,body:DraftEdit,request:Request):return service.edit_request(rid,request.state.user['id'],body.revision,body.body)

    @app.post('/api/purchase/requests/{rid}/submit')
    def submit(rid:str,body:Submit,request:Request):return service.submit(rid,request.state.user['id'],body.revision)

    @app.post('/api/purchase/requests/{rid}/decision')
    def decide(rid:str,body:Decision,request:Request):return service.decide(rid,request.state.user['id'],body.version,body.decision,body.reason)

    @app.post('/api/internal/assistant/plan')
    def assistant_plan(body:AssistantPlanEdit,request:Request):
        identity,_=scoped_plan(body,request)
        return service.edit_plan(body.plan_id,identity['owner_id'],body.revision,body.items,source='assistant')

    @app.post('/api/internal/assistant/plan-choice')
    def assistant_choice(body:AssistantPlanChoice,request:Request):
        import re
        identity,text=scoped_plan(body,request)
        label={'external':'外部采购','transfer':'内部调拨','defer':'暂不处理'}[body.choice]
        # Only explicit current-turn choice language; vague/negative questions use
        # the existing UI instead. This bridge never exposes formal submission.
        if not re.search(r'(?:选择|改为|采用|就用)\s*'+label,text) or re.search(r'(?:不(?:是|要)?|别)\s*(?:选择|改为|采用|就用)?\s*'+label,text) or re.search(r'是否|能否|如果|假如|？|\?',text):
            raise HTTPException(409,'请明确说“选择'+label+'”，或点击方案卡片；不会自动选择')
        return service.choose(body.plan_id,identity['owner_id'],body.revision,body.choice,source='assistant')
