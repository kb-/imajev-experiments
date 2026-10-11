"""GUI-free opponent adapters. Only the ImaJEV adapter consumes decision Replies."""
from dataclasses import replace
import base64
import json
import time
from typing import Protocol

import httpx

from app.core.contracts import MoveResult
from app.inference.imajev_client import ImajevClient, InferenceError, Unavailable
from app.inference.native import (Unsupported, description, native_request, native_payload,
                                  parse_native_choice, request_json)


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
    result = parse_native_choice(raw, name, request['questions'][name], settings)
    for other, question in request['questions'].items():
        if other != name:
            additional = parse_native_choice(raw, other, question, settings)
            result = replace(result, rejection=result.rejection or additional.rejection)
    return result


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
        return native_payload(request, image, s)

    def _request(self, request, image, timeout):
        if self.uncertain_busy and (not self.manager or self.config.external_inference):
            raise InferenceError('The timed-out external opponent may still be working. Restart the external service and this application before Retry.')
        try:
            try:
                raw = request_json(self.settings.endpoint, self._payload(request, image),
                                   timeout or self.config.request_timeout)
            except InferenceError as exc:
                if not hasattr(exc, 'raw'):
                    raise
                return MoveResult('', self.settings.model, exc.raw, self.settings.backend, rejection=str(exc))
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
    client = getattr(client, 'client', client)
    if config.opponent_settings.backend == 'imajev':
        same = (config.opponent_settings.model == config.expected_model
                and config.opponent_settings.endpoint == config.endpoint)
        return ImajevOpponent(config, client if same and hasattr(client, 'decide') else None, manager)
    return HTTPOpponent(config, manager)


def role_metadata(config):
    from dataclasses import asdict
    opponent = asdict(config.opponent_settings)
    # Existing recordings already used this budget; preserve their role identity.
    if opponent['generation_max_tokens'] == 128:
        opponent.pop('generation_max_tokens')
    recognition = config.recognition_settings
    if recognition.backend == 'imajev':
        return {'version': 1, 'recognition': {'backend': 'imajev', 'model': recognition.model,
                                            'endpoint': recognition.endpoint}, 'opponent': opponent}
    return {'version': 2, 'recognition': dict(asdict(recognition),
            score_kind='choice_probability', min_choice_probability=config.threshold), 'opponent': opponent}
