"""Single durable dispatcher. SQLite is the queue; no in-memory submit gap."""
import logging
import os
import threading
from .conversations import Conversations, run_chat


class ProcessLease:
    def __init__(self, path):
        self.path = path

    def acquire(self):
        if getattr(self, 'file', None) is not None and not self.file.closed:
            raise RuntimeError('此实例已经持有数据库锁')
        self.file = open(str(self.path) + '.worker.lock', 'a+b')
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b'1'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('此数据库已有Worker运行，请只启动一个服务进程') from None

    def release(self):
        self.file.close()


class Worker:
    def __init__(self, settings, store, auth, runner):
        self.settings, self.store, self.auth, self.runner = settings, store, auth, runner
        self.stop_event = threading.Event()
        self.chats = Conversations(store)
        self.thread = threading.Thread(target=self.loop, daemon=True, name='durable-agent-worker')

    def start(self):
        self.settings.require_execution()
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()

    def execute(self, task):
        self.settings.require_execution()
        if not self.store.claim(task['id']):
            return
        task = self.store.get(task['id'])
        try:
            task['_tool_token'] = self.auth.issue_worker(task)
            self.store.finish(task['id'], result=self.runner.run(task))
        except Exception as exc:
            self.store.finish(task['id'], error=f'{type(exc).__name__}: {exc}'[:8000])
        finally:
            self.auth.revoke_worker(task['id'])

    def loop(self):
        self.settings.require_execution()
        while not self.stop_event.is_set():
            try:
                task = self.store.ready()
                if task:
                    self.execute(task)
                if self.stop_event.is_set():
                    break
                turn = self.chats.claim_next()
                if turn:
                    try:
                        turn['_chat_token'] = self.auth.issue_chat(turn['id'])
                        reply, provider, model = run_chat(self.settings, self.store, turn)
                        self.chats.finish(turn['id'], reply=reply, provider=provider, model=model)
                    except Exception as exc:
                        self.chats.finish(turn['id'], error=f'{type(exc).__name__}: {exc}'[:4000])
                    finally:
                        self.auth.revoke_chat(turn['id'])
                if not task and not turn:
                    self.stop_event.wait(.25)
            except Exception:
                # Do not write SDK exceptions or credentials to unrestricted console logs.
                logging.getLogger(__name__).error('Worker persistence failure; retrying queue poll')
                self.stop_event.wait(1)
