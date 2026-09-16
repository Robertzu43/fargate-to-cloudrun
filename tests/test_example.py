"""A health response alone must not pass the application's functional checks."""
import importlib.util
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

PATH = os.path.join(os.path.dirname(__file__), '..', 'examples', 'configured-api', 'app.py')


class FunctionalExample(unittest.TestCase):
    def request(self, env, path, token=None):
        self.assertTrue(os.path.isfile(PATH), 'functional example must exist')
        spec = importlib.util.spec_from_file_location('example_app', PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with mock.patch.dict(os.environ, env, clear=True):
            server = mod.make_server('127.0.0.1', 0)
            server.timeout = 2
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()
            try:
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}',
                                             headers={'X-App-Token': token} if token else {})
                try:
                    response = urllib.request.urlopen(req, timeout=2)
                except urllib.error.HTTPError as e:
                    response = e
                with response:
                    return response.status, response.read().decode()
            finally:
                thread.join(timeout=3)
                server.server_close()

    def test_health_passes_while_missing_configuration_fails(self):
        self.assertEqual(self.request({}, '/health')[0], 200)
        self.assertEqual(self.request({}, '/api/check')[0], 503)

    def test_placeholder_credentials_fail_functional_check(self):
        status, body = self.request({'APP_ENV': 'staging', 'DEMO_TOKEN': '<redacted>'}, '/api/check', '<redacted>')
        self.assertEqual(status, 503)
        self.assertNotIn('<redacted>', body)

    def test_configured_application_checks_auth_and_returns_no_secret(self):
        env = {'APP_ENV': 'staging', 'DEMO_TOKEN': 'synthetic-test-token'}
        self.assertEqual(self.request(env, '/api/check', 'wrong')[0], 401)
        status, body = self.request(env, '/api/check', 'synthetic-test-token')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'environment': 'staging', 'configuration': 'ok'})
        self.assertNotIn('synthetic-test-token', body)


if __name__ == '__main__':
    unittest.main()
