import copy
import json
import os
import re
import shlex
import subprocess
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


def gen(edit=lambda inv: None, rules=RULES, project="my-project", region="us-central1"):
    """stateless-http with `edit(inv)` applied -> (yaml_text, sh_text)."""
    inv = load("fixtures", "stateless-http", "inventory.json")
    edit(inv)
    findings = assess.assess(inv, HTTP_SRC, rules)
    return generate.generate({"meta": inv["meta"], "rollup": assess.rollup(findings), "findings": findings}, inv, project, region)


ECR_IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/web:1.0"


def set_image(image):
    def edit(inv):
        inv["taskDefinition"]["containerDefinitions"][0]["image"] = image
    return edit


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
        yaml_text, _ = generate.generate({"meta": inv["meta"], "rollup": roll, "findings": findings}, inv, "my-project", "us-central1")
        self.assertIn("# OMITTED: data — see finding storage.efs", yaml_text)
        self.assertNotIn("nfs:", yaml_text)

    def test_refuses_when_image_unsupported(self):
        rules = copy.deepcopy(RULES)
        next(r for r in rules["rules"] if r["id"] == "container.image")["stale"] = True
        with self.assertRaises(SystemExit) as cm:
            gen(rules=rules)
        self.assertIn("container.image", str(cm.exception))

    def test_refuses_ecr_public(self):
        with self.assertRaises(SystemExit) as cm:
            gen(set_image("public.ecr.aws/nginx/nginx:latest"))
        self.assertIn("ECR Public", str(cm.exception))

    def test_names_normalized(self):
        n = generate.names("My_Very_Long_Service_Name_With_Underscores_And_Even_More_Words", "my-project", "us-central1", "img")
        self.assertRegex(n["slug"], r"^[a-z][a-z0-9-]*[a-z0-9]$")
        self.assertLessEqual(len(n["slug"]), 49)
        self.assertRegex(n["sa_id"], r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
        self.assertEqual(n["sa_email"], f"{n['sa_id']}@my-project.iam.gserviceaccount.com")
        with self.assertRaises(SystemExit):
            generate.names("a", "my-project", "us-central1", "img")
        with self.assertRaises(SystemExit):
            gen(lambda inv: inv["meta"].__setitem__("service", "_"))

    def test_slug_used_in_manifest_and_commands(self):
        yaml_text, sh_text = gen(lambda inv: inv["meta"].__setitem__("service", "Web_API"))
        self.assertIn('  name: "web-api"', yaml_text)
        self.assertIn("gcloud run services describe web-api ", sh_text)
        self.assertIn("gcloud run services logs read web-api ", sh_text)
        self.assertIn("--display-name='Web_API on Cloud Run'", sh_text)

    def test_yaml_scalars_quoted(self):
        def edit(inv):
            inv["taskDefinition"]["containerDefinitions"][0]["environment"].append({"name": "D: E", "value": "v"})
            inv["targetGroups"][0]["healthCheckPath"] = "/health?x=1 # y"
        yaml_text, _ = gen(edit)
        self.assertIn('- name: "D: E"', yaml_text)
        self.assertIn('path: "/health?x=1 # y"', yaml_text)

    def test_health_path_shell_quoted(self):
        path = '"; echo pwned; "'
        _, sh_text = gen(lambda inv: inv["targetGroups"][0].__setitem__("healthCheckPath", path))
        self.assertIn(shlex.quote(path), sh_text)
        self.assertNotIn(path, sh_text.replace(shlex.quote(path), ""))
        self.assertIn("--format='value(status.url)')\"" + shlex.quote(path) + " ", sh_text)

    def test_ecr_path_steps(self):
        yaml_text, sh_text = gen(set_image(ECR_IMAGE))
        target = "us-central1-docker.pkg.dev/my-project/fargate-to-cloudrun/web:migrated"
        expected = [
            "gcloud artifacts repositories describe fargate-to-cloudrun --location=us-central1 --project=my-project >/dev/null 2>&1 || gcloud artifacts repositories create fargate-to-cloudrun",
            "aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com",
            "gcloud auth configure-docker us-central1-docker.pkg.dev",
            f"docker tag {ECR_IMAGE} {target}",
        ]
        pos = -1
        for e in expected:
            i = sh_text.find(e, pos + 1)
            self.assertGreater(i, pos, e)
            pos = i
        self.assertIn(f"- image: {target}  # ecs:", yaml_text)

    def test_deploy_sh_parses(self):
        for edit in (lambda inv: None, set_image(ECR_IMAGE)):
            _, sh_text = gen(edit)
            r = subprocess.run(["bash", "-n"], input=sh_text, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_shared_shell_state(self):
        for edit in (lambda inv: None, set_image(ECR_IMAGE)):
            _, sh_text = gen(edit)
            self.assertNotIn("REGION=", sh_text)
            self.assertNotIn("CLOUDSDK_CORE_PROJECT", sh_text)
            self.assertNotIn("$URL", sh_text)
            gcloud_lines = [l for l in sh_text.splitlines() if l.startswith("gcloud ")]
            self.assertTrue(gcloud_lines)
            for l in gcloud_lines:
                self.assertIn("--project=my-project", l)

    def test_rejects_bad_project_or_region(self):
        for project, region in (("My Project", "us-central1"), ("my-project", "$REGION"), ("my-project", "us-central")):
            with self.assertRaises(SystemExit):
                gen(project=project, region=region)


if __name__ == "__main__":
    unittest.main()
