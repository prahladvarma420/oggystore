"""The scanner covers fixtures/docs and never echoes suspected credentials."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('secret_scan', ROOT / 'tools/scan_secrets.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class SecretScanTests(unittest.TestCase):
    def test_provider_patterns_and_private_keys(self):
        values = ['123456789:' + 'AbCdEf0123_-' * 2 + 'AbCdEf0123_',
                  'ghp_' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8',
                  'AK' + 'IA' + 'ABCDEFGHIJKLMNOP',
                  '-----BEGIN ' + 'PRIVATE KEY-----']
        for value in values:
            with self.subTest(length=len(value)):
                self.assertTrue(scanner.scan_text(value))

    def test_generic_secret_and_credential_url(self):
        value = 'a7B9cD3eF5gH1jK2mN4pQ6rS8tU0vW'
        self.assertTrue(scanner.scan_text('supplier_api_key = ' + repr(value)))
        self.assertTrue(scanner.scan_text('postgresql://operator:' + value + '@db.example/data'))
        self.assertFalse(scanner.scan_text('postgresql://user:pass@host/database'))
        self.assertFalse(scanner.scan_text('postgresql://postgres:...@host/database'))

    def test_documented_placeholders_and_synthetic_values(self):
        for value in ('<your-secret>', 'test-only-key-not-production', 'example-api-key', 'capstone-synthetic-only-not-production', 'x' * 40):
            self.assertFalse(scanner.scan_text('api_secret = ' + repr(value)))
        self.assertFalse(scanner.scan_text('BOT_TOKEN = os.getenv("BOT_TOKEN")'))

    def test_real_token_containing_test_is_not_exempt(self):
        value = 'ghp_' + 'test' + 'A1b2C3d4' * 4
        self.assertTrue(scanner.scan_text('token = ' + repr(value)))

    def test_cli_scans_tests_docs_and_redacts_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'docs').mkdir()
            value = 'ghp_' + 'A1b2C3d4' * 4 + 'Q7r8'
            (root / 'tests/fixture.py').write_text('TOKEN=' + repr(value))
            (root / 'docs/example.md').write_text(value)
            result = subprocess.run([sys.executable, str(ROOT / 'tools/scan_secrets.py'), '--root', directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('tests/fixture.py', result.stdout)
            self.assertIn('docs/example.md', result.stdout)
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_runtime_artifacts_excluded_but_source_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.test-results').mkdir()
            (root / '.test-results/log.txt').write_text('-----BEGIN ' + 'PRIVATE KEY-----')
            (root / 'safe.py').write_text('print("hello")')
            scanned, findings, errors = scanner.scan_tree(root)
            self.assertEqual(scanned, 1)
            self.assertEqual(findings, [])
            self.assertEqual(errors, [])

    def test_sensitive_session_and_env_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'm365_session.json').write_text('{"cookies": []}')
            (root / '.env').write_text('BOT_TOKEN=populated')
            _, findings, _ = scanner.scan_tree(root)
            self.assertEqual({r[2] for r in findings}, {'login-session-file', 'populated-environment-file'})

    def test_empty_tree_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(scanner.main(['--root', directory]), 1)
