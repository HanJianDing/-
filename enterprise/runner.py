import json
import os
import sys
from pathlib import Path
from .settings import ROOT, Settings, load_env
from .events import EventRecorder


class HarnessRunner:
    def __init__(self, settings: Settings, store=None):
        self.settings = settings
        self.store = store

    def run(self, task: dict) -> dict:
        self.settings.require_execution()
        if not self.settings.isolated:
            load_env()
        # Use the SDK from the inspected checkout, without modifying or repackaging it.
        source = Path(os.getenv("HARNESS_SOURCE", str(ROOT.parent / "deepseek-harness-master")))
        sdk = source / "python" / "sdk" / "src"
        if not sdk.exists():
            raise RuntimeError("HARNESS_SOURCE does not contain the Python SDK")
        if str(sdk) not in sys.path:
            sys.path.insert(0, str(sdk))
        from deepseek_harness import DeepSeekHarness

        patch = ROOT / "config" / "enterprise.patch.json"
        mock = self.settings.mode == "mock"
        patches = (str(patch),) + ((str(ROOT / "config" / "mock.patch.json"),) if mock else ())
        if not mock and not os.getenv("DEEPSEEK_API_KEY"):
            raise RuntimeError("DEEPSEEK_API_KEY is missing")
        trace_dir = self.settings.trace_dir or ROOT / "data" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace = trace_dir / f"{task['id']}.jsonl"
        analysis_type = task.get('analysis_type', 'sales')
        from .procurement import Procurement
        procurement = Procurement(self.store) if self.store else None
        risk = procurement.prepare(task) if procurement and analysis_type != 'sales' else None
        context_file = trace_dir / f"{task['id']}-procurement.json"
        context_file.write_text(json.dumps(risk, ensure_ascii=False), encoding='utf-8')
        checkpoints = self.store.checkpoints(task['id']) if self.store else {}
        checkpoint_file = trace_dir / f"{task['id']}-attempt-{task.get('attempt', 1)}-checkpoints.json"
        checkpoint_file.write_text(json.dumps(checkpoints, ensure_ascii=False), encoding='utf-8')
        with DeepSeekHarness(
            dsh_bin=str(ROOT / "node_modules" / ".bin" / ("dsh.cmd" if os.name == "nt" else "dsh")),
            profile="sdk-minimal", patches=patches + self.settings.extra_patches, dsh_home=str(self.settings.home),
            cwd=str(ROOT), provider="enterprise-test" if mock else "deepseek-official",
            model="deterministic" if mock else os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            max_tokens=4096 if analysis_type == 'sales' else 16384,
            initialize_timeout_seconds=90, request_timeout_seconds=360,
            shutdown_timeout_seconds=15,
            env={"SALES_API_URL": self.settings.sales_url, "ENTERPRISE_TRACE": str(trace), 'ENTERPRISE_ANALYSIS_TYPE': analysis_type,
                 'ENTERPRISE_CHECKPOINTS': str(checkpoint_file), 'ENTERPRISE_PROCUREMENT_CONTEXT': str(context_file), 'ENTERPRISE_TOOL_TOKEN': task.get('_tool_token', '')},
        ) as harness:
            instruction = {'sales': '只委派经营分析子Agent。', 'inventory': '只委派库存分析子Agent。',
                           'combined': '分别委派经营分析和库存分析两个子Agent，然后综合两份报告，给出联合经营建议。',
                           'auto': '按用户问题选择经营分析、库存分析或两者，必须委派至少一个子Agent。',
                           'procurement': '委派库存分析；工具会按程序门控继续委派采购专业子Agent。汇总补货方案并给出内部调拨、外部采购、暂不处理三个选项；不执行业务。'}[analysis_type]
            if risk:
                instruction += '\n程序计算的风险信息：' + json.dumps(risk, ensure_ascii=False) + '\n风险以程序结果为准。中风险只提示风险；高风险库存工具会返回采购建议。没有执行任何采购或调拨。'
            outcome = harness.run(instruction + '查询参数原样传递：' + json.dumps(task['query'], ensure_ascii=False)
                                  + '\n业务问题：' + task.get('question', ''), session_id=task['session_id'],
                                  on_notification=EventRecorder(self.store, task) if self.store else None)
        if outcome.finish_reason != "completed":
            raise RuntimeError(f"Harness turn did not complete: {outcome.finish_reason}")
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else []
        delegated = [e for e in events if e["event"] == "child_completed"]
        if (not delegated and not checkpoints) or not outcome.final_response.strip():
            raise RuntimeError("incomplete chain: delegation, sales query, or final response missing")
        children_by_role = dict(checkpoints)
        for child in delegated:
            domain = child.get('domain', 'sales')
            if domain == 'procurement':
                data = {'query': task['query']}
                for needed in ['sales', 'inventory']:
                    queries = [e for e in events if e['event'] == needed + '_queried' and e['session_id'] == child['child_session_id']]
                    if not queries or any(e[needed]['query'] != task['query'] for e in queries):
                        raise RuntimeError('Procurement specialist must query both sales and inventory within scope')
                    data[needed] = queries[-1][needed]
                children_by_role[domain] = {'role':domain,'session_id':child['child_session_id'],'analysis':child['analysis'],'data':data}
                continue
            queries = [e for e in events if e['event'] == domain + '_queried' and e['session_id'] == child['child_session_id']]
            if not queries or any(e[domain]['query'] != task['query'] for e in queries):
                raise RuntimeError('missing child query or query scope mismatch')
            children_by_role[domain] = {'role': domain, 'session_id': child['child_session_id'], 'analysis': child['analysis'], 'data': queries[-1][domain]}
        children = list(children_by_role.values())
        if self.store:
            for e in events:
                if e['event'] == 'checkpoint_reused' and e['session_id'] == task['session_id']:
                    self.store.add_event(task['id'], 'checkpoint.reused', '复用已完成专业分析',
                        {'domain': e['domain'], 'child_session_id': e['child_session_id']}, role=e['domain'],
                        event_key=f"reused:{task.get('attempt', 1)}:{e['domain']}")
        required = {'sales': {'sales'}, 'inventory': {'inventory'}, 'combined': {'sales', 'inventory'}, 'auto': set(), 'procurement': {'inventory', 'procurement'}}[analysis_type]
        if risk and risk['trigger'] and 'inventory' in children_by_role:
            required.add('procurement')
        if not required.issubset({c['role'] for c in children}):
            raise RuntimeError('required specialist did not complete')
        result = {'summary': outcome.final_response, 'children': children, 'analysis_type': analysis_type,
                  'trace_file': str(trace), 'mode': self.settings.mode,
                  'child_session_id': children[0]['session_id'], 'child_analysis': children[0]['analysis']}
        for child in children:
            result[child['role']] = child['data']
        if risk:
            result['risk'] = risk
        if 'procurement' in children_by_role and procurement:
            result['procurement_plan_id'] = procurement.save_plan(task, risk, children_by_role['procurement']['analysis'])
        return result
