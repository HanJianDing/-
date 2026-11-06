"""Purchase demand lifecycle. No order, payment, transfer or stock write operations."""
import json
import hashlib
from decimal import Decimal
from uuid import uuid4
from fastapi import HTTPException
from .store import now
from .sales import SalesQuery, query_sales
from .inventory import query_inventory, SNAPSHOT_DATE
from .security import redact

DEPARTMENTS = {'sales': '销售部', 'purchase': '采购部', 'finance': '财务部'}
PRODUCTS = {'A': {'name': '产品 A', 'unit': '件'}, 'B': {'name': '产品 B', 'unit': '件'}}
WAREHOUSES = {'east': '东部仓库', 'south': '南部仓库', 'north': '北部仓库'}


def encode(value):
    return json.dumps(redact(value), ensure_ascii=False, sort_keys=True)


def allocate_transfers(items, rows):
    balances={(r['region'],r['product']):max(0,r['available']-r['safety_stock']) for r in rows}
    for item in items:
        remaining=item['quantity'];candidates=[]
        for (warehouse,product),available in balances.items():
            if product!=item['product_id'] or warehouse==item['warehouse']:continue
            quantity=min(remaining,available)
            if quantity:
                candidates.append({'warehouse':warehouse,'quantity':quantity})
                balances[(warehouse,product)]-=quantity;remaining-=quantity
        item['transfer_candidates']=candidates;item['external_after_transfer']=remaining
    return items


def migrate(store):
    if store.existing_only:
        raise RuntimeError('验收模式禁止采购数据库迁移')
    with store.connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS departments(id TEXT PRIMARY KEY,name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS business_memberships(user_id TEXT REFERENCES users(id),department_id TEXT REFERENCES departments(id),duty TEXT NOT NULL CHECK(duty IN ('employee','supervisor')),PRIMARY KEY(user_id,department_id,duty));
        CREATE TABLE IF NOT EXISTS risk_policies(version INTEGER PRIMARY KEY,medium_above TEXT NOT NULL,high_above TEXT NOT NULL,active INTEGER NOT NULL,created_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_risk_policy ON risk_policies(active) WHERE active=1;
        CREATE TABLE IF NOT EXISTS risk_evaluations(task_id TEXT PRIMARY KEY REFERENCES tasks(id),payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS replenishment_plans(id TEXT PRIMARY KEY,task_id TEXT UNIQUE REFERENCES tasks(id),owner_id TEXT,revision INTEGER NOT NULL,selection TEXT NOT NULL DEFAULT 'undecided',payload_json TEXT NOT NULL,report TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS replenishment_versions(plan_id TEXT REFERENCES replenishment_plans(id),revision INTEGER,payload_json TEXT NOT NULL,actor TEXT,created_at TEXT NOT NULL,PRIMARY KEY(plan_id,revision));
        CREATE TABLE IF NOT EXISTS purchase_requests(id TEXT PRIMARY KEY,number TEXT UNIQUE NOT NULL,owner_id TEXT NOT NULL REFERENCES users(id),department_id TEXT NOT NULL REFERENCES departments(id),application_date TEXT NOT NULL,plan_id TEXT UNIQUE REFERENCES replenishment_plans(id),status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL DEFAULT 0,body_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS purchase_versions(request_id TEXT REFERENCES purchase_requests(id),version INTEGER,snapshot_json TEXT NOT NULL,snapshot_hash TEXT NOT NULL,submitted_at TEXT NOT NULL,PRIMARY KEY(request_id,version));
        CREATE TABLE IF NOT EXISTS business_approval_instances(id TEXT PRIMARY KEY,business_type TEXT NOT NULL,business_id TEXT NOT NULL,version INTEGER NOT NULL,round INTEGER NOT NULL,status TEXT NOT NULL,route_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(business_type,business_id,version));
        CREATE TABLE IF NOT EXISTS business_approval_steps(id TEXT PRIMARY KEY,instance_id TEXT NOT NULL REFERENCES business_approval_instances(id),ordinal INTEGER NOT NULL,department_id TEXT NOT NULL,status TEXT NOT NULL,reviewer TEXT,reason TEXT,decided_at TEXT,UNIQUE(instance_id,ordinal));
        CREATE TABLE IF NOT EXISTS business_events(id INTEGER PRIMARY KEY,business_type TEXT NOT NULL,business_id TEXT NOT NULL,version INTEGER,action TEXT NOT NULL,actor TEXT,detail_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS purchase_version_immutable_update BEFORE UPDATE ON purchase_versions BEGIN SELECT RAISE(ABORT,'submitted versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS purchase_version_immutable_delete BEFORE DELETE ON purchase_versions BEGIN SELECT RAISE(ABORT,'submitted versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS business_event_immutable_update BEFORE UPDATE ON business_events BEGIN SELECT RAISE(ABORT,'business history is append only'); END;
        CREATE TRIGGER IF NOT EXISTS business_event_immutable_delete BEFORE DELETE ON business_events BEGIN SELECT RAISE(ABORT,'business history is append only'); END;
        CREATE TRIGGER IF NOT EXISTS approval_decision_immutable BEFORE UPDATE ON business_approval_steps WHEN OLD.status IN ('approved','rejected') BEGIN SELECT RAISE(ABORT,'approval decisions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS approval_decision_no_delete BEFORE DELETE ON business_approval_steps BEGIN SELECT RAISE(ABORT,'approval history is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS approval_route_immutable BEFORE UPDATE OF business_type,business_id,version,round,route_json,created_at ON business_approval_instances BEGIN SELECT RAISE(ABORT,'approval route snapshot is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS approval_round_no_delete BEFORE DELETE ON business_approval_instances BEGIN SELECT RAISE(ABORT,'approval history is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS replenishment_version_no_update BEFORE UPDATE ON replenishment_versions BEGIN SELECT RAISE(ABORT,'proposal history is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS replenishment_version_no_delete BEFORE DELETE ON replenishment_versions BEGIN SELECT RAISE(ABORT,'proposal history is immutable'); END;
        ''')
        for code, name in DEPARTMENTS.items():
            db.execute('INSERT OR IGNORE INTO departments VALUES (?,?)', (code, name))
        if not db.execute('SELECT 1 FROM risk_policies').fetchone():
            # Initial confirmed configuration, not classification constants.
            db.execute('INSERT INTO risk_policies VALUES (1,?,?,1,?)', ('0.20', '0.50', now()))


class Procurement:
    def __init__(self, store):
        self.store = store

    def member(self, db, uid, department, duty):
        return bool(db.execute('SELECT 1 FROM business_memberships m JOIN users u ON u.id=m.user_id WHERE m.user_id=? AND m.department_id=? AND m.duty=? AND u.active=1 AND u.must_change=0', (uid, department, duty)).fetchone())

    def capabilities(self, uid):
        with self.store.connection() as db:
            return {'apply': self.member(db, uid, 'sales', 'employee'),
                    'initial': self.member(db, uid, 'sales', 'supervisor'),
                    'final': self.member(db, uid, 'purchase', 'supervisor')}

    def require_applicant(self, db, uid):
        if not self.member(db, uid, 'sales', 'employee'):
            raise HTTPException(403, '仅获授权的销售部员工可以发起或修改采购需求')

    def event(self, db, bid, version, action, actor, detail=None, kind='purchase_request'):
        db.execute('INSERT INTO business_events(business_type,business_id,version,action,actor,detail_json,created_at) VALUES (?,?,?,?,?,?,?)',
                   (kind,bid,version,action,actor,encode(detail or {}),now()))
        linked = db.execute('SELECT p.task_id FROM replenishment_plans p JOIN purchase_requests r ON r.plan_id=p.id WHERE r.id=?',(bid,)).fetchone() if kind=='purchase_request' else db.execute('SELECT task_id FROM replenishment_plans WHERE id=?',(bid,)).fetchone() if kind=='replenishment_plan' else None
        if linked:
            self.store._event(db,linked['task_id'],'purchase.'+action,'采购业务 · '+action,{'business_id':bid,'business_type':kind,'version':version},role='human' if actor else 'procurement')

    def evaluate(self, query):
        with self.store.connection() as db:
            rule = db.execute('SELECT * FROM risk_policies WHERE active=1').fetchone()
        if not rule:
            raise HTTPException(409, '风险阈值尚未配置')
        medium, high = Decimal(rule['medium_above']), Decimal(rule['high_above'])
        if not 0 <= medium < high <= 1:
            raise HTTPException(409, '风险阈值无效，禁止自动判断')
        inventory = query_inventory(SalesQuery(**query))
        rows=[]
        for row in inventory['rows']:
            valid=row['safety_stock']>0 and row['available']>=0
            ratio=Decimal(row['shortage'])/Decimal(row['safety_stock']) if valid else None
            level=None if ratio is None else ('normal' if ratio==0 else 'high' if ratio>high else 'medium' if ratio>medium else 'low')
            rows.append({**row,'ratio':str(ratio) if ratio is not None else None,'level':level})
        valid=bool(rows) and all(r['level'] is not None for r in rows)
        # Source candidates stay within the authorized query scope.
        balances={(r['region'],r['product']):max(0,r['available']-r['safety_stock']) for r in rows}
        items=[]
        for row in rows:
            if not row['shortage']:continue
            remaining=row['shortage'];candidates=[]
            for source in rows:
                key=(source['region'],source['product'])
                if source['product']!=row['product'] or source['region']==row['region']:continue
                quantity=min(remaining,balances[key])
                if quantity:
                    candidates.append({'warehouse':source['region'],'quantity':quantity})
                    balances[key]-=quantity;remaining-=quantity
            items.append({'product_id':row['product'],'product_name':PRODUCTS[row['product']]['name'],'unit':'件',
                          'warehouse':row['region'],'shortage':row['shortage'],'quantity':row['shortage'],
                          'transfer_candidates':candidates,'external_after_transfer':remaining})
        rank={'normal':0,'low':1,'medium':2,'high':3}
        return {'query':query,'source':inventory['source'],'snapshot_date':inventory['snapshot_date'],
                'policy_version':rule['version'],'thresholds':{'medium_above':str(medium),'high_above':str(high)},
                'valid':valid,'level':max((r['level'] for r in rows),key=lambda x:rank.get(x,-1)) if valid else None,
                'rows':rows,'items':items,'notice':'模拟数据；调拨候选未考虑物流成本/时效，不执行调拨。'}

    def prepare(self, task):
        with self.store.connection() as db:
            previous=db.execute('SELECT payload_json FROM risk_evaluations WHERE task_id=?',(task['id'],)).fetchone()
            if previous:return json.loads(previous[0])
        result=self.evaluate(task['query'])
        result['explicit']=task['analysis_type']=='procurement'
        result['trigger']=result['valid'] and (result['level']=='high' or result['explicit'])
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO risk_evaluations VALUES (?,?,?)',(task['id'],encode(result),now()))
        return result

    def save_plan(self, task, risk, report):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT id FROM replenishment_plans WHERE task_id=?',(task['id'],)).fetchone()
            if existing:return existing['id']
            pid=str(uuid4());payload={'risk':risk,'items':risk['items'],'query':task['query']}
            db.execute('INSERT INTO replenishment_plans VALUES (?,?,?,?,?,?,?,?,?)',(pid,task['id'],task['owner_id'],1,'undecided',encode(payload),redact(report),now(),now()))
            db.execute('INSERT INTO replenishment_versions VALUES (?,?,?,?,?)',(pid,1,encode(payload),None,now()))
            self.event(db,pid,1,'plan.created',None,kind='replenishment_plan')
            return pid

    def plan(self, pid, uid):
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM replenishment_plans WHERE id=? AND owner_id=?',(pid,uid)).fetchone()
            if not row:raise HTTPException(404,'方案不存在或无权读取')
            result=dict(row);result['payload']=json.loads(result.pop('payload_json'))
            result['versions']=[dict(r) for r in db.execute('SELECT * FROM replenishment_versions WHERE plan_id=? ORDER BY revision',(pid,))]
            request=db.execute('SELECT id,number,status,version,body_json FROM purchase_requests WHERE plan_id=?',(pid,)).fetchone()
            result['request']=dict(request) if request else None
            if result['request']:
                # Include immutable submissions and reviewer history so follow-up
                # answers distinguish edited proposals from the submitted demand.
                result['request']=self.request(result['request']['id'],uid)
            return result

    def edit_plan(self,pid,uid,revision,items,source='web'):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');self.require_applicant(db,uid)
            row=db.execute('SELECT * FROM replenishment_plans WHERE id=? AND owner_id=?',(pid,uid)).fetchone()
            if not row:raise HTTPException(404,'方案不存在')
            if row['revision']!=revision:raise HTTPException(409,'方案已更新，请刷新后修改')
            payload=json.loads(row['payload_json'])
            known={(r['product_id'],r['warehouse']):r for r in payload['items']}
            if not items:raise HTTPException(422,'至少保留一条方案明细')
            output=[];seen=set()
            for item in items:
                if not isinstance(item.get('product_id'),str) or not isinstance(item.get('warehouse'),str):raise HTTPException(422,'商品和仓库编号必须是文本')
                key=(item.get('product_id'),item.get('warehouse'))
                if key not in known or key in seen or type(item.get('quantity')) is not int or not 0<item['quantity']<=1000000:
                    raise HTTPException(422,'商品/仓库须来自此方案且不重复，数量须为正整数')
                seen.add(key);output.append({**known[key],'quantity':item['quantity']})
            payload['items']=output
            allocate_transfers(payload['items'],payload['risk']['rows'])
            db.execute('UPDATE replenishment_plans SET revision=revision+1,payload_json=?,updated_at=? WHERE id=?',(encode(payload),now(),pid))
            db.execute('INSERT INTO replenishment_versions VALUES (?,?,?,?,?)',(pid,revision+1,encode(payload),uid,now()))
            self.event(db,pid,revision+1,'plan.edited',uid,{'source':source},kind='replenishment_plan')
        return self.plan(pid,uid)

    def choose(self,pid,uid,revision,choice,source='web'):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');self.require_applicant(db,uid)
            row=db.execute('SELECT * FROM replenishment_plans WHERE id=? AND owner_id=?',(pid,uid)).fetchone()
            if not row:raise HTTPException(404,'方案不存在')
            if row['revision']!=revision:raise HTTPException(409,'方案已更新，请重新选择')
            if choice not in {'transfer','external','defer'}:raise HTTPException(422,'无效方案选择')
            old=db.execute('SELECT id FROM purchase_requests WHERE plan_id=?',(pid,)).fetchone()
            if old and choice!='external':raise HTTPException(409,'已生成采购草稿，选择不会撤销既有申请，请在采购页面处理')
            db.execute('UPDATE replenishment_plans SET selection=?,updated_at=? WHERE id=?',(choice,now(),pid))
            self.event(db,pid,revision,'plan.selected',uid,{'choice':choice,'source':source},kind='replenishment_plan')
            if choice=='external' and not old:
                payload=json.loads(row['payload_json']);rid=str(uuid4())
                body={'reason':'依据库存缺口提出补货需求','supplier':None,'expected_arrival':None,'notes':None,
                      'items':[{**r,'unit_price_cents':None} for r in payload['items']]}
                body=self.validate_body(body,False)
                db.execute('INSERT INTO purchase_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (rid,'PR-'+now()[:10].replace('-','')+'-'+rid[:8].upper(),uid,'sales',now()[:10],pid,'draft',1,0,encode(body),now(),now()))
                self.event(db,rid,0,'draft.created',uid,{'plan_id':pid})
        return self.plan(pid,uid)

    def validate_body(self, body, submitting):
        from datetime import date
        if not isinstance(body,dict) or not isinstance(body.get('reason',''),str) or not isinstance(body.get('items',[]),list):raise HTTPException(422,'申请内容格式无效')
        if any(body.get(k) is not None and not isinstance(body[k],str) for k in ['supplier','notes','expected_arrival']):raise HTTPException(422,'辅助信息必须为文本或为空')
        reason=body.get('reason','').strip();items=body.get('items',[])
        if len(reason)>2000 or not 0<=len(items)<=100 or (submitting and (not reason or not items)):raise HTTPException(422,'必须有商品明细和有效申请原因')
        output=[];seen=set();warnings=[];total=0;complete=True
        current=query_inventory(SalesQuery())['rows']
        for item in items:
            if not isinstance(item,dict):raise HTTPException(422,'商品明细格式无效')
            product=item.get('product_id');warehouse=item.get('warehouse');quantity=item.get('quantity')
            if not isinstance(product,str) or not isinstance(warehouse,str):raise HTTPException(422,'商品和仓库编号必须是文本')
            if product not in PRODUCTS or warehouse not in WAREHOUSES or type(quantity) is not int or not 0<quantity<=1000000:
                raise HTTPException(422,'商品、仓库或数量无效（数量必须为正整数）')
            if item.get('unit')!=PRODUCTS[product]['unit']:raise HTTPException(422,'计量单位与商品不一致')
            if (product,warehouse) in seen:raise HTTPException(422,'同商品同仓库请合并为一条明细')
            seen.add((product,warehouse))
            row=next((r for r in current if r['product']==product and r['region']==warehouse),None)
            if not row:raise HTTPException(422,'目标仓库没有此商品库存数据，不能伪造缺口')
            price=item.get('unit_price_cents')
            if price is not None and (type(price) is not int or not 0<=price<=10000000):raise HTTPException(422,'预计单价必须是非负整数分（不超过十万元）或为空')
            amount=price*quantity if price is not None else None
            if amount is None:complete=False
            else:total+=amount
            shortage=row['shortage']
            if quantity<shortage:warnings.append(f'{PRODUCTS[product]["name"]} / {WAREHOUSES[warehouse]} 仍有 {shortage-quantity} 件缺口未覆盖')
            if quantity>shortage:warnings.append(f'{PRODUCTS[product]["name"]} / {WAREHOUSES[warehouse]} 超出当前缺口 {quantity-shortage} 件')
            output.append({'product_id':product,'product_name':PRODUCTS[product]['name'],'quantity':quantity,'unit':PRODUCTS[product]['unit'],
                           'warehouse':warehouse,'shortage':shortage,'unit_price_cents':price,'amount_cents':amount})
        arrival=body.get('expected_arrival') or None
        if arrival:
            try:date.fromisoformat(arrival)
            except ValueError:raise HTTPException(422,'期望到货日期无效') from None
        supplier=body.get('supplier') or None;notes=body.get('notes') or None
        if (supplier and len(supplier)>200) or (notes and len(notes)>2000):raise HTTPException(422,'辅助信息过长')
        return redact({'reason':reason,'supplier':supplier,'expected_arrival':arrival,'notes':notes,'items':output,
                       'expected_total_cents':total if complete and output else None,'known_subtotal_cents':total,'warnings':warnings,'inventory_snapshot_date':SNAPSHOT_DATE})

    def visible(self,db,row,uid):
        if row['owner_id']==uid:return True
        supervisor=self.member(db,uid,'sales','supervisor') or self.member(db,uid,'purchase','supervisor')
        return supervisor and bool(db.execute('SELECT 1 FROM purchase_versions WHERE request_id=? AND version=?',
                                              (row['id'],row['version'])).fetchone())

    def new_draft(self,uid):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');self.require_applicant(db,uid)
            rid=str(uuid4());body=self.validate_body({'items':[],'reason':''},False)
            db.execute('INSERT INTO purchase_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (rid,'PR-'+now()[:10].replace('-','')+'-'+rid[:8].upper(),uid,'sales',now()[:10],None,'draft',1,0,encode(body),now(),now()))
            self.event(db,rid,0,'draft.created',uid)
        return self.request(rid,uid)

    def request(self,rid,uid):
        with self.store.connection() as db:
            # Keep the working row, immutable versions and approval history in one
            # read snapshot, even when an applicant submits concurrently.
            db.execute('BEGIN')
            row=db.execute('SELECT r.*,u.username FROM purchase_requests r JOIN users u ON u.id=r.owner_id WHERE r.id=?',(rid,)).fetchone()
            if not row or not self.visible(db,row,uid):raise HTTPException(404,'采购申请不存在或无权读取')
            result=dict(row);result['body']=json.loads(result.pop('body_json'))
            result['versions']=[{**dict(r),'snapshot':json.loads(r['snapshot_json'])} for r in db.execute('SELECT * FROM purchase_versions WHERE request_id=? ORDER BY version',(rid,))]
            rounds=[]
            for r in db.execute("SELECT * FROM business_approval_instances WHERE business_type='purchase_request' AND business_id=? ORDER BY round",(rid,)):
                rounds.append({**dict(r),'steps':[dict(s) for s in db.execute('SELECT s.*,u.username FROM business_approval_steps s LEFT JOIN users u ON u.id=s.reviewer WHERE s.instance_id=? ORDER BY ordinal',(r['id'],))]})
            result['rounds']=rounds
            result['events']=[dict(r) for r in db.execute("SELECT * FROM business_events WHERE business_type='purchase_request' AND business_id=? ORDER BY id",(rid,))]
            if row['owner_id']!=uid:
                # Supervisors read submissions, never the applicant's working copy.
                latest=result['versions'][-1]
                result['body']=latest['snapshot']['body']
                result['events']=[e for e in result['events'] if e['action'] in
                                  {'submitted','initial.approved','initial.rejected','final.approved','final.rejected'}]
                if row['status'] in {'draft','rejected'}:
                    # A rejected submission remains rejected to reviewers while
                    # the applicant privately prepares its replacement.
                    result['status']=result['rounds'][-1]['status']
                    last_event=result['events'][-1]
                    result['updated_at']=last_event['created_at']
                    submitted=next(e for e in reversed(result['events']) if e['action']=='submitted')
                    result['revision']=json.loads(submitted['detail_json'])['revision']+1+sum(
                        s['status'] in {'approved','rejected'} for s in result['rounds'][-1]['steps'])
            return result

    def list_requests(self,uid):
        with self.store.connection() as db:
            rows=db.execute('SELECT * FROM purchase_requests ORDER BY created_at DESC').fetchall()
            ids=[r['id'] for r in rows if self.visible(db,r,uid)]
        return [self.request(rid,uid) for rid in ids]

    def edit_request(self,rid,uid,revision,body):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');self.require_applicant(db,uid)
            row=db.execute('SELECT * FROM purchase_requests WHERE id=? AND owner_id=?',(rid,uid)).fetchone()
            if not row:raise HTTPException(404,'采购申请不存在')
            if row['revision']!=revision or row['status'] not in {'draft','rejected'}:raise HTTPException(409,'仅可编辑当前草稿或被驳回申请；请刷新版本')
            checked=self.validate_body(body,False)
            db.execute("UPDATE purchase_requests SET status='draft',revision=revision+1,body_json=?,updated_at=? WHERE id=?",(encode(checked),now(),rid))
            self.event(db,rid,row['version'],'draft.saved',uid,{'revision':revision+1,'body':checked})
        return self.request(rid,uid)

    def submit(self,rid,uid,revision):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');self.require_applicant(db,uid)
            row=db.execute('SELECT * FROM purchase_requests WHERE id=? AND owner_id=?',(rid,uid)).fetchone()
            if not row:raise HTTPException(404,'采购申请不存在')
            if row['status']!='draft' or row['revision']!=revision:raise HTTPException(409,'请保存最新草稿，不能重复提交')
            body=self.validate_body(json.loads(row['body_json']),True)
            # Resolve route on backend; require two distinct eligible people, neither applicant.
            eligible={}
            for dept in ['sales','purchase']:
                eligible[dept]=[r[0] for r in db.execute("SELECT m.user_id FROM business_memberships m JOIN users u ON u.id=m.user_id WHERE m.department_id=? AND m.duty='supervisor' AND u.active=1 AND u.must_change=0 AND m.user_id!=?",(dept,uid))]
            if not any(a!=b for a in eligible['sales'] for b in eligible['purchase']):raise HTTPException(409,'需要两位不同且不是申请人的有效主管完成两级审批')
            version=row['version']+1;iid=str(uuid4())
            snapshot={'number':row['number'],'applicant_id':uid,'department_id':'sales','application_date':row['application_date'],'body':body,'version':version}
            text=encode(snapshot)
            db.execute('INSERT INTO purchase_versions VALUES (?,?,?,?,?)',(rid,version,text,hashlib.sha256(text.encode()).hexdigest(),now()))
            route={'version':1,'stages':['sales','purchase'],'eligible':eligible,'distinct_reviewers':True}
            db.execute('INSERT INTO business_approval_instances VALUES (?,?,?,?,?,?,?,?)',(iid,'purchase_request',rid,version,version,'pending',encode(route),now()))
            for ordinal,dept in enumerate(['sales','purchase'],1):
                db.execute('INSERT INTO business_approval_steps VALUES (?,?,?,?,?,?,?,?)',(str(uuid4()),iid,ordinal,dept,'pending' if ordinal==1 else 'blocked',None,None,None))
            db.execute("UPDATE purchase_requests SET status='pending_sales',version=?,revision=revision+1,body_json=?,updated_at=? WHERE id=?",(version,encode(body),now(),rid))
            self.event(db,rid,version,'submitted',uid,{'revision':revision,'instance_id':iid,'snapshot_hash':hashlib.sha256(text.encode()).hexdigest()})
        return self.request(rid,uid)

    def decide(self,rid,uid,version,decision,reason):
        if decision not in {'approved','rejected'} or not reason.strip() or len(reason)>2000:raise HTTPException(422,'必须选择批准或驳回并填写意见')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM purchase_requests WHERE id=?',(rid,)).fetchone()
            if not row or not self.visible(db,row,uid):raise HTTPException(404,'采购申请不存在')
            from .auth import require_independent_reviewer
            require_independent_reviewer(row['owner_id'],uid)
            if row['version']!=version or row['status'] not in {'pending_sales','pending_purchase'}:raise HTTPException(409,'申请版本或审批状态已变更')
            dept='sales' if row['status']=='pending_sales' else 'purchase';ordinal=1 if dept=='sales' else 2
            if not self.member(db,uid,dept,'supervisor'):raise HTTPException(403,'没有当前节点的采购审批权限')
            instance=db.execute("SELECT * FROM business_approval_instances WHERE business_type='purchase_request' AND business_id=? AND version=?",(rid,version)).fetchone()
            if uid not in json.loads(instance['route_json'])['eligible'][dept]:raise HTTPException(403,'不在本轮审批路由中')
            if db.execute('SELECT 1 FROM business_approval_steps WHERE instance_id=? AND reviewer=?',(instance['id'],uid)).fetchone():raise HTTPException(403,'同一人不能同时完成初审和终审')
            if ordinal==1 and decision=='approved' and not any(other!=uid and self.member(db,other,'purchase','supervisor') for other in json.loads(instance['route_json'])['eligible']['purchase']):
                raise HTTPException(409,'本轮没有可由另一人完成终审的有效主管，不能由当前账号初审通过')
            changed=db.execute("UPDATE business_approval_steps SET status=?,reviewer=?,reason=?,decided_at=? WHERE instance_id=? AND ordinal=? AND status='pending'",(decision,uid,redact(reason.strip()),now(),instance['id'],ordinal)).rowcount
            if changed!=1:raise HTTPException(409,'审批节点已处理')
            status='rejected' if decision=='rejected' else 'pending_purchase' if ordinal==1 else 'approved'
            if status=='pending_purchase':db.execute("UPDATE business_approval_steps SET status='pending' WHERE instance_id=? AND ordinal=2 AND status='blocked'",(instance['id'],))
            else:db.execute('UPDATE business_approval_instances SET status=? WHERE id=?',(status,instance['id']))
            db.execute('UPDATE purchase_requests SET status=?,revision=revision+1,updated_at=? WHERE id=?',(status,now(),rid))
            self.event(db,rid,version,'initial.'+decision if ordinal==1 else 'final.'+decision,uid,{'reason':reason,'instance_id':instance['id']})
        return self.request(rid,uid)
