#!/usr/bin/env python3
"""Run tests/*.py in fresh interpreters and disposable, secret-free source copies.

No application imports occur in the parent. The private child mode uses unittest's
normal module loader/runner, with a separate JSON result channel (never stdout
parsing). Exit codes: 0 successful assertions (skips disclosed), 1 failed run,
2 invalid arguments/no matching suites/all skipped, 130 interrupted.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {'.git', '.venv', 'venv', 'env', 'node_modules', '__pycache__',
                '.test-results', 'logs', 'backups', 'restore_uploads', '.pytest_cache'}
ENV_KEYS = {'PATH', 'PYTHONPATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT',
            'LANG', 'LC_ALL', 'LC_CTYPE', 'TMP', 'TEMP', 'TMPDIR'}


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def source_ignore(directory, names):
    """Never copy operators' credentials, sessions, databases or generated logs."""
    ignored = []
    for name in names:
        lower = name.lower()
        if (name in EXCLUDE_DIRS or lower == '.env' or lower.startswith('.env.')
                or lower.endswith(('.env', '.db', '.db-wal', '.db-shm', '.sqlite',
                                   '.sqlite3', '.pyc', '.pyo', '.zip', '.log', '.dump'))
                or lower in {'m365_session.json', 'canva_session.json'}):
            ignored.append(name)
    return ignored


def test_environment():
    env = {key: value for key, value in os.environ.items() if key in ENV_KEYS}
    # Legacy integration probes launch literal 'python'; keep them in this
    # interpreter's environment even when the caller did not activate its venv.
    env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', os.defpath)
    # Share installed Node dependencies like Python's external site-packages;
    # disposable copies deliberately omit node_modules. Never inherit NODE_OPTIONS.
    node_deps = ROOT / 'examples' / 'node_modules'
    if node_deps.is_dir():
        env['NODE_PATH'] = str(node_deps)
    env.update(DATABASE_URL='', BOT_TOKEN='', ADMIN_ID='0',
               DATA_ENCRYPTION_KEY='test-only-runner-key-not-production',
               PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1',
               PYTHON_DOTENV_DISABLED='1')
    return env


def child_main(module, result_path):
    # The copied root, not the caller's checkout, wins over installed 'tests'.
    sys.path.insert(0, str(ROOT))
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    counts = dict(tests=result.testsRun, failures=len(result.failures),
                  errors=len(result.errors), skipped=len(result.skipped),
                  expected_failures=len(result.expectedFailures),
                  unexpected_successes=len(result.unexpectedSuccesses))
    counts['passed'] = counts['tests'] - sum(counts[k] for k in (
        'failures', 'errors', 'skipped', 'expected_failures', 'unexpected_successes'))
    write_json(result_path, counts)
    return 0 if result.wasSuccessful() else 1


def tail(path, limit=8000):
    with Path(path).open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - limit))
        return stream.read().decode('utf-8', errors='replace')


def stop_process_group(process):
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    process.wait()


def run_suite(root, path, output, timeout):
    started = time.monotonic()
    log = output / (path.stem + '.log')
    result_file = output / (path.stem + '.result.json')
    row = dict(suite=path.stem, status='NOT_RUN', exit_code=None, seconds=0,
               tests=0, passed=0, failures=0, errors=0, skipped=0,
               expected_failures=0, unexpected_successes=0, log=str(log))
    try:
        with tempfile.TemporaryDirectory(prefix='isolated-suite-') as directory:
            copy = Path(directory) / 'repo'
            shutil.copytree(root, copy, ignore=source_ignore)
            command = [sys.executable, str(copy / 'tools/run_tests.py'),
                       '--_suite', 'tests.' + path.stem, '--_result-file', str(result_file)]
            with log.open('wb') as stream:
                process = subprocess.Popen(command, cwd=copy, env=test_environment(),
                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name == 'posix')
                try:
                    row['exit_code'] = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    stop_process_group(process)
                    row['exit_code'] = process.returncode
                    row['status'] = 'TIMEOUT'
                except KeyboardInterrupt:
                    stop_process_group(process)
                    raise
            if row['status'] != 'TIMEOUT':
                code = row['exit_code']
                if code < 0 or (os.name == 'nt' and code > 0x7fffffff):
                    row['status'] = 'CRASH'
                elif not result_file.exists():
                    row['status'] = 'EXIT_ERROR' if code else 'MISSING_RESULT'
                else:
                    counts = json.loads(result_file.read_text(encoding='utf-8'))
                    required = ('tests', 'passed', 'failures', 'errors', 'skipped',
                                'expected_failures', 'unexpected_successes')
                    if any(type(counts.get(k)) is not int or counts[k] < 0 for k in required):
                        raise ValueError('invalid child result counts')
                    if counts['tests'] != sum(counts[k] for k in required if k != 'tests'):
                        raise ValueError('inconsistent child result counts')
                    row.update({k: counts[k] for k in required})
                    if code or any(counts[k] for k in ('failures', 'errors', 'unexpected_successes')):
                        row['status'] = 'FAIL'
                    elif not counts['tests']:
                        row['status'] = 'EMPTY'
                    elif counts['skipped'] == counts['tests']:
                        row['status'] = 'SKIPPED'
                    elif counts['skipped'] or counts['expected_failures']:
                        row['status'] = 'PASS_WITH_NOTES'
                    else:
                        row['status'] = 'PASS'
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        row['status'] = 'RUNNER_ERROR'
        # Infrastructure errors only; suite output is already in its full log.
        row['detail'] = type(exc).__name__
    row['seconds'] = round(time.monotonic() - started, 3)
    return row


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('timeout must be finite and greater than zero')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--filter', default='', help='case-sensitive filename substring')
    parser.add_argument('--timeout', type=positive_seconds, default=300.0, help='seconds per suite (default 300)')
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.test-results', help='artifact parent directory')
    parser.add_argument('--_suite', help=argparse.SUPPRESS)
    parser.add_argument('--_result-file', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._suite:
        if not args._result_file:
            parser.error('private child mode requires a result file')
        return child_main(args._suite, args._result_file)
    paths = sorted(p for p in (ROOT / 'tests').glob('*.py')
                   if p.name != '__init__.py' and args.filter in p.name)
    if not paths:
        print('NO MATCHING SUITES: nothing executed.', file=sys.stderr)
        return 2
    parent = args.output_dir.resolve()
    # A custom artifact folder inside the source could recursively copy itself.
    if parent != ROOT / '.test-results' and (parent == ROOT or ROOT in parent.parents):
        parser.error('custom output directory must be outside the repository')
    parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-'), dir=parent))
    rows = []
    interrupted = False
    print(f'{len(paths)} isolated suites; timeout {args.timeout:g}s each. Artifacts: {output}', flush=True)
    try:
        for path in paths:
            row = run_suite(ROOT, path, output, args.timeout)
            rows.append(row)
            write_json(output / 'summary.json', dict(complete=False, results=rows))
            print(f"{row['suite']}: {row['status']} ({row['tests']} cases, {row['seconds']:.2f}s, exit={row['exit_code']})", flush=True)
            if row['status'] not in {'PASS', 'PASS_WITH_NOTES'}:
                print(tail(row['log']) if Path(row['log']).exists() else row.get('detail', 'no child output'), flush=True)
    except KeyboardInterrupt:
        interrupted = True
        done = {row['suite'] for row in rows}
        rows.extend(dict(suite=p.stem, status='NOT_RUN', tests=0, passed=0, skipped=0,
                         seconds=0, exit_code=None) for p in paths if p.stem not in done)
    totals = {k: sum(row.get(k, 0) for row in rows) for k in (
        'tests', 'passed', 'failures', 'errors', 'skipped', 'expected_failures', 'unexpected_successes')}
    bad = any(row['status'] not in {'PASS', 'PASS_WITH_NOTES', 'SKIPPED'} for row in rows)
    code = 130 if interrupted else 1 if bad else 0 if totals['passed'] else 2
    summary = dict(complete=not interrupted, exit_code=code, totals=totals,
                   statuses=dict(Counter(row['status'] for row in rows)), results=rows)
    write_json(output / 'summary.json', summary)
    print('\nSUITE                                              OUTCOME             CASES   SKIP  SECONDS')
    for row in rows:
        print(f"{row['suite']:<50} {row['status']:<19} {row['tests']:>5} {row['skipped']:>6} {row['seconds']:>8.2f}")
    print('\n10 slowest suites:')
    for row in sorted(rows, key=lambda r: r['seconds'], reverse=True)[:10]:
        print(f"  {row['seconds']:8.2f}s  {row['suite']} ({row['status']})")
    print(f'\nTotals: {totals}; outcomes: {summary["statuses"]}; exit {code}')
    print(f'Full logs and structured counts: {output}')
    return code


if __name__ == '__main__':
    sys.exit(main())
