import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from depledger import store  # noqa: E402


class LedgerTestCase(unittest.TestCase):
    """A fresh database file per test, so tests never touch data/depledger.db."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'test.db')
        self.old_env = os.environ.get('DEPLEDGER_DB')
        os.environ['DEPLEDGER_DB'] = self.db
        self.conn = store.connect()

    def tearDown(self):
        self.conn.close()
        if self.old_env is None:
            os.environ.pop('DEPLEDGER_DB', None)
        else:
            os.environ['DEPLEDGER_DB'] = self.old_env
        self.tmp.cleanup()

    def make_git_repo(self, remote):
        path = os.path.join(self.tmp.name, 'checkout-' + remote.replace('/', '-').replace(':', '-'))
        os.makedirs(path)
        subprocess.run(['git', 'init', '-q'], cwd=path, check=True)
        subprocess.run(['git', 'remote', 'add', 'origin', remote], cwd=path, check=True)
        return path
