"""Small migration test application, not a production web-server template."""
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            status, body = 200, {'status': 'up'}
        elif self.path == '/api/check':
            env, token = os.environ.get('APP_ENV', ''), os.environ.get('DEMO_TOKEN', '')
            if not env or not token or '<redacted>' in token:
                status, body = 503, {'error': 'required configuration missing'}
            elif not hmac.compare_digest(self.headers.get('X-App-Token', '').encode(), token.encode()):
                status, body = 401, {'error': 'unauthorized'}
            else:
                status, body = 200, {'environment': env, 'configuration': 'ok'}
        else:
            status, body = 404, {'error': 'not found'}
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass  # Never log request headers or application credentials in this test app.


def make_server(host, port):
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == '__main__':
    make_server('0.0.0.0', int(os.environ.get('PORT', '8080'))).serve_forever()
