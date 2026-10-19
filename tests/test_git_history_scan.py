"""Read-only Git-history scanner: historical sessions, clean history, scan errors."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tools import scan_git_history


class HistoryScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Synthetic Test')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'synthetic fixture')

    def scan(self):
        output = io.StringIO()
        report = self.root / 'report.json'
        with patch.object(scan_git_history, 'ROOT', self.root), contextlib.redirect_stdout(output):
            result = scan_git_history.main(['--output', str(report)])
        return result, output.getvalue(), json.loads(report.read_text())

    def test_deleted_session_is_detected_without_disclosing_values(self):
        marker = 'synthetic-cookie-must-not-be-printed'
        session = self.root / 'm365_session.json'
        session.write_text(json.dumps({'cookies': [{'value': marker}]}))
        self.commit()
        session.unlink()
        self.commit()
        result, output, report = self.scan()
        self.assertEqual(result, 1)
        self.assertIn('sensitive-file-history', output)
        self.assertEqual(report['findings'][0]['path'], 'm365_session.json')
        self.assertNotIn(marker, output + json.dumps(report))
        self.assertFalse(session.exists())

    def test_clean_history(self):
        (self.root / 'README.md').write_text('Only documentation.\n')
        self.commit()
        result, output, report = self.scan()
        self.assertEqual(result, 0)
        self.assertEqual(report['text_blobs'], 1)
        self.assertEqual(report['findings'], [])

    def test_unreadable_history_is_error_not_clean(self):
        with patch.object(scan_git_history, 'ROOT', self.root / 'missing'), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scan_git_history.main([]), 2)


if __name__ == '__main__':
    unittest.main()
