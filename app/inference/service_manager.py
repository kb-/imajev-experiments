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
from dataclasses import replace


class ServiceManager:
    def __init__(self, config):
        self.config = config
        self.process = self.ollama_process = None
        self.log = None
        self.swap_pending = False
        self.selected_model = config.coach_model
        self.progress = lambda message: None
        self.active_imajev_config = config
        self.opponent_process = None
        self.opponent_log = None
        self.active_opponent = None
        self.opponent_ollama = None
        self.runtime_provenance = {}

    def start(self):
        self._release_opponent()
        if self.swap_pending:
            raise RuntimeError('Ollama unload is not confirmed. Retry coaching before restarting Imajev.')
        if self.config.external_inference:
            return
        url = urlsplit(self.active_imajev_config.endpoint)
        if url.hostname == '::1':
            raise RuntimeError('Managed inference supports IPv4 loopback only. Use 127.0.0.1 or localhost, or --external-inference for IPv6.')
        if self.process is not None and self.process.poll() is None:
            return
        if url.port != 8765:
            raise RuntimeError('Managed service uses port 8765. Use --external-inference for other ports.')
        with socket.socket() as probe:
            if probe.connect_ex((url.hostname, url.port)) == 0:
                raise RuntimeError('Imajev port is already occupied. Stop the existing service or use --external-inference.')
        if self.config.coach_backend == 'ollama' or self.config.opponent_settings.backend == 'ollama':
            with httpx.Client(timeout=10, trust_env=False) as transport:
                original = self.config
                if original.opponent_settings.backend == 'ollama':
                    from urllib.parse import urlunsplit
                    parts = urlsplit(original.opponent_settings.endpoint)
                    self.config = replace(original, ollama_url=urlunsplit((parts.scheme, parts.netloc, '', '', '')))
                try:
                    self._ensure_ollama(transport)
                    response = transport.get(self.config.ollama_url.rstrip('/') + '/api/ps')
                finally:
                    self.config = original
                response.raise_for_status()
                loaded = [m['name'] for m in response.json()['models']]
                if loaded:
                    raise RuntimeError('Unload Ollama models before starting Imajev: ' + ', '.join(loaded))
        if self.process is not None:
            self.stop()
        root = Path(__file__).resolve().parents[2]
        launchers = {'imajev-4b-nf4': 'launch_inference_4b_nf4.sh', 'imajev-2b': 'launch_inference_2b.sh'}
        if self.active_imajev_config.expected_model not in launchers:
            raise RuntimeError('No managed launcher for this model. Use --external-inference.')
        (root / 'logs').mkdir(exist_ok=True)
        self.log = open(root / 'logs' / 'managed-service.log', 'a')
        self.process = subprocess.Popen(['bash', str(root / 'scripts' / launchers[self.active_imajev_config.expected_model])],
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
            self._release_opponent()
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
        self.prepare_imajev(self.config)

    def imajev_active(self, config):
        return (self.active_opponent is None and self.process is not None and self.process.poll() is None
                and self.active_imajev_config.endpoint == config.endpoint
                and self.active_imajev_config.expected_model == config.expected_model)

    def prepare_imajev(self, config):
        if self.config.external_inference:
            return
        self._release_opponent()
        if (self.active_imajev_config.endpoint, self.active_imajev_config.expected_model) != (config.endpoint, config.expected_model):
            self.stop()
        self.active_imajev_config = config
        self.start()

    def run_imajev(self, config, operation):
        started = time.monotonic()
        self.prepare_imajev(config)
        activation_seconds = time.monotonic() - started
        result = operation()
        from app.core.contracts import MoveResult
        if isinstance(result, MoveResult):
            return replace(result, raw={**result.raw, 'activation_seconds':
                                        activation_seconds + result.raw.get('activation_seconds', 0)})
        return result

    def _release_opponent(self):
        """Release only our activated runtime; failed unload keeps activation blocked."""
        if self.opponent_ollama is not None:
            url, model = self.opponent_ollama
            with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
                response = client.post(url + '/api/generate', json={'model': model, 'keep_alive': 0})
                response.raise_for_status()
                deadline = time.monotonic() + 30
                while True:
                    response = client.get(url + '/api/ps'); response.raise_for_status()
                    loaded = response.json()['models']
                    if not any(item['name'] == model for item in loaded):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Opponent unload is unconfirmed; GPU activation blocked.')
                    time.sleep(.25)
            self.opponent_ollama = None
        if self.opponent_process is not None:
            import signal
            if self.opponent_process.poll() is None:
                try:
                    os.killpg(self.opponent_process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.opponent_process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(self.opponent_process.pid, signal.SIGKILL)
                    self.opponent_process.wait(timeout=10)
            self.opponent_process = None
        if self.opponent_log:
            self.opponent_log.close()
            self.opponent_log = None
        self.active_opponent = None

    def run_opponent(self, settings, operation):
        if self.config.external_inference:
            return operation()
        if self.swap_pending:
            with httpx.Client(timeout=30, trust_env=False) as client:
                self._unload(client)
        # Leave a successful opponent resident until the next role needs the GPU.
        started = time.monotonic()
        if self.active_opponent != settings or (self.opponent_process is not None and self.opponent_process.poll() is not None):
            self._release_opponent()
            self.progress('Loading ' + settings.model + '…')
            if settings.backend == 'ollama':
                self._activate_ollama(settings)
            else:
                self._activate_local(settings)
            self.active_opponent = settings
        activation_seconds = time.monotonic() - started
        try:
            inference_started = time.monotonic()
            result = operation()
            return replace(result, raw={**result.raw, 'runtime_provenance': self.runtime_provenance,
                                        'activation_seconds': activation_seconds,
                                        'inference_seconds': time.monotonic() - inference_started})
        except BaseException:
            # An HTTP timeout does not prove GPU work has finished. Process exit or
            # a confirmed Ollama unload is required before another operation.
            self._release_opponent()
            raise

    def _activate_ollama(self, settings):
        from urllib.parse import urlunsplit
        parts = urlsplit(settings.endpoint)
        url = urlunsplit((parts.scheme, parts.netloc, '', '', ''))
        with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
            # Reuse the existing Windows relay/daemon discovery with the role URL.
            original = self.config
            try:
                self.config = replace(original, ollama_url=url)
                self._ensure_ollama(client)
            finally:
                self.config = original
            tags = client.get(url + '/api/tags'); tags.raise_for_status()
            entries = tags.json()['models']
            entry = next((m for m in entries if m['name'] in (settings.model, settings.model + ':latest')), None)
            if entry is None:
                raise RuntimeError('Opponent model is not installed; install it explicitly before playing.')
            loaded = client.get(url + '/api/ps'); loaded.raise_for_status()
            if loaded.json()['models']:
                raise RuntimeError('Unload existing Ollama models before activating an opponent.')
            version = client.get(url + '/api/version'); version.raise_for_status()
            show = client.post(url + '/api/show', json={'model': entry['name']}); show.raise_for_status()
            capabilities = show.json().get('capabilities', [])
            if settings.input_mode == 'text_image' and 'vision' not in capabilities:
                raise RuntimeError('Configured Ollama model does not advertise image support.')
            if settings.protocol == 'systemone':
                numbers = tuple(int(x) for x in version.json()['version'].split('-')[0].split('.')[:3])
                if numbers < (0, 35, 0):
                    raise RuntimeError('Ollama System One requires version 0.35 or later.')
            self.stop()
            self.opponent_ollama = (url, entry['name'])
            self.runtime_provenance = {'ollama': version.json(), 'model': entry, 'capabilities': capabilities}
            try:
                if settings.protocol == 'chat':
                    # Empty chat loads a completion model without generating tokens.
                    response = client.post(url + '/api/chat', json={
                        'model': settings.model, 'messages': [], 'stream': False, 'keep_alive': -1,
                        'options': {'num_ctx': settings.context_limit}}, timeout=self.config.startup_timeout)
                    response.raise_for_status()
                    raw = response.json()
                    if raw.get('model') != settings.model or raw.get('done') is not True:
                        raise RuntimeError('Ollama load did not confirm the configured model.')
                else:
                    # Decision-only models need their native endpoint, not /api/generate.
                    from app.inference.opponents import parse_move
                    request = {'state': 'Runtime readiness check.', 'questions': {'ready': {
                        'type': 'choice', 'instructions': 'Select ready.',
                        'criteria': {'ready': 'The service is ready.', 'other': 'Another option.'}}}}
                    response = client.post(url + '/v1/systemone', json={
                        **request, 'model': settings.model, 'keep_alive': -1}, timeout=self.config.startup_timeout)
                    response.raise_for_status()
                    result = parse_move(response.json(), request, settings)
                    if result.rejection:
                        raise RuntimeError('Ollama readiness request failed: ' + result.rejection)
                loaded = client.get(url + '/api/ps'); loaded.raise_for_status()
                resident = loaded.json()['models']
                served = next((m for m in resident if m['name'] == entry['name']), None)
                if served is None or served.get('digest') != entry['digest']:
                    raise RuntimeError('Ollama readiness did not confirm the installed model identity.')
                if len(resident) != 1:
                    raise RuntimeError('Unrelated Ollama models prevent exclusive activation.')
                self.runtime_provenance['served_model'] = served
            except BaseException:
                self._release_opponent()
                raise

    def _activate_local(self, settings):
        from app.inference.provenance import file_identity
        model = Path(settings.model_path)
        if not settings.model_path or not model.is_file():
            raise RuntimeError('Configure an installed local opponent model_path; gameplay never downloads weights.')
        parts = urlsplit(settings.endpoint)
        if parts.hostname == '::1':
            raise RuntimeError('Managed local opponents require IPv4 loopback.')
        port = parts.port or 80
        with socket.socket() as probe:
            if probe.connect_ex((parts.hostname, port)) == 0:
                raise RuntimeError('Opponent port is already occupied; its owner was not touched.')
        if settings.backend == 'decider':
            executable = shutil.which(settings.executable) or settings.executable
            command = [executable, '-m', 'app.inference.decider_service', '--model', str(model),
                       '--identity', settings.model, '--port', str(port), '--context-limit', str(settings.context_limit), *settings.runtime_args]
            version = {'runtime': 'decider-ai', 'executable': str(executable)}
        else:
            executable = shutil.which(settings.executable)
            if executable is None or not Path(executable).is_file():
                raise RuntimeError('Configured llama.cpp executable is not installed.')
            version = subprocess.check_output([executable, '--version'], stderr=subprocess.STDOUT, text=True, timeout=10)
            command = [executable, '-m', str(model), '--alias', settings.model, '--host', '127.0.0.1',
                       '--port', str(port), '-c', str(settings.context_limit), '-np', '1', '--offline',
                       *settings.runtime_args]
            if settings.projector_path:
                if not Path(settings.projector_path).is_file():
                    raise RuntimeError('Configured image projector is missing.')
                command += ['--mmproj', settings.projector_path]
            elif settings.input_mode == 'text_image':
                raise RuntimeError('Image opponents require a local projector_path.')
        self.runtime_provenance = {'runtime': version, 'model': file_identity(model), 'command': command,
                                   'executable': file_identity(executable)}
        if settings.projector_path:
            self.runtime_provenance['projector'] = file_identity(settings.projector_path)
        self.stop()
        root = Path(__file__).resolve().parents[2]
        (root / 'logs').mkdir(exist_ok=True)
        self.opponent_log = open(root / 'logs/managed-opponent.log', 'a')
        self.opponent_process = subprocess.Popen(command, cwd=root, stdout=self.opponent_log,
                                                stderr=subprocess.STDOUT, start_new_session=True,
                                                env=dict(os.environ, HF_HUB_OFFLINE='1'))
        url = f'http://127.0.0.1:{port}'
        deadline = time.monotonic() + self.config.startup_timeout
        try:
            with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
                while True:
                    if self.opponent_process.poll() is not None:
                        raise RuntimeError('Opponent process exited; inspect logs/managed-opponent.log.')
                    try:
                        response = client.get(url + '/health')
                        if response.status_code == 200:
                            models = client.get(url + '/v1/models'); models.raise_for_status()
                            entries = models.json()['data']
                            entry = next((m for m in entries if m['id'] == settings.model), None)
                            if entry is None:
                                raise RuntimeError('Opponent readiness returned a different model identity.')
                            modalities = entry.get('architecture', {}).get('output_modalities')
                            if modalities is not None and 'decisions' not in modalities:
                                raise RuntimeError('Runtime has no native decision support for this model.')
                            inputs = entry.get('architecture', {}).get('input_modalities', [])
                            if settings.input_mode == 'text_image' and 'image' not in inputs:
                                raise RuntimeError('Runtime does not advertise image input for this model.')
                            self.runtime_provenance['served_model'] = entry
                            return
                    except httpx.RequestError:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Opponent readiness timed out.')
                    time.sleep(.25)
        except BaseException:
            self._release_opponent()
            raise

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
        self._release_opponent()
        self.active_imajev_config = self.config
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
                response = client.post(url + '/api/chat', json={'model': self.selected_model,
                    'messages': request['messages'], 'stream': False, 'keep_alive': 0, 'think': False,
                    'options': {'temperature': 0, 'num_ctx': 8192, 'num_predict': 512}})
                response.raise_for_status()
                raw = response.json()
                text = raw['message']['content']
                return dict(response_text=text, truncated=raw.get('done_reason') == 'length' or not raw.get('done', True),
                            prompt=request['messages'], model=self.selected_model,
                            provenance=provenance,
                            usage={k: raw.get(k) for k in ('prompt_eval_count', 'eval_count')},
                            timings={k: raw.get(k) for k in ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration')})
            try:
                return operation(invoke)
            finally:
                self._unload(client)
                self.progress('Reloading Imajev…')
                self.start()
