"""Checks on public course structure, provenance and offline packaging."""
import io
import json
from pathlib import Path
import re
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from build import load_data, render


class CourseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_data()
        cls.files = render()

    def test_chapter_navigation_and_references(self):
        lessons = self.data['lessons']
        self.assertEqual([l['n'] for l in lessons], list(range(1, 21)))
        self.assertEqual([l['id'] for l in lessons], [str(i) for i in range(1, 21)])
        refs = {s['id'] for s in self.data['sources']}
        for lesson in lessons:
            with self.subTest(chapter=lesson['n']):
                self.assertTrue(set(lesson['refs']) <= refs)
                if lesson.get('image'):
                    self.assertIn(lesson['image'], self.data['images'])
                if lesson.get('diagram'):
                    self.assertIn(lesson['diagram'], self.data['diagrams'])

    def test_every_figure_has_author_and_license(self):
        for asset in self.data['images'].values():
            self.assertTrue(asset['author'])
            self.assertEqual(asset['license'], 'CC-BY-4.0')
            self.assertTrue(asset['src'].startswith('data:image/'))
            self.assertTrue(asset['caption'])
            self.assertTrue(asset['explain'])

    def test_site_embeds_data_without_external_runtime_dependencies(self):
        page = self.files['index.html'].decode('utf-8')
        self.assertNotRegex(page, r'<script[^>]+src=')
        self.assertNotRegex(page, r'<link[^>]+rel=["\']stylesheet')
        self.assertNotIn('__DATA__', page)
        embedded = re.search(r'<script id="course-data" type="application/json">(.*?)</script>', page, re.S)
        self.assertIsNotNone(embedded)
        self.assertEqual(len(json.loads(embedded.group(1))['lessons']), 20)
        self.assertNotIn('<', embedded.group(1))

    def test_generated_files_are_reproducible(self):
        self.assertEqual(self.files, render())

    def test_download_contains_code_and_licenses_only(self):
        with zipfile.ZipFile(io.BytesIO(self.files['ddd-code-examples.zip'])) as archive:
            names = archive.namelist()
            self.assertIn('ddd-code-examples/python/lifecycle.py', names)
            self.assertIn('ddd-code-examples/go/booking_test.go', names)
            self.assertIn('ddd-code-examples/LICENSE', names)
            self.assertEqual(archive.testzip(), None)
            self.assertFalse(any('..' in name or '__pycache__' in name for name in names))
            self.assertFalse(any(name.endswith(('.pdf', '.db', '.pyc')) for name in names))

    def test_no_private_host_paths_or_conversation_in_generated_site(self):
        page = self.files['index.html'].decode('utf-8')
        for phrase in ('/' + 'Users/', 'file:' + '///', '你提供的', '你指定的', '这次修改解决了什么',
                       '这里究竟讲了多少', '原书内容取舍', '上一版', 'gho_'):
            self.assertNotIn(phrase, page)

    def test_read_only_learning_ui(self):
        source = (ROOT / 'web/app.js').read_text(encoding='utf-8')
        self.assertNotIn('function coveragePage(', source)
        self.assertNotRegex(source, r'data-action=["\'](?:rate|timer)["\']')
        self.assertIn('function readButton(', source)
        self.assertEqual(len(self.data['interviews']), 8)

    def test_no_book_pdfs_or_screenshots_in_assets(self):
        assets = ROOT / 'web/assets'
        self.assertFalse(list(assets.glob('*.pdf')))
        self.assertFalse(list(assets.glob('evans-*.png')))
        self.assertEqual(len(list(assets.glob('*.svg'))), 6)


if __name__ == '__main__':
    unittest.main()
