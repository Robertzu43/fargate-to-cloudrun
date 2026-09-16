#!/usr/bin/env python3
"""Plan or explicitly apply AWS -> Secret Manager copies without printing secret values.

Dry-run is the default. --apply reads the listed AWS secrets and adds Google secret versions.
Values exist in subprocess pipes and process memory, never command arguments or output files.
The output file contains destination version numbers only. Each apply creates new versions.
"""
import argparse
import base64
import json
import os
import re
import subprocess


def command(argv, data=None):
    try:
        p = subprocess.run(argv, input=data, capture_output=True)
    except OSError:
        raise SystemExit(f"Could not run {argv[0]}; check installation and authentication.") from None
    if p.returncode:
        # Provider errors may echo request payloads. Do not copy stdout/stderr into agent context.
        raise SystemExit(f"{argv[0]} {argv[1]} {argv[2]} failed (exit {p.returncode}); check permissions and resource metadata.")
    return p.stdout


def read_value(source):
    m = re.fullmatch(r"arn:(aws(?:-us-gov|-cn)?):(secretsmanager|ssm):([a-z0-9-]+):(\d{12}):(.+)", source)
    if not m:
        raise SystemExit("Secret source must be a full Secrets Manager or SSM ARN.")
    partition, provider, region, account, resource = m.groups()
    try:
        if provider == 'ssm':
            if not resource.startswith('parameter/'):
                raise ValueError()
            raw = command(['aws', 'ssm', 'get-parameter', '--name', source, '--with-decryption',
                           '--region', region, '--output', 'json'])
            return json.loads(raw)['Parameter']['Value'].encode('utf-8')
        parts = resource.split(':')
        if parts[0] != 'secret' or len(parts) not in (2, 5) or not parts[1]:
            raise ValueError()
        key, stage, version = parts[2:] if len(parts) == 5 else ('', '', '')
        if stage and version:
            raise ValueError()
        arn = f'arn:{partition}:{provider}:{region}:{account}:secret:{parts[1]}'
        args = ['aws', 'secretsmanager', 'get-secret-value', '--secret-id', arn,
                '--region', region, '--output', 'json']
        if stage:
            args += ['--version-stage', stage]
        if version:
            args += ['--version-id', version]
        payload = json.loads(command(args))
        if key:
            value = json.loads(payload['SecretString'])[key]
            if not isinstance(value, str):
                raise ValueError()
            return value.encode('utf-8')
        if 'SecretString' in payload:
            return payload['SecretString'].encode('utf-8')
        return base64.b64decode(payload['SecretBinary'], validate=True)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SystemExit('Secret reference or selected value is invalid; no secret content was logged.') from None


def transfer(mappings, project, versions_out):
    destinations = {}
    for item in mappings:
        name, source = item['secret'], item['source']
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,254}', name) or not isinstance(source, str):
            raise SystemExit('Invalid secret mapping.')
        if name in destinations and destinations[name] != source:
            raise SystemExit('Different sources map to one destination; resolve the collision first.')
        destinations[name] = source
    existing = {n.rsplit('/', 1)[-1] for n in command(
        ['gcloud', 'secrets', 'list', f'--project={project}', '--format=value(name)']).decode().splitlines()}
    versions = {}
    for name, source in destinations.items():
        value = read_value(source)
        if name not in existing:
            command(['gcloud', 'secrets', 'create', name, f'--project={project}', '--replication-policy=automatic'])
        output = command(['gcloud', 'secrets', 'versions', 'add', name, f'--project={project}',
                          '--data-file=-', '--format=value(name)'], data=value).decode().strip()
        del value
        version = output.rsplit('/', 1)[-1]
        if not re.fullmatch(r'[1-9][0-9]*', version):
            raise SystemExit('Google did not return a numeric secret version; inspect version metadata before retrying.')
        versions[name] = version
        # Preserve completed metadata if a later copy fails. No values are written here.
        with open(versions_out + '.tmp', 'w') as f:
            json.dump(versions, f, indent=2)
            f.write('\n')
        os.replace(versions_out + '.tmp', versions_out)
        print(f'Copied {name}, version {version} (value not displayed).')
    return versions


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--assessment', required=True)
    ap.add_argument('--project', required=True)
    ap.add_argument('--versions-out', default='secret-versions.json')
    ap.add_argument('--apply', action='store_true', help='authorize reading listed secret values and writing Google secret versions')
    a = ap.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', a.project):
        ap.error('invalid project id')
    with open(a.assessment) as f:
        assessment = json.load(f)
    mappings = [s for finding in assessment['findings'] if finding['rule'] == 'secrets.env'
                and finding['verdict'] == 'supported' for s in finding.get('value', [])]
    for s in mappings:
        print(f"{s['name']}: {s['source']} -> {a.project}/{s['secret']}")
    if not mappings:
        print('No supported secret references to transfer.')
    elif a.apply:
        transfer(mappings, a.project, a.versions_out)
    else:
        print('Plan only. Review the accounts, source selectors, and destination project before using --apply.')


if __name__ == '__main__':
    main()
