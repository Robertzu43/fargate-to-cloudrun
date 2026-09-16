"""Regressions for migration correctness, not just stable generated text."""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import assess
import generate
import inventory

ROOT = os.path.dirname(os.path.dirname(__file__))
with open(os.path.join(ROOT, 'references', 'rules.json')) as f:
    RULES = json.load(f)
with open(os.path.join(ROOT, 'fixtures', 'stateless-http', 'inventory.json')) as f:
    BASE = json.load(f)
SRC = os.path.join(ROOT, 'fixtures', 'stateless-http', 'src')


def result(inv):
    findings = assess.assess(inv, SRC, RULES)
    return {'meta': inv['meta'], 'rollup': assess.rollup(findings), 'findings': findings}


class MigrationRegressions(unittest.TestCase):
    def test_environment_is_private_by_default(self):
        env = [{'name': 'DATABASE_URL', 'value': 'postgres://user:password@host/db'},
               {'name': 'APP_ENV', 'value': 'production'}]
        self.assertTrue(all(e['value'] == '<redacted>' for e in inventory.redact(env)))

    def test_redacted_values_cannot_be_deployed(self):
        inv = copy.deepcopy(BASE)
        inv['taskDefinition']['containerDefinitions'][0]['environment'].append(
            {'name': 'API_KEY', 'value': '<redacted>'})
        a = result(inv)
        self.assertNotEqual(a['rollup'], 'supported')
        with self.assertRaises(SystemExit):
            generate.generate(a, inv, 'my-project', 'us-central1')

    def test_secret_selectors_remain_distinct(self):
        inv = copy.deepcopy(BASE)
        prefix = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:db-AbCdEf'
        inv['taskDefinition']['containerDefinitions'][0]['secrets'] = [
            {'name': 'DB_USER', 'valueFrom': prefix + ':username::'},
            {'name': 'DB_PASS', 'valueFrom': prefix + ':password::'}]
        mapped = next(f['value'] for f in result(inv)['findings'] if f['rule'] == 'secrets.env')
        self.assertNotEqual(mapped[0]['secret'], mapped[1]['secret'])
        self.assertEqual(mapped[0].get('source'), prefix + ':username::')

    def test_normalized_secret_names_do_not_collide(self):
        inv = copy.deepcopy(BASE)
        inv['taskDefinition']['containerDefinitions'][0]['secrets'] = [
            {'name': 'A', 'valueFrom': 'arn:aws:ssm:us-east-1:123456789012:parameter/a/b'},
            {'name': 'B', 'valueFrom': 'arn:aws:ssm:us-east-1:123456789012:parameter/a-b'}]
        mapped = next(f['value'] for f in result(inv)['findings'] if f['rule'] == 'secrets.env')
        self.assertNotEqual(mapped[0]['secret'], mapped[1]['secret'])

    def test_missing_target_group_is_not_a_pass(self):
        inv = copy.deepcopy(BASE)
        inv['targetGroups'] = []
        self.assertEqual(result(inv)['rollup'], 'needs-investigation')

    def test_multiple_ports_need_investigation(self):
        inv = copy.deepcopy(BASE)
        inv['taskDefinition']['containerDefinitions'][0]['portMappings'].append({'containerPort': 9090})
        self.assertEqual(result(inv)['rollup'], 'needs-investigation')

    def test_no_silent_omission_of_required_storage(self):
        with open(os.path.join(ROOT, 'fixtures', 'efs-mount', 'inventory.json')) as f:
            inv = json.load(f)
        with self.assertRaises(SystemExit):
            generate.generate(result(inv), inv, 'my-project', 'us-central1')

    def test_missing_source_is_not_a_deployable_assessment(self):
        inv = copy.deepcopy(BASE)
        findings = assess.assess(inv, None, RULES)
        with self.assertRaises(SystemExit):
            generate.generate({'rollup': assess.rollup(findings), 'findings': findings},
                              inv, 'my-project', 'us-central1')

    def test_generator_does_not_trust_rollup_over_findings(self):
        inv = copy.deepcopy(BASE)
        a = result(inv)
        a['rollup'] = 'supported'
        a['findings'].append({'rule': 'denied', 'verdict': 'blocked', 'subject': 'ecs'})
        with self.assertRaises(SystemExit):
            generate.generate(a, inv, 'my-project', 'us-central1')

    def test_collect_preserves_unmapped_service_configuration(self):
        svc = {'serviceArn': 'arn:svc', 'taskDefinition': 'arn:task', 'desiredCount': 1,
               'serviceConnectConfiguration': {'enabled': True}}
        def aws(*args, **kwargs):
            return {('sts', 'get-caller-identity'): {'Account': '123'},
                    ('ecs', 'describe-services'): {'services': [svc]},
                    ('ecs', 'describe-task-definition'): {'taskDefinition': {'containerDefinitions': []}}}[args[:2]]
        with mock.patch.object(inventory, 'aws', aws):
            inv = inventory.collect('cluster', 'web', 'us-east-1')
        self.assertIn('serviceConnectConfiguration', inv['service'])

    def test_copy_is_explicitly_amd64(self):
        inv = copy.deepcopy(BASE)
        inv['taskDefinition']['containerDefinitions'][0]['image'] = '123456789012.dkr.ecr.us-east-1.amazonaws.com/web:1'
        # This case exercises only image handling.
        inv['taskDefinition']['containerDefinitions'][0]['environment'] = []
        inv['taskDefinition']['containerDefinitions'][0]['secrets'] = []
        _, script = generate.generate(result(inv), inv, 'my-project', 'us-central1')
        self.assertIn('docker pull --platform linux/amd64', script)

    def test_requires_actual_secret_versions(self):
        inv = copy.deepcopy(BASE)
        with self.assertRaises(SystemExit):
            generate.generate(result(inv), inv, 'my-project', 'us-central1')

    def test_uses_returned_secret_version_instead_of_first_version(self):
        inv = copy.deepcopy(BASE)
        a = result(inv)
        secret = next(f['value'][0]['secret'] for f in a['findings'] if f['rule'] == 'secrets.env')
        yaml, _ = generate.generate(a, inv, 'my-project', 'us-central1', secret_versions={secret: '7'})
        self.assertIn('key: "7"', yaml)

    def test_inventory_binding_rejects_a_different_inventory(self):
        inv = copy.deepcopy(BASE)
        a = result(inv)
        a['inventory_sha256'] = '0' * 64
        with self.assertRaises(SystemExit):
            generate.generate(a, inv, 'my-project', 'us-central1')

    def test_invalid_container_port_is_not_supported(self):
        for port in (-1, 65536, '8080\nextra: value'):
            with self.subTest(port=port):
                inv = copy.deepcopy(BASE)
                inv['taskDefinition']['containerDefinitions'][0]['portMappings'] = [{'containerPort': port}]
                self.assertNotEqual(result(inv)['rollup'], 'supported')

    def test_missing_collected_role_evidence_is_reported(self):
        inv = copy.deepcopy(BASE)
        del inv['taskRoleActions']
        self.assertEqual(result(inv)['rollup'], 'needs-investigation')


if __name__ == '__main__':
    unittest.main()
