import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import assess  # noqa: E402
import generate  # noqa: E402

FIXTURES = os.path.join(ROOT, "fixtures")
EXEMPT = {"denied", "source-unavailable", "not-covered"}


def load(*parts):
    with open(os.path.join(ROOT, *parts)) as fh:
        return json.load(fh)


RULES = load("references", "rules.json")
FIXTURE_NAMES = sorted(n for n in os.listdir(FIXTURES) if os.path.isdir(os.path.join(FIXTURES, n)))


HTTP_SRC = os.path.join(FIXTURES, "stateless-http", "src")


def http_case(edit, src=HTTP_SRC, rules=RULES):
    """Load stateless-http, apply `edit(inv)`, assess; return (findings, rollup)."""
    inv = load("fixtures", "stateless-http", "inventory.json")
    edit(inv)
    findings = assess.assess(inv, src, rules)
    return findings, assess.rollup(findings)


def run_fixture(name):
    inv = load("fixtures", name, "inventory.json")
    findings = assess.assess(inv, os.path.join(FIXTURES, name, "src"), RULES)
    return inv, findings, assess.rollup(findings)


def simplify(findings):
    return sorted(
        ({"rule": f["rule"], "verdict": f["verdict"], "subject": f.get("subject", "")} for f in findings),
        key=lambda x: (x["rule"], x["subject"]),
    )


class TestVerdicts(unittest.TestCase):
    def test_every_fixture_matches_expected(self):
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                expected = load("fixtures", name, "expected.json")
                _, findings, roll = run_fixture(name)
                self.assertEqual(roll, expected["rollup"])
                self.assertEqual(simplify(findings), expected["findings"])

    def test_no_false_pass(self):
        for name in FIXTURE_NAMES:
            expected = load("fixtures", name, "expected.json")
            if expected["rollup"] == "supported":
                continue
            with self.subTest(fixture=name):
                _, _, roll = run_fixture(name)
                self.assertNotEqual(roll, "supported")

    def test_every_cloud_run_finding_is_cited(self):
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                _, findings, _ = run_fixture(name)
                for f in findings:
                    if f.get("reason") in EXEMPT:
                        continue
                    with self.subTest(rule=f["rule"]):
                        self.assertTrue(f.get("url"), "missing url")
                        self.assertTrue(f.get("quote"), "missing quote")

    def test_arm64_or_windows_is_blocked(self):
        for rp in ({"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
                   {"cpuArchitecture": "X86_64", "operatingSystemFamily": "WINDOWS_SERVER_2022_CORE"}):
            with self.subTest(runtimePlatform=rp):
                inv = load("fixtures", "stateless-http", "inventory.json")
                inv["taskDefinition"]["runtimePlatform"] = rp
                findings = assess.assess(inv, os.path.join(FIXTURES, "stateless-http", "src"), RULES)
                self.assertEqual(assess.rollup(findings), "blocked")
                self.assertIn("container.architecture", {f["rule"] for f in findings})

    def test_x86_linux_platform_is_not_a_finding(self):
        inv = load("fixtures", "stateless-http", "inventory.json")
        inv["taskDefinition"]["runtimePlatform"] = {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"}
        findings = assess.assess(inv, os.path.join(FIXTURES, "stateless-http", "src"), RULES)
        self.assertEqual(assess.rollup(findings), "supported")

    def test_rules_and_checks_match(self):
        with open(os.path.join(ROOT, "scripts", "assess.py")) as fh:
            src = fh.read()
        used = set(re.findall(r'R\["([a-z.-]+)"\]', src))
        declared = {r["id"] for r in RULES["rules"]}
        self.assertEqual(used, declared)

    def test_missing_source_is_needs_investigation(self):
        inv = load("fixtures", "stateless-http", "inventory.json")
        findings = assess.assess(inv, None, RULES)
        reasons = {f.get("reason") for f in findings}
        self.assertIn("source-unavailable", reasons)
        self.assertEqual(assess.rollup(findings), "needs-investigation")

    def test_bogus_source_path_is_needs_investigation(self):
        findings, roll = http_case(lambda inv: None, src=os.path.join(FIXTURES, "no-such-dir"))
        self.assertIn("source-unavailable", {f.get("reason") for f in findings})
        self.assertEqual(roll, "needs-investigation")

    def test_source_without_recognized_files_is_needs_investigation(self):
        findings, roll = http_case(lambda inv: None, src=os.path.join(FIXTURES, "denied-permission", "src"))
        self.assertIn("source-unavailable", {f.get("reason") for f in findings})
        self.assertEqual(roll, "needs-investigation")

    def test_udp_only_port_is_not_covered(self):
        def edit(inv):
            inv["taskDefinition"]["containerDefinitions"][0]["portMappings"] = [{"containerPort": 5300, "protocol": "udp"}]
        findings, roll = http_case(edit)
        self.assertEqual(roll, "needs-investigation")
        self.assertIn("containerDefinitions[].portMappings[].protocol=udp", {f["subject"] for f in findings})
        self.assertNotIn("workload.background", {f["rule"] for f in findings})

    def test_tcp_target_group_is_not_covered(self):
        def edit(inv):
            inv["targetGroups"] = [{"targetGroupArn": "arn:tg", "healthCheckProtocol": "TCP", "port": 8080}]
        findings, roll = http_case(edit)
        self.assertEqual(roll, "needs-investigation")
        self.assertIn("targetGroups[].healthCheckProtocol=TCP", {f["subject"] for f in findings})

    def test_tcp_traffic_target_group_is_not_covered(self):
        def edit(inv):
            inv["targetGroups"] = [{"targetGroupArn": "arn:tg", "protocol": "TCP", "healthCheckProtocol": "HTTP", "healthCheckPath": "/"}]
        findings, roll = http_case(edit)
        self.assertEqual(roll, "needs-investigation")
        self.assertIn("targetGroups[].protocol=TCP", {f["subject"] for f in findings})
        self.assertNotIn("health.http-probe", {f["rule"] for f in findings})

    def test_mixed_target_groups_probe_http_and_flag_the_rest(self):
        def edit(inv):
            inv["targetGroups"] = [{"targetGroupArn": "arn:a", "protocol": "TCP", "healthCheckProtocol": "TCP"},
                                   {"targetGroupArn": "arn:b", "protocol": "HTTP", "healthCheckProtocol": "HTTP", "healthCheckPath": "/hc"}]
        findings, roll = http_case(edit)
        self.assertEqual(roll, "needs-investigation")
        self.assertEqual(next(f for f in findings if f["rule"] == "health.http-probe")["value"], "/hc")
        self.assertIn("targetGroups[].protocol=TCP", {f["subject"] for f in findings})

    def test_port_prefers_load_balancer_container_port(self):
        def edit(inv):
            inv["taskDefinition"]["containerDefinitions"][0]["portMappings"] = [
                {"containerPort": 9090, "protocol": "tcp"}, {"containerPort": 8080, "protocol": "tcp"}]
        findings, _ = http_case(edit)
        port = next(f for f in findings if f["rule"] == "container.port")
        self.assertEqual(port["value"], 8080)
        self.assertTrue(any("multiple ports" in e for e in port["evidence"]))

    def test_memory_above_cpu_tier_bumps_cpu(self):
        def edit(inv):
            inv["taskDefinition"].update(cpu="1024", memory="8192")
        findings, roll = http_case(edit)
        self.assertEqual(roll, "supported")
        self.assertEqual(next(f for f in findings if f["rule"] == "resources.cpu-memory")["value"]["cpu"], "2")

    def test_out_of_range_cpu_memory_is_blocked(self):
        for cpu, mem in (("16384", "32768"), ("1024", "65536")):
            with self.subTest(cpu=cpu, memory=mem):
                findings, roll = http_case(lambda inv: inv["taskDefinition"].update(cpu=cpu, memory=mem))
                self.assertEqual(roll, "blocked")
                self.assertIn("resources.unsupported-pair", {f["rule"] for f in findings})

    def test_unparseable_size_is_blocked_not_an_exception(self):
        for cpu in (None, "1024abc"):
            with self.subTest(cpu=cpu):
                findings, roll = http_case(lambda inv: inv["taskDefinition"].update(cpu=cpu))
                self.assertEqual(roll, "blocked")
                pair = next(f for f in findings if f["rule"] == "resources.unsupported-pair")
                self.assertIn("missing or unparseable", pair["evidence"][0])

    def test_stale_supported_rule_downgrades(self):
        rules = json.loads(json.dumps(RULES))
        next(r for r in rules["rules"] if r["id"] == "container.port")["stale"] = True
        findings, roll = http_case(lambda inv: None, rules=rules)
        self.assertEqual(roll, "needs-investigation")
        self.assertEqual(next(f for f in findings if f["rule"] == "container.port")["reason"], "stale")

    def test_unknown_container_field_is_not_covered(self):
        def edit(inv):
            inv["taskDefinition"]["containerDefinitions"][0]["linuxParameters"] = {"initProcessEnabled": True}
        findings, roll = http_case(edit)
        self.assertNotEqual(roll, "supported")
        self.assertIn("containerDefinitions[].linuxParameters", {f["subject"] for f in findings if f.get("reason") == "not-covered"})

    def test_non_awslogs_driver_is_not_covered(self):
        def edit(inv):
            inv["taskDefinition"]["containerDefinitions"][0]["logConfiguration"] = {"logDriver": "splunk", "options": {}}
        findings, roll = http_case(edit)
        self.assertEqual(roll, "needs-investigation")
        self.assertIn("containerDefinitions[].logConfiguration.logDriver=splunk", {f["subject"] for f in findings})


class TestHelpers(unittest.TestCase):
    def test_secret_name(self):
        self.assertEqual(assess.secret_name("arn:aws:secretsmanager:us-east-1:1:secret:prod/db-url-AbCdEf"), "prod-db-url")
        self.assertEqual(assess.secret_name("arn:aws:ssm:us-east-1:1:parameter/prod/api/key"), "prod-api-key")
        self.assertEqual(assess.secret_name("arn:aws:secretsmanager:us-east-1:1:secret:x-AbCdEf:json_key::"), "x")
        self.assertEqual(assess.secret_name("arn:aws:secretsmanager:us-east-1:1:secret:my-db-secret"), "my-db-secret")

    def test_fargate_size_rounds_cpu_up_to_one(self):
        self.assertEqual(assess.fargate_size({"cpu": "256", "memory": "512"}), (0.25, 512, 1))
        self.assertEqual(assess.fargate_size({"cpu": "4096", "memory": "8192"}), (4.0, 8192, 4))
        self.assertEqual(assess.fargate_size({"cpu": "16384", "memory": "32768"}), (16.0, 32768, 16))

    def test_fargate_size_accepts_unit_forms_and_missing_values(self):
        self.assertEqual(assess.fargate_size({"cpu": "1 vCPU", "memory": "2 GB"}), (1.0, 2048, 1))
        self.assertEqual(assess.fargate_size({"cpu": None}), (None, None, None))

    def test_scan_finds_python_and_js_clients(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.py"), "w") as fh:
                fh.write('import boto3\ns3 = boto3.client("s3")\nddb = session.client("dynamodb")\n')
            with open(os.path.join(d, "b.ts"), "w") as fh:
                fh.write('import { SQSClient } from "@aws-sdk/client-sqs";\n')
            hits, scanned = assess.scan(d)
        self.assertEqual(scanned, 2)
        self.assertEqual(sorted(hits), ["dynamodb", "s3", "sqs"])
        self.assertEqual(hits["s3"], ["a.py:2"])


class TestGenerate(unittest.TestCase):
    def test_golden_stateless_http(self):
        inv, findings, roll = run_fixture("stateless-http")
        assessment = {"meta": inv["meta"], "rollup": roll, "findings": findings}
        yaml_text, sh_text = generate.generate(assessment, inv, "my-project", "us-central1")
        g = os.path.join(FIXTURES, "stateless-http", "golden")
        with open(os.path.join(g, "service.yaml")) as fh:
            self.assertEqual(yaml_text, fh.read())
        with open(os.path.join(g, "deploy.sh")) as fh:
            self.assertEqual(sh_text, fh.read())

    def test_refuses_blocked(self):
        inv, findings, roll = run_fixture("sqs-worker")
        with self.assertRaises(SystemExit):
            generate.generate({"meta": inv["meta"], "rollup": roll, "findings": findings}, inv, "p", "r")

    def test_omits_non_supported(self):
        inv, findings, roll = run_fixture("efs-mount")
        yaml_text, _ = generate.generate({"meta": inv["meta"], "rollup": roll, "findings": findings}, inv, "p", "us-central1")
        self.assertIn("# OMITTED: data — see finding storage.efs", yaml_text)
        self.assertNotIn("nfs:", yaml_text)


if __name__ == "__main__":
    unittest.main()
