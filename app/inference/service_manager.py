"""Own only child processes launched by this application."""
from pathlib import Path
import os
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
import httpx


class ServiceManager:
    def __init__(self, config):
        self.config = config
        self.process = self.ollama_process = None
        self.log = None
        self.swap_pending = False
        self.selected_model = config.coach_model
        self.progress = lambda message: None

    def start(self):
        if self.swap_pending:
            raise RuntimeError('Ollama unload is not confirmed. Retry coaching before restarting Imajev.')
        if self.config.external_inference:
            return
        url = urlsplit(self.config.endpoint)
        if url.hostname == '::1':
            raise RuntimeError('Managed inference supports IPv4 loopback only. Use 127.0.0.1 or localhost, or --external-inference for IPv6.')
        if self.process is not None and self.process.poll() is None:
            return
        if url.port != 8765:
            raise RuntimeError('Managed service uses port 8765. Use --external-inference for other ports.')
        with socket.socket() as probe:
            if probe.connect_ex((url.hostname, url.port)) == 0:
                raise RuntimeError('Imajev port is already occupied. Stop the existing service or use --external-inference.')
        if self.config.coach_backend == 'ollama':
            with httpx.Client(timeout=10, trust_env=False) as transport:
                self._ensure_ollama(transport)
                response = transport.get(self.config.ollama_url.rstrip('/') + '/api/ps')
                response.raise_for_status()
                loaded = [m['name'] for m in response.json()['models']]
                if loaded:
                    raise RuntimeError('Unload Ollama models before starting Imajev: ' + ', '.join(loaded))
        if self.process is not None:
            self.stop()
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
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
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
        try:
            self.stop()
            if self.swap_pending:
                with httpx.Client(timeout=30, trust_env=False) as client:
                    self._unload(client)
        finally:
            if self.ollama_process is not None and self.ollama_process.poll() is None:
                import signal
                os.killpg(self.ollama_process.pid, signal.SIGTERM)
                try:
                    self.ollama_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(self.ollama_process.pid, signal.SIGKILL)
                    self.ollama_process.wait(timeout=10)

    def recover(self):
        """Confirm a selected model is unloaded before permitting an Imajev restart."""
        if self.swap_pending:
            with httpx.Client(timeout=30, trust_env=False) as client:
                self._unload(client)
        self.start()

    def _ensure_ollama(self, client):
        """Reach the daemon before GPU allocation, including through the WSL relay."""
        url = self.config.ollama_url.rstrip('/')
        try:
            client.get(url + '/api/ps', timeout=10).raise_for_status()
            return
        except httpx.ConnectError:
            pass
        if self.ollama_process is not None and self.ollama_process.poll() is None:
            raise RuntimeError('The owned Ollama daemon or Windows relay is unavailable.')
        executable = shutil.which('ollama')
        if executable:
            command = [executable, 'serve']
        elif Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe').exists():
            command = [sys.executable, '-m', 'app.inference.windows_ollama_bridge', '--url', url]
        else:
            raise RuntimeError('Install Ollama and the configured model explicitly before coaching.')
        self.ollama_process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2],
            env=dict(os.environ, OLLAMA_HOST=urlsplit(url).netloc), start_new_session=True)
        deadline = time.monotonic() + 20
        while True:
            try:
                client.get(url + '/api/ps', timeout=10).raise_for_status()
                return
            except httpx.RequestError:
                if self.ollama_process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Ollama daemon or Windows relay did not become ready.')
                time.sleep(.25)

    def _unload(self, client):
        url = self.config.ollama_url.rstrip('/')
        response = client.post(url + '/api/generate', json={'model': self.selected_model, 'keep_alive': 0})
        response.raise_for_status()
        deadline = time.monotonic() + 30
        while True:
            response = client.get(url + '/api/ps'); response.raise_for_status()
            loaded = [m['name'] for m in response.json()['models']]
            if not loaded:
                self.swap_pending = False
                return
            if any(m != self.selected_model for m in loaded):
                raise RuntimeError('Unrelated Ollama models prevent restart: ' + ', '.join(loaded))
            if time.monotonic() >= deadline:
                raise RuntimeError('Ollama unload is unconfirmed; Imajev restart blocked.')
            time.sleep(.25)

    def ollama(self, operation):
        """Run the entire history pipeline within a single exclusive GPU swap."""
        if self.config.external_inference:
            raise RuntimeError('Ollama coaching requires managed Imajev process ownership.')
        from app.inference.coach import coach_messages, parse_coach_output
        url = self.config.ollama_url.rstrip('/')
        with httpx.Client(timeout=300, trust_env=False) as client:
            self._ensure_ollama(client)
            if self.swap_pending:
                self._unload(client)
            tags = client.get(url + '/api/tags'); tags.raise_for_status()
            names = {m['name'] for m in tags.json()['models']}
            model = self.config.coach_model
            self.selected_model = model if model in names else model + ':latest'
            if self.selected_model not in names:
                raise RuntimeError('Configured Ollama model is not installed; gameplay never downloads models.')
            provenance = next(m for m in tags.json()['models'] if m['name'] == self.selected_model)
            response = client.get(url + '/api/ps'); response.raise_for_status()
            unrelated = [m['name'] for m in response.json()['models'] if m['name'] != self.selected_model]
            if unrelated:
                raise RuntimeError('Unload unrelated Ollama models before coaching: ' + ', '.join(unrelated))
            self.stop()
            self.swap_pending = True

            def invoke(request):
                self.progress('Diagnosing loss…' if request['task'] == 'diagnose'
                              else 'Updating strategy…' if request['task'] == 'update' else 'Studying games…')
                response = client.post(url + '/api/chat', json={'model': self.selected_model,
                    'messages': coach_messages(request), 'stream': False, 'keep_alive': 0, 'think': False,
                    'options': {'temperature': 0, 'num_ctx': 8192, 'num_predict': 512}})
                response.raise_for_status()
                raw = response.json()
                text = raw['message']['content']
                result = parse_coach_output(text, request['task'], raw.get('done_reason') == 'length' or not raw.get('done', True))
                return dict(result, response_text=text, prompt=coach_messages(request), model=self.selected_model,
                            provenance=provenance,
                            usage={k: raw.get(k) for k in ('prompt_eval_count', 'eval_count')},
                            timings={k: raw.get(k) for k in ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration')})
            try:
                return operation(invoke)
            finally:
                self._unload(client)
                self.progress('Reloading Imajev…')
                self.start()
