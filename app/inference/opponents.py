"""GUI-free opponent adapters. Only the ImaJEV adapter consumes decision Replies."""
from dataclasses import replace
import base64
import json
import math
import time
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.contracts import MoveResult
from app.inference.imajev_client import ImajevClient, InferenceError, Unavailable


class Unsupported(InferenceError):
    """The complete experiment cannot be represented by this runtime."""


class OpponentBackend(Protocol):
    def choose_move(self, request: dict, image: bytes | None = None) -> MoveResult: ...


def from_imajev(reply):
    answer = reply.answers['move' if 'move' in reply.answers else next(iter(reply.answers))]
    return MoveResult(answer.choice, reply.model, reply.raw, 'imajev',
                      dict(answer.probabilities), answer.abstained, answer.unknown_probability)


def named_result(result, name):
    """Read an additional research question without coupling callers to Reply."""
    if result.rejection:
        raise ValueError(result.rejection)
    answer = result.raw['answers'][name]
    return MoveResult(answer['choice'], result.model, result.raw, result.backend,
                      answer.get('probabilities'), answer.get('abstained'),
                      answer.get('unknown_probability'), answer.get('confidence'))


def main_question(request):
    return request['questions']['move' if 'move' in request['questions'] else next(iter(request['questions']))]


def metadata(result):
    return {'choice': result.choice, 'model': result.model, 'backend': result.backend,
            'probabilities': dict(result.probabilities) if result.probabilities is not None else None,
            'abstained': result.abstained, 'unknown_probability': result.unknown_probability,
            'confidence': result.confidence, 'rejection': result.rejection}


def description(value):
    """Match ImaJEV's structured-description flattening, including insertion order."""
    def text(item):
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        return '; '.join(f'{key}: {text(item)}' for key, item in value.items()) or None
    return '; '.join(text(item) for item in value) or None


def native_request(request):
    return {**request, 'questions': {name: {**question, 'criteria': {
        key: description(value) for key, value in question['criteria'].items()}}
        for name, question in request['questions'].items()}}


def parse_move(raw, request, settings):
    if not isinstance(raw, dict) or raw.get('model') != settings.model:
        raise InferenceError(f'Expected opponent model {settings.model}; received a different model identity.')
    offered = main_question(request)['criteria']
    if settings.protocol == 'chat':
        try:
            content = raw['message']['content']
            if raw.get('done_reason') == 'length' or not raw.get('done', True):
                raise ValueError('Truncated opponent response.')
            parsed = json.loads(content)
            choice = parsed['choice']
            if not isinstance(choice, str):
                raise ValueError('Opponent choice must be a string.')
            rejection = None if choice in offered else 'Opponent returned an unoffered choice.'
            return MoveResult(choice, raw['model'], raw, settings.backend, rejection=rejection)
        except (KeyError, TypeError, ValueError) as exc:
            return MoveResult('', settings.model, raw, settings.backend,
                              rejection=f'Opponent did not return a complete JSON choice: {exc}')
    name = 'move' if 'move' in request['questions'] else next(iter(request['questions']))
    answers = raw.get('answers')
    answer = answers.get(name) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get('type') != 'choice':
        return MoveResult('', settings.model, raw, settings.backend, rejection='Opponent response is missing its choice answer.')
    choice = answer.get('choice')
    probabilities = answer.get('probabilities')
    rejection = None
    if not isinstance(choice, str) or choice not in offered:
        rejection = 'Opponent returned an unoffered choice.'
    if probabilities is not None:
        if (not isinstance(probabilities, dict) or set(probabilities) != set(offered)
                or any(isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p)
                       or not 0 <= p <= 1 for p in probabilities.values())
                or abs(sum(probabilities.values()) - 1) > .02):
            rejection = 'Opponent returned an invalid choice distribution.'
            probabilities = None
        elif choice in probabilities and probabilities[choice] + 1e-6 < max(probabilities.values()):
            rejection = 'Opponent choice does not match its highest probability.'
    confidence = answer.get('confidence')
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                   or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        rejection = 'Opponent returned invalid confidence.'
        confidence = None
    abstained = answer.get('abstained')
    unknown = answer.get('unknown_probability')
    if abstained is not None and not isinstance(abstained, bool):
        rejection = 'Opponent returned invalid abstention.'
        abstained = None
    if unknown is not None and (isinstance(unknown, bool) or not isinstance(unknown, (int, float))
                                or not math.isfinite(unknown) or not 0 <= unknown <= 1):
        rejection = 'Opponent returned invalid unknown probability.'
        unknown = None
    for other, question in request['questions'].items():
        if other != name:
            additional = parse_move(raw, {'questions': {other: question}}, settings)
            rejection = rejection or additional.rejection
    return MoveResult(choice if isinstance(choice, str) else '', settings.model, raw, settings.backend,
                      probabilities, abstained, unknown, confidence, rejection)


class ImajevOpponent:
    def __init__(self, config, client=None, manager=None):
        self.settings = config.opponent_settings
        self.config = replace(config, endpoint=self.settings.endpoint, expected_model=self.settings.model)
        self.client = client or ImajevClient(self.config)
        self.manager = manager

    def choose_move(self, request, image=None, timeout=None):
        image = image if self.settings.input_mode == 'text_image' else None
        def operation():
            if self.manager and not self.config.external_inference:
                self.client.manager = self.manager
                readiness_started = time.monotonic()
                self.client.wait_ready()
                activation_seconds = time.monotonic() - readiness_started
                inference_started = time.monotonic()
                reply = self.client.decide(request, image, **({'timeout': timeout} if timeout else {}))
                result = from_imajev(reply)
                return replace(result, raw={**result.raw, 'activation_seconds': activation_seconds,
                                            'inference_seconds': time.monotonic() - inference_started,
                                            'runtime_provenance': result.raw.get('service', {}).get('provenance', {})})
            else:
                reply = self.client.decide(request, image, **({'timeout': timeout} if timeout else {}))
            return from_imajev(reply)
        try:
            if self.manager:
                return self.manager.run_imajev(self.config, operation)
            return operation()
        except InferenceError as exc:
            raw = getattr(exc, 'raw', None)
            if not isinstance(raw, dict):
                raise
            result = parse_move(raw, request, self.settings)
            return replace(result, rejection=result.rejection or str(exc))

    def warmup(self, request, image=None):
        if self.manager:
            return self.choose_move(request, image)
        image = image if self.settings.input_mode == 'text_image' else None
        return from_imajev(self.client.warmup(request, image))


class HTTPOpponent:
    def __init__(self, config, manager=None):
        self.config = config
        self.settings = config.opponent_settings
        self.manager = manager
        self.uncertain_busy = False
        self.provenance = {}

    def preflight(self, request, image):
        choices = main_question(request)['criteria']
        if self.settings.protocol == 'chat' and len(request['questions']) != 1:
            raise Unsupported('Chat opponent experiments support one choice question per request.')
        if any(len(q.get('criteria', {})) < 2 for q in request['questions'].values()):
            raise Unsupported('A single legal move must bypass inference.')
        largest = max(len(q.get('criteria', {})) for q in request['questions'].values())
        if self.settings.protocol != 'chat' and largest > self.settings.max_choices:
            raise Unsupported(f'{self.settings.model} supports at most {self.settings.max_choices} choices; '
                              f'this position requires all {largest}. No moves were removed.')
        if self.settings.input_mode == 'text_image' and image is None:
            raise Unsupported('This opponent profile requires a board image.')

    def _payload(self, request, image):
        s = self.settings
        if s.protocol == 'chat':
            body = {'model': s.model, 'stream': False, 'think': False,
                    'keep_alive': -1, 'truncate': False, 'shift': False, 'format': {'type': 'object', 'properties': {'choice': {
                        'type': 'string', 'enum': list(main_question(request)['criteria'])}},
                        'required': ['choice'], 'additionalProperties': False},
                    'messages': [{'role': 'system', 'content': 'Choose one offered move. Return JSON with exactly one field, choice.'},
                                 {'role': 'user', 'content': json.dumps(request, ensure_ascii=False)}],
                    'options': {'temperature': s.generation_temperature, 'num_ctx': s.context_limit, 'num_predict': s.generation_max_tokens}}
            if image is not None:
                body['messages'][-1]['images'] = [base64.b64encode(image).decode('ascii')]
            return body
        body = dict(native_request(request), model=s.model)
        if s.backend == 'ollama':
            body['keep_alive'] = -1
        if image is not None:
            encoded = base64.b64encode(image).decode('ascii')
            body['images'] = [encoded if s.backend == 'ollama' else 'data:image/png;base64,' + encoded]
        return body

    def _request(self, request, image, timeout):
        if self.uncertain_busy and (not self.manager or self.config.external_inference):
            raise InferenceError('The timed-out external opponent may still be working. Restart the external service and this application before Retry.')
        try:
            with httpx.Client(timeout=timeout or self.config.request_timeout, trust_env=False, follow_redirects=False) as transport:
                response = transport.post(self.settings.endpoint, json=self._payload(request, image))
            if response.status_code in (400, 413, 501):
                raise Unsupported(f'Opponent cannot represent this request (HTTP {response.status_code}): {response.text[:300]}')
            if response.status_code != 200:
                raise InferenceError(f'Opponent returned HTTP {response.status_code}: {response.text[:300]}')
            try:
                def invalid_constant(value):
                    raise ValueError(f'Nonfinite JSON number: {value}')
                raw = json.loads(response.content, parse_constant=invalid_constant)
            except ValueError:
                return MoveResult('', self.settings.model, {'response_text': response.text}, self.settings.backend,
                                  rejection='Opponent returned malformed JSON.')
            result = parse_move(raw, request, self.settings)
            if self.provenance:
                result = replace(result, raw={**result.raw, 'runtime_provenance': self.provenance})
            return result
        except httpx.TimeoutException as exc:
            self.uncertain_busy = True
            raise InferenceError('Opponent timed out; GPU release must be confirmed before Retry.') from exc
        except httpx.RequestError as exc:
            raise Unavailable('Cannot reach the configured opponent service.') from exc

    def choose_move(self, request, image=None, timeout=None):
        image = image if self.settings.input_mode == 'text_image' else None
        self.preflight(request, image)
        def operation():
            self.uncertain_busy = False  # Managed activation has confirmed release/recovery.
            return self._request(request, image, timeout)
        if self.manager and not self.config.external_inference:
            return self.manager.run_opponent(self.settings, operation)
        return self._request(request, image, timeout)

    def warmup(self, request, image=None):
        return self.choose_move(request, image, timeout=self.config.startup_timeout)


def create_opponent(config, client=None, manager=None):
    if config.opponent_settings.backend == 'imajev':
        same = (config.opponent_settings.model == config.expected_model
                and config.opponent_settings.endpoint == config.endpoint)
        return ImajevOpponent(config, client if same else None, manager)
    return HTTPOpponent(config, manager)


def role_metadata(config):
    from dataclasses import asdict
    opponent = asdict(config.opponent_settings)
    # Existing recordings already used this budget; preserve their role identity.
    if opponent['generation_max_tokens'] == 128:
        opponent.pop('generation_max_tokens')
    return {'version': 1, 'recognition': {'backend': 'imajev', 'model': config.expected_model,
                                        'endpoint': config.endpoint},
            'opponent': opponent}
