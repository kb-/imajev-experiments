import asyncio
from dataclasses import replace
from types import SimpleNamespace
import threading
import pytest
from app.config import Config
from app.inference.service_manager import ServiceManager
from scripts.serve_local import BusyGuard


@pytest.mark.parametrize('path',['/v1/systemone','/v1/coach'])
def test_gpu_lock_covers_both_endpoints(path):
    state=SimpleNamespace(serving=False,lock=threading.Lock()); state.lock.acquire(); sent=[]
    async def send(x): sent.append(x)
    async def downstream(*_): pytest.fail('Overlapping inference')
    asyncio.run(BusyGuard(downstream,state)({'type':'http','method':'POST','path':path},None,send))
    assert sent[0]['status']==409


def test_external_start_and_shutdown_never_launch(monkeypatch):
    monkeypatch.setattr('app.inference.service_manager.subprocess.Popen',lambda *_ ,**__: pytest.fail('External process managed'))
    manager=ServiceManager(replace(Config(),external_inference=True)); manager.start(); manager.shutdown()
    assert manager.process is None


def test_conflict_does_not_attach_or_kill(monkeypatch):
    class Probe:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def connect_ex(self,_): return 0
    monkeypatch.setattr('app.inference.service_manager.socket.socket',Probe)
    monkeypatch.setattr('app.inference.service_manager.subprocess.Popen',lambda *_ ,**__: pytest.fail('Launched on occupied port'))
    with pytest.raises(RuntimeError,match='occupied'): ServiceManager(Config()).start()


def test_owned_process_waits_for_exit_and_detects_failure(monkeypatch):
    trace=[]
    process=SimpleNamespace(pid=12345,poll=lambda:None,wait=lambda **_:trace.append('wait'))
    monkeypatch.setattr('app.inference.service_manager.os.killpg',lambda pid,signal:trace.append(('kill',pid)))
    manager=ServiceManager(Config()); manager.process=process; manager.stop()
    assert trace==[('kill',12345),'wait'] and manager.process is None
    manager.process=SimpleNamespace(poll=lambda:2)
    with pytest.raises(RuntimeError,match='child exited'): manager.check()


def fake_ollama(monkeypatch, unrelated=False, failed=False, unload_failure=False):
    trace=[]; loaded=[]
    class Response:
        def __init__(self,raw): self.raw=raw
        def raise_for_status(self): pass
        def json(self): return self.raw
    class Transport:
        def __init__(self,**_): pass
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def get(self,url,**_):
            key=url.rsplit('/',1)[-1]; trace.append(key)
            if key=='tags': return Response({'models':[{'name':'coach:latest'}]})
            return Response({'models':[{'name':m} for m in (['other'] if unrelated else loaded)]})
        def post(self,url,json):
            key=url.rsplit('/',1)[-1]; trace.append(key)
            assert json['keep_alive']==0
            if key=='generate':
                if unload_failure: raise RuntimeError('Unload failed')
                loaded.clear(); return Response({})
            loaded.append('coach:latest')
            if failed: raise RuntimeError('Generation failed')
            return Response({'message':{'content':'{"strategy":["win","learn"]}'},'done_reason':'stop','done':True})
    monkeypatch.setattr('app.inference.service_manager.httpx.Client',Transport)
    manager=ServiceManager(replace(Config(),coach_backend='ollama',coach_model='coach'))
    manager.stop=lambda:trace.append('stop')
    def start():
        assert not manager.swap_pending
        trace.append('start')
    manager.start=start
    return manager,trace


def test_ollama_pipeline_swap_order(monkeypatch):
    manager,trace=fake_ollama(monkeypatch)
    result=manager.ollama(lambda invoke:invoke({'task':'update','games':[]}))
    assert result['strategy']==['win','learn']
    assert trace.index('stop')<trace.index('chat')<trace.index('generate')<trace.index('start')
    assert trace[-2:]==['ps','start']


def test_ollama_generation_failure_still_unloads_and_restarts(monkeypatch):
    manager,trace=fake_ollama(monkeypatch,failed=True)
    with pytest.raises(RuntimeError,match='Generation failed'): manager.ollama(lambda invoke:invoke({'task':'update'}))
    assert trace[-3:]==['generate','ps','start'] and not manager.swap_pending


def test_ollama_unload_failure_blocks_restart_and_continue(monkeypatch):
    manager,trace=fake_ollama(monkeypatch,unload_failure=True)
    with pytest.raises(RuntimeError,match='Unload failed'): manager.ollama(lambda invoke:invoke({'task':'update'}))
    assert manager.swap_pending and 'start' not in trace
    with pytest.raises(RuntimeError,match='Unload failed'): manager.recover()
    assert 'start' not in trace


def test_unrelated_models_are_reported_without_touching_process(monkeypatch):
    manager,trace=fake_ollama(monkeypatch,unrelated=True)
    with pytest.raises(RuntimeError,match='unrelated'): manager.ollama(lambda _:pytest.fail('Coached'))
    assert 'stop' not in trace and 'generate' not in trace


def test_shutdown_cleans_owned_daemon_after_unload_failure(monkeypatch):
    manager,trace=fake_ollama(monkeypatch,unload_failure=True)
    manager.swap_pending=True
    manager.ollama_process=SimpleNamespace(pid=456,poll=lambda:None,wait=lambda **_: trace.append('daemon waited'))
    monkeypatch.setattr('app.inference.service_manager.os.killpg',lambda *_:trace.append('daemon killed'))
    with pytest.raises(RuntimeError): manager.shutdown()
    assert trace[-2:]==['daemon killed','daemon waited']


def test_windows_relay_uses_stdin_and_fixed_loopback(monkeypatch):
    import json
    from app.inference.windows_ollama_bridge import forward
    calls=[]
    def run(command,**kwargs):
        calls.append((command,kwargs))
        return SimpleNamespace(returncode=0,stdout=json.dumps({'status':200,'body':'{"models":[]}'}),stderr='')
    monkeypatch.setattr('app.inference.windows_ollama_bridge.subprocess.run',run)
    status,body=forward('POST','/api/chat',b'{"model":"qwen3.5:latest","messages":[]}',11434)
    assert status==200 and json.loads(body)=={'models':[]}
    command,kwargs=calls[0]
    assert 'http://127.0.0.1:11434/api/chat' in command[-1]
    assert json.loads(kwargs['input'])['model']=='qwen3.5:latest'
    assert 'qwen3.5:latest' not in command[-1]
    assert kwargs['timeout']==310
    for method,path in [('DELETE','/api/chat'),('POST','/api/pull'),('GET','http://outside/api/ps')]:
        with pytest.raises(ValueError):forward(method,path,b'',11434)
    assert len(calls)==1


def test_start_checks_windows_models_before_allocating_gpu(monkeypatch):
    trace=[]
    class Probe:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def connect_ex(self,_): return 1
    class Transport:
        def __init__(self,**_): pass
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def get(self,*_,**__):
            trace.append('check_models')
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'models':[{'name':'other:latest'}]})
    monkeypatch.setattr('app.inference.service_manager.socket.socket',Probe)
    monkeypatch.setattr('app.inference.service_manager.httpx.Client',Transport)
    monkeypatch.setattr('app.inference.service_manager.subprocess.Popen',lambda *_ ,**__:pytest.fail('Allocated GPU before checking Windows models'))
    manager=ServiceManager(replace(Config(),coach_backend='ollama',coach_model='coach'))
    manager._ensure_ollama=lambda _:trace.append('relay_ready')
    with pytest.raises(RuntimeError,match='other:latest'):manager.start()
    assert trace==['relay_ready','check_models']


def test_slow_existing_ollama_does_not_launch_duplicate_daemon(monkeypatch):
    import httpx
    transport=SimpleNamespace(get=lambda *_,**__: (_ for _ in ()).throw(httpx.ReadTimeout('slow Windows relay')))
    manager=ServiceManager(replace(Config(),coach_backend='ollama',coach_model='coach'))
    monkeypatch.setattr('app.inference.service_manager.subprocess.Popen',lambda *_,**__:pytest.fail('Duplicate daemon launched after a read timeout'))
    with pytest.raises(httpx.ReadTimeout):manager._ensure_ollama(transport)
