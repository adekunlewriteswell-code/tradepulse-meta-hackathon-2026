"""Loopback, same-origin, bounded JSON and browser-CSRF protection for the local demo."""
from http.cookies import SimpleCookie
from ipaddress import ip_address
import re
import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from app.ai.meta import strict_json
from app.services.demo import digest

COOKIE = 'tp_local_demo'
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'; object-src 'none'")


def csrf(token):
    return digest('tradepulse-local-csrf-v1:' + token)


class DemoSecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not (scope['path'] == '/demo' or scope['path'].startswith('/demo/')):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope['headers']}

        async def safe_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] += [(b'content-security-policy', CSP.encode()),
                    (b'x-content-type-options', b'nosniff'), (b'x-frame-options', b'DENY'),
                    (b'referrer-policy', b'no-referrer'), (b'cache-control', b'no-store')]
            await send(message)

        async def reject(message, status=403):
            await JSONResponse({'detail': message}, status_code=status)(scope, receive, safe_send)

        try:
            local = ip_address(scope.get('client', ('', 0))[0]).is_loopback
            host = urlsplit('http://' + headers.get('host', ''))
            valid_host = (host.hostname in ('127.0.0.1', 'localhost', '::1') and host.path == ''
                          and not host.query and not host.fragment and host.username is None)
            valid_host = valid_host and (host.port is None or 1 <= host.port <= 65535)
        except (ValueError, TypeError):
            local = valid_host = False
        duplicates = any(sum(k.decode().lower() == name for k, _ in scope['headers']) > 1
                         for name in ('host', 'origin', 'cookie', 'x-demo-csrf'))
        if (not local or not valid_host or duplicates or scope['scheme'] != 'http'
                or any(k.startswith('x-forwarded-') or k == 'forwarded' for k in headers)):
            return await reject('This demonstration is restricted to direct HTTP loopback access.')
        origin = headers.get('origin')
        expected_origin = 'http://' + headers['host']
        if (origin is not None and origin != expected_origin
                or headers.get('sec-fetch-site') == 'cross-site'):
            return await reject('Cross-origin demo access is not allowed.')
        if scope['method'] not in ('GET', 'HEAD'):
            if scope['method'] != 'POST' or origin != expected_origin:
                return await reject('A same-origin POST is required.')
            jar = SimpleCookie()
            try:
                jar.load(headers.get('cookie', ''))
                token = jar[COOKIE].value if COOKIE in jar else ''
            except Exception:
                token = ''
            if (not re.fullmatch('[0-9a-f]{64}', token)
                    or not secrets.compare_digest(headers.get('x-demo-csrf', ''), csrf(token))):
                return await reject('Missing or invalid local demo CSRF token.')
            if headers.get('content-type', '').split(';')[0].lower() != 'application/json':
                return await reject('Use a JSON request.', 415)
            body = bytearray()
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    return
                body.extend(event.get('body', b''))
                if len(body) > 8192:
                    return await reject('Demo request exceeds 8192 bytes.', 413)
                if not event.get('more_body', False):
                    break
            try:
                strict_json(body.decode('utf-8'))
            except (ValueError, UnicodeDecodeError, RecursionError):
                return await reject('Malformed or duplicate-field JSON.', 400)

            async def replay():
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}

            return await self.app(scope, replay, safe_send)
        await self.app(scope, receive, safe_send)
