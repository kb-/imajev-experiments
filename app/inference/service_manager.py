"""Own only child processes launched by this application."""
from pathlib import Path
import os
import shutil
import socket
import subprocess
import time
from urllib.parse import urlsplit
import httpx


class ServiceManager:
    def __init__(self, config):
        self.config = config
        self.process = self.ollama_process = None
        self.log = None
        self.swap_pending = False
        self.progress = lambda message: None

    def start(self):
        if self.swap_pending:
            raise RuntimeError('Ollama unload is not confirmed. Retry coaching before restarting Imajev.')
        if self.config.external_inference:
            return
        if self.process is not None and self.process.poll() is None:
            return
        url = urlsplit(self.config.endpoint)
        if url.port != 8765:
            raise RuntimeError('Managed service uses port 8765. Use --external-inference for other ports.')
        with socket.socket() as probe:
            if probe.connect_ex((url.hostname, url.port)) == 0:
                raise RuntimeError('Imajev port is already occupied. Stop the existing service or use --external-inference.')
        root = Path(__file__).resolve().parents[2]
        launchers = {'imajev-4b-nf4': 'launch_inference_4b_nf4.sh', 'imajev-2b': 'launch_inference_2b.sh'}
        if self.config.expected_model not in launchers:
            raise RuntimeError('No managed launcher for this model. Use --external-inference.')
        (root / 'logs').mkdir(exist_ok=True)
        self.log = open(root / 'logs' / 'managed-service.log', 'a')
        self.process = subprocess.Popen(['bash', str(root / 'scripts' / launchers[self.config.expected_model])],
                                        cwd=root, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)

    def check(self):
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError('Imajev child exited. See logs/managed-service.log and Retry.')

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                import signal
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=10)
            self.process = None
        if self.log:
            self.log.close()
            self.log = None

    def shutdown(self):
        self.stop()
        if self.ollama_process is not None and self.ollama_process.poll() is None:
            self.ollama_process.terminate()
            self.ollama_process.wait(timeout=10)

    def ollama(self, request):
        if self.config.external_inference:
            raise RuntimeError('Ollama coaching requires managed Imajev process ownership.')
        from app.inference.coach import coach_messages
        url = self.config.ollama_url.rstrip('/')
        with httpx.Client(timeout=300, trust_env=False) as client:
            try:
                client.get(url + '/api/ps', timeout=2).raise_for_status()
            except httpx.RequestError:
                executable = shutil.which('ollama')
                if not executable:
                    raise RuntimeError('Install Ollama and the configured model explicitly before coaching.')
                self.ollama_process = subprocess.Popen([executable, 'serve'], env=dict(os.environ, OLLAMA_HOST=urlsplit(url).netloc))
                deadline = time.monotonic() + 20
                while True:
                    try:
                        client.get(url + '/api/ps', timeout=2).raise_for_status()
                        break
                    except httpx.RequestError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Ollama daemon did not become ready.')
                        time.sleep(.25)
            tags = client.get(url + '/api/tags'); tags.raise_for_status()
            model = self.config.coach_model
            names = {m['name'] for m in tags.json()['models']}
            selected = model if model in names else model + ':latest'
            if selected not in names:
                raise RuntimeError('Configured Ollama model is not installed; gameplay never downloads models.')
            def loaded():
                response = client.get(url + '/api/ps'); response.raise_for_status()
                return [m['name'] for m in response.json()['models']]
            unrelated = [m for m in loaded() if m != selected]
            if unrelated:
                raise RuntimeError('Unload unrelated Ollama models before coaching: ' + ', '.join(unrelated))
            self.stop()
            self.swap_pending = True
            self.progress('Updating strategy…')
            try:
                response = client.post(url + '/api/chat', json={'model': selected, 'messages': coach_messages(request),
                    'stream': False, 'keep_alive': 0, 'think': False,
                    'options': {'temperature': 0, 'num_ctx': 4096, 'num_predict': 512}})
                response.raise_for_status()
                raw = response.json()
                result = {'strategy': raw['message']['content'], 'truncated': raw.get('done_reason') == 'length',
                          'prompt': coach_messages(request), 'included_game_ids': request.get('included_game_ids', []),
                          'usage': {k: raw.get(k) for k in ('prompt_eval_count', 'eval_count')}, 'model': selected}
            finally:
                response = client.post(url + '/api/generate', json={'model': selected, 'keep_alive': 0})
                response.raise_for_status()
                deadline = time.monotonic() + 30
                while loaded():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Ollama remains loaded; Imajev restart blocked to preserve GPU ownership.')
                    time.sleep(.25)
                self.swap_pending = False
                self.progress('Reloading Imajev…')
                self.start()
            return result
