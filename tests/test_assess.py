import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import assess  # noqa: E402

FIXTURES = os.path.join(ROOT, "fixtures")
EXEMPT = {"denied", "source-unavailable", "not-covered"}


def load(*parts):
    with open(os.path.join(ROOT, *parts)) as fh:
        return json.load(fh)


RULES = load("references", "rules.json")
FIXTURE_NAMES = sorted(n for n in os.listdir(FIXTURES) if os.path.isdir(os.path.join(FIXTURES, n)))


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


class TestHelpers(unittest.TestCase):
    def test_secret_name(self):
        self.assertEqual(assess.secret_name("arn:aws:secretsmanager:us-east-1:1:secret:prod/db-url-AbCdEf"), "prod-db-url")
        self.assertEqual(assess.secret_name("arn:aws:ssm:us-east-1:1:parameter/prod/api/key"), "prod-api-key")
        self.assertEqual(assess.secret_name("arn:aws:secretsmanager:us-east-1:1:secret:x-AbCdEf:json_key::"), "x")

    def test_fargate_size_rounds_cpu_up_to_one(self):
        self.assertEqual(assess.fargate_size({"cpu": "256", "memory": "512"}), (0.25, 512, 1))
        self.assertEqual(assess.fargate_size({"cpu": "4096", "memory": "8192"}), (4.0, 8192, 4))
        self.assertEqual(assess.fargate_size({"cpu": "16384", "memory": "32768"}), (16.0, 32768, 16))

    def test_scan_finds_python_and_js_clients(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.py"), "w") as fh:
                fh.write('import boto3\ns3 = boto3.client("s3")\n')
            with open(os.path.join(d, "b.ts"), "w") as fh:
                fh.write('import { SQSClient } from "@aws-sdk/client-sqs";\n')
            hits = assess.scan(d)
        self.assertEqual(sorted(hits), ["s3", "sqs"])
        self.assertEqual(hits["s3"], ["a.py:2"])


if __name__ == "__main__":
    unittest.main()
