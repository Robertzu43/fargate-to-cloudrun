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


class ResolutionRegressions(unittest.TestCase):
    """A finding that no amount of further collection can turn into `supported` -- a denied AWS call,
    an absent source tree -- used to lock the generator out of every real service permanently."""

    def denied(self):
        inv = copy.deepcopy(BASE)
        inv['denied'] = [{'call': 'ecs:DescribeServices', 'error': 'AccessDenied'}]
        return inv, assess.assess(inv, SRC, RULES)

    def test_denied_call_blocks_until_adjudicated(self):
        _, findings = self.denied()
        self.assertEqual(assess.rollup(findings), 'blocked')

    def test_resolution_clears_the_finding_and_records_why(self):
        inv, findings = self.denied()
        findings = assess.apply_resolutions(findings, {
            'denied:ecs:DescribeServices': {'decision': 'read from the exported service JSON instead',
                                            'evidence': ['service.json sha256 abc']}})
        f = next(f for f in findings if f['rule'] == 'denied')
        self.assertEqual(assess.rollup(findings), 'supported')
        self.assertEqual(f['original_verdict'], 'blocked')
        self.assertEqual(f['resolved']['decision'], 'read from the exported service JSON instead')
        generate.generate({'meta': inv['meta'], 'rollup': 'supported', 'findings': findings}, inv,
                          'my-project', 'us-central1',
                          secret_versions={s['secret']: '1' for f in findings
                                           if f['rule'] == 'secrets.env' for s in f['value']})

    def test_stale_resolution_is_an_error_not_a_silent_noop(self):
        _, findings = self.denied()
        with self.assertRaises(SystemExit):
            assess.apply_resolutions(findings, {'denied:ecs:DescribeTasks': {'decision': 'x'}})

    def test_resolution_without_a_decision_is_rejected(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump({'denied:ecs:DescribeServices': {'evidence': ['x']}}, fh)
        self.addCleanup(os.unlink, fh.name)
        with self.assertRaises(SystemExit):
            assess.load_resolutions(fh.name)

    def test_resolved_findings_are_visible_in_the_summary(self):
        _, findings = self.denied()
        findings = assess.apply_resolutions(findings, {
            'denied:ecs:DescribeServices': {'decision': 'exported JSON used instead'}})
        out = assess.summary(findings, assess.rollup(findings))
        self.assertIn('resolved by decision', out)
        self.assertIn('exported JSON used instead', out)


class InventoryAnnotationRegressions(unittest.TestCase):
    def test_provenance_notes_are_not_findings(self):
        """A hand-built inventory has to be able to say where it came from without earning findings
        that then block the generator."""
        inv = copy.deepcopy(BASE)
        inv['taskDefinition']['WARNING_image'] = 'IaC-declared, not the deployed image'
        inv['taskDefinition']['containerDefinitions'][0]['NOTE_secrets'] = 'ARNs are placeholders'
        inv['_provenance'] = 'hand-built from OpenTofu'
        self.assertEqual(result(inv)['rollup'], 'supported')


class SecretIdRegressions(unittest.TestCase):
    def test_ids_stay_readable_when_nothing_collides(self):
        """Operators have to recognize an id in the Secret Manager console."""
        mapped = assess.secret_mappings([
            {'name': 'A', 'valueFrom': 'arn:aws:secretsmanager:us-east-1:1:secret:app/db-url-AbCdEf'}])
        self.assertEqual(mapped[0]['secret'], 'app-db-url')

    def test_colliding_sources_still_get_distinct_ids(self):
        prefix = 'arn:aws:secretsmanager:us-east-1:1:secret:db-AbCdEf'
        mapped = assess.secret_mappings([{'name': 'U', 'valueFrom': prefix + ':username::'},
                                         {'name': 'P', 'valueFrom': prefix + ':password::'}])
        self.assertNotEqual(mapped[0]['secret'], mapped[1]['secret'])
        self.assertTrue(all(m['secret'].startswith('db-') for m in mapped))


class ResourceRegressions(unittest.TestCase):
    def test_sub_vcpu_task_says_how_far_it_was_raised(self):
        """0.25 vCPU silently becoming 1 is a 4x cost and behavior change."""
        inv = copy.deepcopy(BASE)
        inv['taskDefinition']['cpu'] = '256'
        inv['taskDefinition']['memory'] = '512'
        f = next(f for f in result(inv)['findings'] if f['rule'] == 'resources.cpu-memory')
        self.assertTrue(any('raises the allocation 4x' in e for e in f['evidence']), f['evidence'])
        y, _ = generate.generate(result(inv), inv, 'my-project', 'us-central1',
                                 secret_versions={s['secret']: '1' for g in result(inv)['findings']
                                                  if g['rule'] == 'secrets.env' for s in g['value']})
        self.assertIn('raises the allocation 4x', y)


class LiveServiceShapeRegressions(unittest.TestCase):
    """describe-services returns far more than the fixtures carried. Control-plane bookkeeping must not
    become findings that block the generator, and the fields that do decide a migration must survive."""

    LIVE_ONLY = {'availabilityZoneRebalancing': 'DISABLED', 'currentServiceRevisions': [{'arn': 'x'}],
                 'deploymentController': {'type': 'ECS'}, 'enableECSManagedTags': True,
                 'placementConstraints': [], 'placementStrategy': [], 'platformFamily': 'Linux',
                 'platformVersion': 'LATEST', 'propagateTags': 'SERVICE', 'resourceManagementType': 'CUSTOMER',
                 'roleArn': 'arn:aws:iam::1:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS',
                 'schedulingStrategy': 'REPLICA'}
    MEANINGFUL = {'serviceRegistries': [{'registryArn': 'arn:aws:servicediscovery:us-east-1:1:service/srv-1'}],
                  'healthCheckGracePeriodSeconds': 120,
                  'deploymentConfiguration': {'maximumPercent': 200},
                  'enableExecuteCommand': True}

    def uncovered_for(self, extra):
        inv = copy.deepcopy(BASE)
        inv['service'].update(extra)
        return {f['subject'] for f in result(inv)['findings'] if f['rule'] == 'not-covered'}

    def test_control_plane_fields_are_not_findings(self):
        self.assertEqual(self.uncovered_for(self.LIVE_ONLY), set())

    def test_fields_that_decide_a_migration_stay_findings(self):
        found = self.uncovered_for(self.MEANINGFUL)
        for k in self.MEANINGFUL:
            self.assertIn('service.' + k, found)


ECR_REF = '123456789012.dkr.ecr.us-east-1.amazonaws.com/web:v7'
DIGEST = 'sha256:' + 'a' * 64


def ecr_inv(with_digest=True, **service):
    inv = copy.deepcopy(BASE)
    inv['taskDefinition']['containerDefinitions'][0]['image'] = ECR_REF
    if with_digest:
        inv['imageDigests'] = {ECR_REF: DIGEST}
    inv['service'].update(service)
    return inv


def gen_for(inv, **kw):
    a = result(inv)
    versions = {s['secret']: '1' for f in a['findings'] if f['rule'] == 'secrets.env' for s in f['value']}
    return generate.generate(a, inv, 'my-project', 'us-central1', secret_versions=versions, **kw)


class ImageIdentityRegressions(unittest.TestCase):
    """A tag can be repointed after the revision is created; a digest cannot. The manifest has to
    pin what was actually assessed."""

    def test_manifest_pins_the_digest_when_one_was_collected(self):
        y, sh = gen_for(ecr_inv())
        self.assertIn('@' + DIGEST, y)
        self.assertNotIn(':migrated"', y)
        # The copy still pushes a tag -- you cannot push to a digest -- so the script must prove
        # the pushed image is the same one.
        self.assertIn(':migrated', sh)
        self.assertIn("image_summary.digest", sh)
        self.assertIn(DIGEST, sh)

    def test_falls_back_to_the_tag_when_no_digest_was_collected(self):
        y, sh = gen_for(ecr_inv(with_digest=False))
        self.assertIn(':migrated', y)
        self.assertNotIn('image_summary.digest', sh)

    def test_registry_repo_is_configurable(self):
        y, sh = gen_for(ecr_inv(), repo='platform')
        self.assertIn('my-project/platform/', y)
        self.assertIn('repositories describe platform', sh)
        self.assertNotIn('fargate-to-cloudrun', y)


class ScalingRegressions(unittest.TestCase):
    def test_min_instances_defaults_to_zero_and_keeps_the_count_as_evidence(self):
        """Copying desiredCount into minScale bills idle instances for a service that may not need them."""
        y, _ = gen_for(ecr_inv(desiredCount=4))
        self.assertIn('autoscaling.knative.dev/minScale: "0"', y)
        self.assertIn('service.desiredCount=4', y)

    def test_min_instances_can_be_raised_deliberately(self):
        y, _ = gen_for(ecr_inv(desiredCount=4), min_instances=2)
        self.assertIn('autoscaling.knative.dev/minScale: "2"', y)


class ProvenanceRegressions(unittest.TestCase):
    """Without AWS credentials the only inventory an agent can build is from an export. That path
    has to exist, and it has to say loudly that it is not the live API."""

    def test_exports_replace_the_api_calls_and_are_labelled(self):
        with tempfile.TemporaryDirectory() as d:
            sp, tp = os.path.join(d, 'svc.json'), os.path.join(d, 'td.json')
            json.dump({'services': [BASE['service']]}, open(sp, 'w'))
            json.dump({'taskDefinition': BASE['taskDefinition']}, open(tp, 'w'))
            with mock.patch.object(inventory, 'aws', return_value={}) as m:
                inv = inventory.collect('c', 's', 'us-east-1', (), service_file=sp, taskdef_file=tp)
            called = [c.args[:2] for c in m.call_args_list]
        self.assertNotIn(('ecs', 'describe-services'), called)
        self.assertNotIn(('ecs', 'describe-task-definition'), called)
        self.assertTrue(inv['meta']['provenance']['service'].startswith('file:'))
        self.assertTrue(inv['meta']['provenance']['taskDefinition'].startswith('file:'))
        self.assertEqual(inv['taskDefinition']['containerDefinitions'][0]['name'],
                         BASE['taskDefinition']['containerDefinitions'][0]['name'])

    def test_api_collection_is_labelled_too(self):
        with mock.patch.object(inventory, 'aws', return_value={}):
            inv = inventory.collect('c', 's', 'us-east-1')
        self.assertEqual(inv['meta']['provenance'], {'service': 'aws-api', 'taskDefinition': 'aws-api'})
