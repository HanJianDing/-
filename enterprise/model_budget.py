"""Local, authenticated Messages gateway. Never records credentials or prompts."""
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx


class ModelBudget:
    def __init__(self, root, key, limit=20, calls=150):
        self.root, self.key, self.limit, self.calls = root, key, limit, calls
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.path = root/'model-usage.json'
        self.rows = json.loads(self.path.read_text()) if self.path.exists() else []

    def save(self):
        tmp=self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.rows,indent=2),encoding='utf-8')
        tmp.replace(self.path)

    def request(self, body, emit=None):
        data=json.loads(body)
        if data.get('model')!='deepseek-flash' or not 1<=data.get('max_tokens',0)<=16384:
            return 400,b'{"error":{"type":"invalid_request_error","message":"Budget model/token restriction"}}'
        data['max_tokens']=min(data['max_tokens'],4096)
        body=json.dumps(data,ensure_ascii=False).encode()
        # Deliberately pessimistic UTF-8-byte input bound plus protocol allowance;
        # unknown/failed calls retain the entire reservation, never assume free.
        reserve=((len(body)*2+10000)*2+data['max_tokens']*8)/1_000_000
        with self.lock:
            if len(self.rows)>=self.calls or sum(x['charged_upper_cny'] for x in self.rows)+reserve>self.limit:
                return 429,b'{"error":{"type":"rate_limit_error","message":"Local validation budget exhausted"}}'
            row={'call':len(self.rows)+1,'charged_upper_cny':reserve,'state':'reserved'}
            self.rows.append(row);self.save()
        try:
            with httpx.Client(timeout=180,trust_env=False) as c:
                headers={'x-api-key':self.key,'anthropic-version':'2023-06-01','content-type':'application/json'}
                url='https://api.deepseek.com/anthropic/v1/messages'
                if emit:
                    started=time.monotonic();chunks=[]
                    with c.stream('POST',url,content=body,headers=headers) as upstream:
                        emit(upstream.status_code,b'')
                        for chunk in upstream.iter_bytes():
                            if time.monotonic()-started>150:raise TimeoutError('Model request deadline')
                            chunks.append(chunk);emit(None,chunk)
                        response=httpx.Response(upstream.status_code,content=b''.join(chunks))
                else:
                    response=c.post(url,content=body,headers=headers)
            usage={}
            if response.status_code==200:
                for line in response.text.splitlines():
                    if line.startswith('data: '):
                        try:
                            event=json.loads(line[6:]);usage.update(event.get('message',{}).get('usage',{}));usage.update(event.get('usage',{}))
                        except ValueError:pass
                if not data.get('stream'):
                    usage=response.json().get('usage',{})
            with self.lock:
                row.update(status=response.status_code,state='complete',usage=usage)
                if 'output_tokens' in usage and 'input_tokens' in usage:
                    estimate=(usage['input_tokens']*2+usage.get('cache_creation_input_tokens',0)*2+
                              usage.get('cache_read_input_tokens',0)*.04+usage['output_tokens']*8)/1_000_000
                    row.update(charged_upper_cny=max(estimate,0),estimated_peak_cny=estimate)
                self.save()
            return response.status_code,response.content
        except Exception:
            with self.lock:row['state']='unknown_failure';self.save()
            return 502,b'{"error":{"type":"api_error","message":"Upstream request failed; reservation retained"}}'

    def start(self):
        budget=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                if self.path!='/v1/messages' or self.headers.get('x-api-key')!=budget.token:
                    self.send_error(403);return
                length=int(self.headers.get('content-length','0'))
                if length>2_000_000:self.send_error(413);return
                sent=[False]
                def emit(status,chunk):
                    if status is not None:
                        self.send_response(status);self.send_header('Content-Type','text/event-stream' if status==200 else 'application/json');self.end_headers();sent[0]=True
                    if chunk:self.wfile.write(chunk);self.wfile.flush()
                status,body=budget.request(self.rfile.read(length),emit)
                if sent[0]:return
                self.send_response(status)
                self.send_header('Content-Type','text/event-stream' if status==200 else 'application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        return 'http://127.0.0.1:'+str(self.server.server_port)

    def stop(self):self.server.shutdown();self.server.server_close();self.thread.join()
