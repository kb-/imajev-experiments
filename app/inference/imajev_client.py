import math
import time
from urllib.parse import urlsplit, urlunsplit
import httpx
from app.core.contracts import ChoiceAnswer, Reply

ADAPTER_VERSION = '1'
UPSTREAM_COMMIT = 'ccf586d43d2a580319b6535c893668904d909eb9'


class InferenceError(Exception):
    pass


class Unavailable(InferenceError):
    pass


def probability(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise InferenceError('Imajev returned an invalid probability. Check the service installation and retry.')
    return float(value)


def parse_reply(raw: dict, request: dict, expected_model: str) -> Reply:
    if not isinstance(raw, dict) or raw.get('model') != expected_model:
        raise InferenceError(f'Expected {expected_model}. Check the configured model and local service.')
    answers = raw.get('answers')
    if not isinstance(answers, dict):
        raise InferenceError('Imajev response is missing answers.')
    result = {}
    for name, question in request['questions'].items():
        answer = answers.get(name)
        if not isinstance(answer, dict) or answer.get('type') != 'choice':
            raise InferenceError(f'Imajev response is missing choice answer {name}.')
        probs = answer.get('probabilities')
        if not isinstance(probs, dict) or set(probs) != set(question['criteria']):
            raise InferenceError('Imajev probabilities do not match the offered choices.')
        probs = {k: probability(v) for k, v in probs.items()}
        if abs(sum(probs.values()) - 1) > .02:
            raise InferenceError('Imajev choice probabilities must sum to one.')
        choice = answer.get('choice')
        if not isinstance(choice, str) or choice not in probs or not isinstance(answer.get('abstained'), bool):
            raise InferenceError('Imajev returned an invalid choice or abstention.')
        if probs[choice] + 1e-6 < max(probs.values()):
            raise InferenceError('Imajev choice does not match its highest probability.')
        result[name] = ChoiceAnswer(choice, probs, probability(answer.get('unknown_probability')), answer['abstained'])
    return Reply(raw['model'], result, raw)


class ImajevClient:
    def __init__(self, config):
        self.config = config
        self.uncertain_busy = False
        self.service_metadata = None
        self.manager = None

    def _check_busy(self, client):
        parts = urlsplit(self.config.endpoint)
        status_url = urlunsplit((parts.scheme, parts.netloc, '/v1/status', '', ''))
        response = client.get(status_url, timeout=min(2, self.config.request_timeout))
        if response.status_code == 404:
            if self.uncertain_busy:
                raise InferenceError('The timed-out service may still be working. Use the supplied guarded service launcher to enable safe Retry.')
            return
        if response.status_code != 200:
            raise InferenceError('Cannot check local service availability. Retry when it is ready.')
        try:
            metadata = response.json()
        except ValueError as exc:
            raise InferenceError('Local service status is malformed.') from exc
        if not isinstance(metadata, dict) or not isinstance(metadata.get('busy'), bool):
            raise InferenceError('Local service status is missing its busy flag.')
        if metadata['busy']:
            raise InferenceError('The previous GPU request is still running. Wait until it finishes, then Retry.')
        self.uncertain_busy = False
        self.service_metadata = metadata

    def decide(self, request: dict, image: bytes, timeout=None) -> Reply:
        import json
        try:
            with httpx.Client(timeout=timeout or self.config.request_timeout, trust_env=False, follow_redirects=False) as client:
                self._check_busy(client)
                response = client.post(self.config.endpoint, data={'request': json.dumps(request)},
                                       files={'image': ('board.png', image, 'image/png')})
            if response.status_code >= 400:
                detail = response.text[:300]
                if response.status_code == 409:
                    raise InferenceError('The previous GPU request is still running. Wait until it finishes, then Retry.')
                if 'out of memory' in detail.lower() or 'oom' in detail.lower():
                    raise InferenceError('The local GPU ran out of memory. Restart the service with the configured model, then Retry.')
                raise InferenceError(f'Local service returned HTTP {response.status_code}: {detail}')
            if response.status_code != 200:
                raise InferenceError('Unexpected service redirect or status. Check the endpoint.')
            try:
                raw = response.json()
            except ValueError as exc:
                raise InferenceError('The service returned malformed JSON. Check its logs and retry.') from exc
            parsed = parse_reply(raw, request, self.config.expected_model)
            if self.service_metadata:
                raw['service'] = self.service_metadata
            return parsed
        except httpx.TimeoutException as exc:
            self.uncertain_busy = True
            raise InferenceError('The model request timed out. Wait for the local service to finish, then Retry.') from exc
        except httpx.RequestError as exc:
            raise Unavailable('Cannot reach Imajev. Start the local service, then Retry.') from exc

    def warmup(self, request: dict, image: bytes) -> Reply:
        # Upstream exposes /v1/models; it has no dedicated readiness/health endpoint.
        parts = urlsplit(self.config.endpoint)
        models_url = urlunsplit((parts.scheme, parts.netloc, '/v1/models', '', ''))
        deadline = time.monotonic() + self.config.startup_timeout
        while True:
            if self.manager:
                self.manager.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Unavailable('Startup readiness timed out. Start the local image service, then Retry.')
            try:
                with httpx.Client(timeout=min(2, remaining), trust_env=False) as client:
                    response = client.get(models_url)
                if response.status_code != 200:
                    raise InferenceError(f'Readiness check returned HTTP {response.status_code}. Check the local service.')
                try:
                    status = response.json()
                except ValueError as exc:
                    raise InferenceError('Readiness response is malformed.') from exc
                if not isinstance(status, dict) or status.get('loaded') is not True or status.get('model') != self.config.expected_model:
                    raise InferenceError('The local service is not loaded with the configured model.')
                if status.get('backend') != 'torch':
                    raise InferenceError('Use the configured PyTorch image service. No backend fallback is enabled.')
                break
            except httpx.RequestError:
                time.sleep(min(.5, max(0, deadline - time.monotonic())))
        # One actual image request validates model/contract and warms its image stack.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Unavailable('Startup warm-up budget expired. Retry after the service is ready.')
        return self.decide(request, image, timeout=min(remaining, self.config.request_timeout))
