import tempfile
import unittest

import helpers  # noqa: F401

from depledger.parse import release_notes_for, release_notes

BODY = '''
<details><summary>getsentry/sentry-javascript (@&#8203;sentry/vite-plugin)</summary>sentry text</details>
<details><summary>vitejs/vite (vite)</summary>vite text</details>
<details><summary>typescript-eslint/typescript-eslint (@&#8203;typescript-eslint/parser)</summary>parser text</details>
<details><summary>typescript-eslint/typescript-eslint (typescript-eslint)</summary>umbrella text</details>
<details><summary>sindresorhus/globals</summary>globals text</details>
'''


class NotesForTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notes, _ = release_notes(BODY, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def text(self, package):
        with open(release_notes_for(package, self.notes)[2]) as f:
            return f.read()

    def test_each_package_gets_its_own_block_not_a_substring_match(self):
        self.assertEqual(self.text('vite'), 'vite text')
        self.assertEqual(self.text('@sentry/vite-plugin'), 'sentry text')
        self.assertEqual(self.text('@typescript-eslint/parser'), 'parser text')
        self.assertEqual(self.text('typescript-eslint'), 'umbrella text')
        self.assertEqual(self.text('globals'), 'globals text')

    def test_a_package_without_a_block_gets_none(self):
        self.assertIsNone(release_notes_for('@typescript-eslint/eslint-plugin', self.notes))
