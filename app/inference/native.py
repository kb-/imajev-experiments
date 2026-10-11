"""Shared native decision wire format, transport and answer validation."""
import base64
import json
import math
import httpx
from app.core.contracts import MoveResult
from app.inference.imajev_client import InferenceError


class Unsupported(InferenceError):
    """The complete request cannot be represented by this runtime."""


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


def native_payload(request, image, settings):
    body = dict(native_request(request), model=settings.model)
    if settings.backend == 'ollama':
        body['keep_alive'] = -1
    if image is not None:
        encoded = base64.b64encode(image).decode('ascii')
        body['images'] = [encoded if settings.backend == 'ollama' else 'data:image/png;base64,' + encoded]
    return body


def request_json(endpoint, payload, timeout):
    with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as transport:
        response = transport.post(endpoint, json=payload)
    if response.status_code in (400, 413, 501):
        raise Unsupported(f'Decision runtime cannot represent this request (HTTP {response.status_code}): {response.text[:300]}')
    if response.status_code != 200:
        raise InferenceError(f'Decision runtime returned HTTP {response.status_code}: {response.text[:300]}')
    try:
        def invalid_constant(value):
            raise ValueError(f'Nonfinite JSON number: {value}')
        return json.loads(response.content, parse_constant=invalid_constant)
    except ValueError as exc:
        error = InferenceError('Decision runtime returned malformed JSON.')
        error.raw = {'response_text': response.text}
        raise error from exc


def parse_native_choice(raw, name, question, settings):
    offered = question['criteria']
    answers = raw.get('answers')
    answer = answers.get(name) if isinstance(answers, dict) else None
    if isinstance(answer, dict) and answer.get('error') is not None:
        return MoveResult('', settings.model, raw, settings.backend,
                          rejection=f'Decision question {name}: {answer["error"]}')
    if not isinstance(answer, dict) or answer.get('type') != 'choice':
        return MoveResult('', settings.model, raw, settings.backend, rejection='Decision response is missing its choice answer.')
    choice = answer.get('choice')
    probabilities = answer.get('probabilities')
    rejection = None
    if not isinstance(choice, str) or choice not in offered:
        rejection = 'Decision returned an unoffered choice.'
    if probabilities is not None:
        if (not isinstance(probabilities, dict) or set(probabilities) != set(offered)
                or any(isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p)
                       or not 0 <= p <= 1 for p in probabilities.values())
                or abs(sum(probabilities.values()) - 1) > .02):
            rejection = 'Decision returned an invalid choice distribution.'
            probabilities = None
        elif isinstance(choice, str) and choice in probabilities and probabilities[choice] + 1e-6 < max(probabilities.values()):
            rejection = 'Decision choice does not match its highest probability.'
    confidence = answer.get('confidence')
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                   or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        rejection = 'Decision returned invalid confidence.'
        confidence = None
    abstained = answer.get('abstained')
    unknown = answer.get('unknown_probability')
    if abstained is not None and not isinstance(abstained, bool):
        rejection = 'Decision returned invalid abstention.'
        abstained = None
    if unknown is not None and (isinstance(unknown, bool) or not isinstance(unknown, (int, float))
                                or not math.isfinite(unknown) or not 0 <= unknown <= 1):
        rejection = 'Decision returned invalid unknown probability.'
        unknown = None
    return MoveResult(choice if isinstance(choice, str) else '', settings.model, raw, settings.backend,
                      probabilities, abstained, unknown, confidence, rejection)
