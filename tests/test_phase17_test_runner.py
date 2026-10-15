"""Exercise the real standard-library runner in miniature disposable repositories."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='runner-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        (self.repo / 'tools').mkdir(parents=True)
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/__init__.py').write_text('')
        shutil.copy2(ROOT / 'tools/run_tests.py', self.repo / 'tools/run_tests.py')

    def fixture(self, name, body):
        (self.repo / 'tests' / (name + '.py')).write_text(body, encoding='utf-8')

    def run_cli(self, *args, env=None):
        command = [sys.executable, str(self.repo / 'tools/run_tests.py'),
                   '--output-dir', str(self.base / 'results'), *args]
        result = subprocess.run(command, cwd=self.base, env=env, capture_output=True,
                                text=True, timeout=25)
        files = sorted((self.base / 'results').glob('*/summary.json'))
        summary = json.loads(files[-1].read_text()) if files else None
        return result, summary

    def test_pass_from_outside_cwd_and_isolation(self):
        (self.repo / '.env').write_text('SOME_SECRET=do-not-load\n')
        (self.repo / 'shopbot.db').write_text('never touch this')
        env = dict(os.environ, BOT_TOKEN='do-not-inherit', DATABASE_URL='do-not-use',
                   BINANCE_API_SECRET='do-not-inherit', ARBITRARY_CREDENTIAL='private')
        body = '''import os, pathlib, unittest, subprocess, sys
class T(unittest.TestCase):
 def test_safe(self):
  self.assertEqual(os.environ['DATABASE_URL'], '')
  self.assertEqual(os.environ['BOT_TOKEN'], '')
  self.assertNotIn('BINANCE_API_SECRET', os.environ)
  self.assertNotIn('ARBITRARY_CREDENTIAL', os.environ)
  nested = subprocess.check_output(['python', '-c', 'import sys;print(sys.prefix)'], text=True).strip()
  self.assertEqual(nested, sys.prefix)
  self.assertFalse(pathlib.Path('.env').exists())
  self.assertFalse(pathlib.Path('shopbot.db').exists())
  pathlib.Path('shopbot.db').write_text('temporary')
'''
        self.fixture('test_a', body)
        self.fixture('test_b', body)
        result, data = self.run_cli(env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(data['totals']['passed'], 2)
        self.assertEqual((self.repo / 'shopbot.db').read_text(), 'never touch this')
        self.assertIn('10 slowest suites', result.stdout)

    def test_filter_multiple_and_non_test_named_suite(self):
        body = 'import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n'
        self.fixture('helper_pick', body)
        self.fixture('test_pick', body)
        self.fixture('test_other', 'raise RuntimeError("must not run")')
        result, data = self.run_cli('--filter', 'pick')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual({r['suite'] for r in data['results']}, {'helper_pick', 'test_pick'})

    def test_node_dependencies_are_shared_without_inheriting_node_options(self):
        (self.repo / 'examples/node_modules').mkdir(parents=True)
        expected = str(self.repo / 'examples/node_modules')
        self.fixture('test_node_environment', f"import os,pathlib,unittest\nclass T(unittest.TestCase):\n def test_env(self):\n  self.assertEqual(os.environ['NODE_PATH'], {expected!r})\n  self.assertNotIn('NODE_OPTIONS', os.environ)\n  self.assertFalse(pathlib.Path('examples/node_modules').exists())\n")
        result, data = self.run_cli(env=dict(os.environ, NODE_PATH='untrusted', NODE_OPTIONS='untrusted'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(data['totals']['passed'], 1)

    def test_no_matches_is_nonzero(self):
        result, data = self.run_cli('--filter', 'missing')
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(data)
        self.assertIn('nothing executed', result.stderr)

    def test_fail_crash_exit_timeout_continue_and_traceback(self):
        self.fixture('test_a_fail', 'import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail("diagnostic-marker")\n')
        self.fixture('test_b_crash', 'import os\nos.abort()\n')
        self.fixture('test_c_exit', 'import sys\nprint("exit-marker", flush=True)\nsys.exit(7)\n')
        self.fixture('test_d_hang', 'import time\nprint("hang-marker", flush=True)\ntime.sleep(60)\n')
        self.fixture('test_z_pass', 'import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
        result, data = self.run_cli('--timeout', '1')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual([r['status'] for r in data['results']], ['FAIL', 'CRASH', 'EXIT_ERROR', 'TIMEOUT', 'PASS'])
        self.assertEqual(data['results'][2]['exit_code'], 7)
        self.assertTrue(data['complete'])
        self.assertIn('Traceback', result.stdout)
        self.assertIn('diagnostic-marker', result.stdout)
        self.assertIn('hang-marker', result.stdout)
        self.assertTrue(all(Path(r['log']).exists() for r in data['results']))

    def test_empty_suite_fails(self):
        self.fixture('test_empty', '')
        result, data = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data['results'][0]['status'], 'EMPTY')

    def test_skips_are_not_reported_as_passes(self):
        self.fixture('test_skip', 'import unittest\n@unittest.skip("external unavailable")\nclass T(unittest.TestCase):\n def test_skip(self): pass\n')
        result, data = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(data['results'][0]['status'], 'SKIPPED')
        self.assertEqual(data['totals']['passed'], 0)
        self.assertEqual(data['totals']['skipped'], 1)

    def test_partial_skip_and_expected_failure_counts(self):
        self.fixture('test_notes', '''import unittest
class T(unittest.TestCase):
 def test_ok(self): self.assertTrue(True)
 @unittest.skip('not available')
 def test_skip(self): pass
 @unittest.expectedFailure
 def test_xfail(self): self.fail('known')
''')
        result, data = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(data['results'][0]['status'], 'PASS_WITH_NOTES')
        self.assertEqual(data['totals']['passed'], 1)
        self.assertEqual(data['totals']['expected_failures'], 1)
        self.assertEqual(data['totals']['skipped'], 1)

    def test_unexpected_success_fails(self):
        self.fixture('test_xpass', 'import unittest\nclass T(unittest.TestCase):\n @unittest.expectedFailure\n def test_ok(self): self.assertTrue(True)\n')
        result, data = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data['totals']['unexpected_successes'], 1)

    def test_stdout_cannot_forge_counts(self):
        self.fixture('test_noise', 'import unittest\nprint("Ran 9999 tests in 0.01s\\nOK")\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
        result, data = self.run_cli()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(data['totals']['tests'], 1)

    def test_timeout_validation(self):
        for value in ('0', '-1', 'nan', 'inf'):
            with self.subTest(value=value):
                result, _ = self.run_cli('--timeout', value)
                self.assertEqual(result.returncode, 2)

    @unittest.skipUnless(os.name == 'posix', 'process-group probe is POSIX only')
    def test_timeout_kills_descendants(self):
        marker = self.base / 'descendant-marker'
        script = f'import time,pathlib;time.sleep(2);pathlib.Path({str(marker)!r}).write_text("alive")'
        self.fixture('test_hang', f'import subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c",{script!r}])\ntime.sleep(60)\n')
        result, data = self.run_cli('--timeout', '0.5')
        self.assertEqual(data['results'][0]['status'], 'TIMEOUT')
        import time
        time.sleep(2.1)
        self.assertFalse(marker.exists())
