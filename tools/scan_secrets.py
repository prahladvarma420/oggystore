#!/usr/bin/env python3
"""Offline, standard-library credential-pattern scan (not a security guarantee).

Scans text in the repository, INCLUDING tests and docs. Omits VCS/dependency/
runtime artifacts, never prints matched values. Obvious placeholders are allowed
only for generic assignments; provider token/private-key signatures always fail.
This is a high-confidence pattern scan, not arbitrary-secret/entropy detection.
"""
from __future__ import annotations
import argparse
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', '.venv', 'venv', 'env', 'node_modules', '__pycache__',
             '.test-results', 'logs', 'backups', 'restore_uploads', '.pytest_cache'}
SKIP_SUFFIXES = {'.pyc', '.pyo', '.zip', '.db', '.sqlite', '.sqlite3', '.log',
                 '.png', '.jpg', '.jpeg', '.gif', '.ico', '.woff', '.woff2', '.pdf'}
PATTERNS = {
    'telegram-bot-token': re.compile(r'(?<![\w])\d{6,12}:[A-Za-z0-9_-]{35}(?![\w-])'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,255})\b'),
    'aws-access-key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----'),
    'credential-url': re.compile(r'\b(?:postgres(?:ql)?|mysql|https?|socks5)://[^\s/:@]+:([^\s/@]+)@[^\s/]+'),
}
ASSIGNMENT = re.compile(
    r'''(?ix)["']?\b(?:[a-z_]*?(?:api_key|api_secret|secret_key|access_token|auth_token|bot_token|password|passwd|encryption_key|hmac_secret))\b["']?\s*[:=]\s*["']([^"'\r\n]{16,})["']''')
# Whole values, not substring 'test': an arbitrary real token containing 'test'
# must not be exempted. Provider-specific signatures above bypass this allowlist.
PLACEHOLDER = re.compile(r'(?ix)^(?:<[^>]+>|\$\{[^}]+\}|(?:your|example|placeholder|dummy|synthetic|test|review|ci)[-_ ](?:[a-z0-9_-]+)|x+|\*+)$')


def entropy(value):
    return -sum((value.count(c) / len(value)) * math.log2(value.count(c) / len(value)) for c in set(value))


def scan_text(text):
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for category, pattern in PATTERNS.items():
            for match in pattern.finditer(line):
                if category == 'credential-url':
                    password = match.group(1)
                    if password.lower() in {'pass', 'password', 'secret', 'example', 'test', '...'} or PLACEHOLDER.fullmatch(password):
                        continue
                findings.append((number, category))
        for match in ASSIGNMENT.finditer(line):
            value = match.group(1)
            if (value != 'capstone-synthetic-only-not-production'
                    and not PLACEHOLDER.fullmatch(value) and entropy(value) >= 3.5):
                findings.append((number, 'literal-secret-assignment'))
    return sorted(set(findings))


def scan_tree(root):
    findings, errors = [], []
    scanned = 0
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts) or path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or path.name.endswith(('-wal', '-shm')):
            continue
        try:
            data = path.read_bytes()
            if b'\x00' in data:
                continue
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
        except OSError:
            errors.append(str(relative))
            continue
        scanned += 1
        # Cookie exports and a populated dotenv are unsafe committed artifacts,
        # even when their field names evade credential assignment patterns.
        if path.name in {'m365_session.json', 'canva_session.json'} and text.strip():
            findings.append((str(relative), 1, 'login-session-file'))
        if path.name == '.env' or path.name.startswith('.env.') and path.name not in {'.env.example', '.env.sample', '.env.template'}:
            if re.search(r'(?m)^\s*[A-Za-z_]\w*\s*=\s*[^\s#]+', text):
                findings.append((str(relative), 1, 'populated-environment-file'))
        findings.extend((str(relative), line, category) for line, category in scan_text(text))
    return scanned, sorted(set(findings)), errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        parser.error('root must be an existing directory')
    scanned, findings, errors = scan_tree(root)
    for path, line, category in findings:
        print(f'{path}:{line}: {category} (value withheld)')
    for path in errors:
        print(f'{path}: unreadable file', file=sys.stderr)
    print(f'Secret-pattern scan: {scanned} text files, {len(findings)} findings, {len(errors)} read errors.')
    return 1 if findings or errors or not scanned else 0


if __name__ == '__main__':
    sys.exit(main())
