"""Drawing recognizers, independent of games and the GUI."""
from dataclasses import asdict, replace
import time
from typing import Protocol

import httpx

from app.core.contracts import RecognitionAnswer, RecognitionResult, Reply
from app.inference.imajev_client import ImajevClient, InferenceError, Unavailable
from app.inference.native import Unsupported, native_payload, parse_native_choice, request_json


class RecognitionBackend(Protocol):
    def recognize(self, request: dict, image: bytes) -> RecognitionResult: ...
    def warmup(self, request: dict, image: bytes) -> RecognitionResult: ...


def from_imajev(reply: Reply) -> RecognitionResult:
    return RecognitionResult(reply.model, {
        name: RecognitionAnswer(answer.choice, dict(answer.probabilities),
                                answer.unknown_probability, answer.abstained,
                                reply.raw.get('answers', {}).get(name, {}).get('confidence'))
        for name, answer in reply.answers.items()}, reply.raw, 'imajev')


def metadata(result):
    return {'backend': result.backend, 'model': result.model, 'score_kind': result.score_kind,
            'answers': {name: dict(asdict(answer), score=result.score(name))
                        for name, answer in result.answers.items()}}


def parse_recognition(raw, request, settings):
    if not isinstance(raw, dict) or raw.get('model') != settings.model:
        error = InferenceError(f'Expected recognition model {settings.model}; received a different model identity.')
        error.raw = raw
        raise error
    answers = {}
    for name, question in request['questions'].items():
        parsed = parse_native_choice(raw, name, question, settings)
        if parsed.rejection or parsed.probabilities is None:
            error = InferenceError(f'Recognition answer {name}: ' +
                                   (parsed.rejection or 'a measured choice distribution is required.'))
            error.raw = raw
            raise error
        answers[name] = RecognitionAnswer(parsed.choice, parsed.probabilities,
                                         parsed.unknown_probability, parsed.abstained, parsed.confidence)
    return RecognitionResult(settings.model, answers, raw, settings.backend)


class ImajevRecognizer:
    def __init__(self, config, client=None, manager=None):
        self.settings = config.recognition_settings
        self.config = replace(config, endpoint=self.settings.endpoint, expected_model=self.settings.model)
        self.client = client or ImajevClient(self.config)
        self.manager = manager

    def _invoke(self, request, image, warmup=False):
        if image is None:
            raise Unsupported('Drawing recognition requires an image.')
        self.client.manager = self.manager
        method = self.client.warmup if warmup else self.client.decide
        started = time.monotonic()
        reply = method(request, image)
        result = from_imajev(reply)
        return replace(result, raw={**result.raw, 'recognition_seconds': time.monotonic() - started})

    def recognize(self, request, image):
        return self._invoke(request, image)

    def warmup(self, request, image):
        return self._invoke(request, image, warmup=True)


class NativeRecognizer:
    def __init__(self, config, manager=None):
        self.config, self.settings, self.manager = config, config.recognition_settings, manager
        self.uncertain_busy = False

    def preflight(self, request, image):
        if (self.settings.backend not in ('llama_cpp', 'ollama') or self.settings.protocol != 'systemone'
                or self.settings.input_mode != 'text_image'):
            raise Unsupported('Recognition requires an image-capable native System One backend.')
        if image is None:
            raise Unsupported('Drawing recognition requires an image.')
        questions = request.get('questions', {})
        if not questions or any(q.get('type') != 'choice' or len(q.get('criteria', {})) < 2
                                for q in questions.values()):
            raise Unsupported('Recognition requires named choice questions with at least two options.')
        largest = max(len(q['criteria']) for q in questions.values())
        if largest > self.settings.max_choices:
            raise Unsupported(f'{self.settings.model} supports at most {self.settings.max_choices} choices; '
                              f'recognition requires all {largest}. No choices were removed.')

    def recognize(self, request, image, timeout=None):
        self.preflight(request, image)
        managed = self.manager is not None and not self.config.external_inference
        if self.uncertain_busy and not managed:
            raise InferenceError('The timed-out external recognizer may still be working. Restart its service and this application before Retry.')

        def operation():
            if managed:
                self.uncertain_busy = False
            try:
                raw = request_json(self.settings.endpoint, native_payload(request, image, self.settings),
                                   timeout or self.config.request_timeout)
                return parse_recognition(raw, request, self.settings)
            except httpx.TimeoutException as exc:
                self.uncertain_busy = True
                raise InferenceError('Recognition timed out; GPU release must be confirmed before Retry.') from exc
            except httpx.RequestError as exc:
                raise Unavailable('Cannot reach the configured recognition service.') from exc
        if managed:
            return self.manager.run_backend(self.settings, operation)
        return operation()

    def warmup(self, request, image):
        # Validates image execution and response shape, not blank-ink acceptance.
        return self.recognize(request, image, timeout=self.config.startup_timeout)


def create_recognizer(config, client=None, manager=None):
    if config.recognition_settings.backend == 'imajev':
        return ImajevRecognizer(config, client, manager)
    return NativeRecognizer(config, manager)
