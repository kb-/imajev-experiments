"""Loopback-only WSL relay to an existing Windows Ollama daemon.

Windows PowerShell performs HTTP on Windows localhost. No daemon or model is
installed or started here; only this relay process is app-owned.
"""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

POWERSHELL = Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
PATHS = {'/api/ps', '/api/tags', '/api/chat', '/api/generate', '/api/show', '/api/version', '/v1/systemone'}
MAX_REQUEST_BYTES = 32 * 1024 * 1024
logger = logging.getLogger(__name__)


def configure_relay_logging(path):
    """HTTP response logging must survive the launching terminal disappearing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return handler


def forward(method, path, body, port):
    if method not in ('GET', 'POST') or path not in PATHS:
        raise ValueError('Unsupported Ollama relay request.')
    # Only fixed method/path values and a validated integer enter the script.
    script = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
        "$body=[Console]::In.ReadToEnd(); "
        f"$params=@{{Uri='http://127.0.0.1:{int(port)}{path}'; Method='{method}'; UseBasicParsing=$true; TimeoutSec=300}}; "
        "if ($body) { $params.Body=$body; $params.ContentType='application/json' }; "
        "try { $r=Invoke-WebRequest @params; @{status=[int]$r.StatusCode; body=$r.Content} | ConvertTo-Json -Compress } "
        "catch { if (!$_.Exception.Response) { throw }; "
        "$r=$_.Exception.Response; $reader=New-Object System.IO.StreamReader($r.GetResponseStream()); "
        "@{status=[int]$r.StatusCode; body=$reader.ReadToEnd()} | ConvertTo-Json -Compress }")
    # ASCII JSON avoids Windows console input encoding surprises; no shell interpolation.
    payload = json.dumps(json.loads(body), ensure_ascii=True) if body else ''
    completed = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-Command', script],
                               input=payload, capture_output=True, encoding='utf-8', timeout=310)
    if completed.returncode:
        raise RuntimeError('Windows Ollama request failed: ' + completed.stderr[:1000])
    response = json.loads(completed.stdout.lstrip('\ufeff'))
    return response['status'], response['body'].encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # BaseHTTPRequestHandler writes to stderr while sending response headers.
        # An orphaned WSL terminal can raise EIO there and abort every response.
        logger.info('%s %s', self.client_address[0], format % args)

    def handle_request(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 <= length <= MAX_REQUEST_BYTES:
                raise ValueError('Request is oversized.')
            status, body = forward(self.command, self.path, self.rfile.read(length), self.server.windows_port)
        except Exception as exc:
            logger.exception('Windows Ollama forwarding failed method=%s path=%s', self.command, self.path)
            status, body = 502, json.dumps({'error': str(exc)}).encode()
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.warning('Ollama client disconnected method=%s path=%s', self.command, self.path)
    do_GET = do_POST = handle_request


class RelayServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        logger.exception('Ollama relay request handler failed client=%s', client_address)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:11434')
    parser.add_argument('--log-file', type=Path,
                        default=Path(__file__).resolve().parents[2] / 'logs' / 'windows-ollama-bridge.log')
    args = parser.parse_args()
    url = urlsplit(args.url)
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost') or url.path not in ('', '/') or url.query or url.fragment or url.username:
        parser.error('Windows relay requires an HTTP IPv4 loopback URL.')
    if not POWERSHELL.exists():
        parser.error('Windows PowerShell is unavailable.')
    port = url.port or 11434
    handler = configure_relay_logging(args.log_file)
    try:
        with RelayServer(('127.0.0.1', port), Handler) as server:
            server.windows_port = port
            logger.info('Windows Ollama relay listening endpoint=%s pid=%s', args.url, os.getpid())
            server.serve_forever()
    except BaseException:
        logger.exception('Windows Ollama relay stopped')
        raise
    finally:
        logger.removeHandler(handler)
        handler.close()


if __name__ == '__main__': main()
