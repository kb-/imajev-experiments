import json
from urllib.parse import urlsplit, urlunsplit
import httpx


def coach_messages(request):
    return [{'role': 'system', 'content': 'You coach O in tic-tac-toe. Revise the previous strategy using accepted moves and outcomes, retaining useful older lessons. Return only a concise actionable strategy, in at most 120 words, using six short numbered rules. No analysis or thinking.'},
            {'role': 'user', 'content': json.dumps(request, ensure_ascii=False)}]


def shared_coach(client, request):
    parts = urlsplit(client.config.endpoint)
    with httpx.Client(timeout=300, trust_env=False) as transport:
        client._check_busy(transport)
        response = transport.post(urlunsplit((parts.scheme, parts.netloc, '/v1/coach', '', '')), json=request)
        if response.status_code != 200:
            raise RuntimeError(f'Coach HTTP {response.status_code}: {response.text[:4096]}')
        return response.json()
