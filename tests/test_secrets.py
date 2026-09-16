import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

PATH = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'transfer_secrets.py')


class SecretTransfer(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(PATH), 'secret transfer helper must exist')
        spec = importlib.util.spec_from_file_location('transfer_secrets', PATH)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_extracts_selected_json_key_and_version(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({'SecretString': '{"username":"alice","password":"do-not-log"}'}).encode(), b'')
        with mock.patch.object(self.mod.subprocess, 'run', return_value=response) as run:
            value = self.mod.read_value('arn:aws:secretsmanager:us-east-1:123456789012:secret:db-AbCdEf:password::version-id')
        self.assertEqual(value, b'do-not-log')
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('--version-id') + 1], 'version-id')
        self.assertEqual(argv[argv.index('--secret-id') + 1], 'arn:aws:secretsmanager:us-east-1:123456789012:secret:db-AbCdEf')

    def test_ssm_value_preserves_newline(self):
        response = subprocess.CompletedProcess([], 0, b'{"Parameter":{"Value":"line1\\nline2\\n"}}', b'')
        with mock.patch.object(self.mod.subprocess, 'run', return_value=response):
            value = self.mod.read_value('arn:aws:ssm:us-east-1:123456789012:parameter/key')
        self.assertEqual(value, b'line1\nline2\n')

    def test_error_does_not_echo_secret_material(self):
        response = subprocess.CompletedProcess([], 1, b'super-secret', b'super-secret')
        with mock.patch.object(self.mod.subprocess, 'run', return_value=response):
            with self.assertRaises(SystemExit) as error:
                self.mod.read_value('arn:aws:ssm:us-east-1:123456789012:parameter/key')
        self.assertNotIn('super-secret', str(error.exception))

    def test_bad_selector_stops_without_returning_whole_secret(self):
        response = subprocess.CompletedProcess([], 0, b'{"SecretString":"{\\"username\\":\\"alice\\"}"}', b'')
        with mock.patch.object(self.mod.subprocess, 'run', return_value=response):
            with self.assertRaises(SystemExit):
                self.mod.read_value('arn:aws:secretsmanager:us-east-1:123456789012:secret:db-AbCdEf:missing::')

    def test_invalid_reference_does_not_call_aws(self):
        with mock.patch.object(self.mod.subprocess, 'run') as run:
            with self.assertRaises(SystemExit):
                self.mod.read_value('--profile other-account')
        run.assert_not_called()

    def test_transfer_writes_only_version_metadata(self):
        source = 'arn:aws:ssm:us-east-1:123456789012:parameter/key'
        seen = []
        def run(argv, **kw):
            seen.append((argv, kw))
            if argv[:3] == ['gcloud', 'secrets', 'list']:
                out = b''
            elif argv[:3] == ['aws', 'ssm', 'get-parameter']:
                out = b'{"Parameter":{"Value":"private-value"}}'
            elif argv[:4] == ['gcloud', 'secrets', 'versions', 'add']:
                out = b'projects/p/secrets/dest/versions/7\n'
            else:
                out = b''
            return subprocess.CompletedProcess(argv, 0, out, b'')
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(self.mod.subprocess, 'run', side_effect=run):
            path = os.path.join(tmp, 'versions.json')
            self.mod.transfer([{'source': source, 'secret': 'dest'}], 'my-project', path)
            with open(path) as f:
                self.assertEqual(json.load(f), {'dest': '7'})
        add = next((argv, kw) for argv, kw in seen if argv[:4] == ['gcloud', 'secrets', 'versions', 'add'])
        self.assertEqual(add[1]['input'], b'private-value')
        self.assertNotIn('private-value', ' '.join(add[0]))

    def test_dry_run_makes_no_provider_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'assessment.json')
            with open(path, 'w') as f:
                json.dump({'findings': [{'rule': 'secrets.env', 'verdict': 'supported', 'value': [
                    {'name': 'KEY', 'secret': 'dest', 'source': 'arn:aws:ssm:us-east-1:123456789012:parameter/key'}]}]}, f)
            with mock.patch('sys.argv', ['transfer_secrets.py', '--assessment', path, '--project', 'my-project']), mock.patch.object(self.mod.subprocess, 'run') as run:
                self.mod.main()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
