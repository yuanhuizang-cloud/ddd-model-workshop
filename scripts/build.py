#!/usr/bin/env python3
"""Build the offline site with only Python's standard library.

Generated files are reproducible; --check detects stale committed output.
"""
import argparse
import base64
import io
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load_data():
    data = {key: json.loads((ROOT / 'content' / (key + '.json')).read_text(encoding='utf-8'))
            for key in ('lessons', 'sources', 'interviews', 'diagrams')}
    assets = ROOT / 'web' / 'assets'
    images = json.loads((ROOT / 'content/images.json').read_text(encoding='utf-8'))
    for item in images.values():
        file = (assets / item['file']).resolve()
        if file.parent != assets.resolve() or not file.is_file():
            raise ValueError('Invalid asset path: ' + item['file'])
        item['src'] = 'data:' + item['mime'] + ';base64,' + base64.b64encode(file.read_bytes()).decode('ascii')
    data['images'] = images
    examples = ROOT / 'examples'
    data['files'] = {p.relative_to(examples).as_posix(): p.read_text(encoding='utf-8')
                     for p in sorted(examples.rglob('*'))
                     if p.is_file() and p.suffix in ('.py', '.go', '.mod', '.md')}
    return data


def render():
    data = load_data()
    template = (ROOT / 'web/template.html').read_text(encoding='utf-8')
    # Escape < so teaching snippets cannot terminate the JSON script element.
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    text = template.replace('__CSS__', (ROOT / 'web/style.css').read_text(encoding='utf-8'))
    text = text.replace('__JS__', (ROOT / 'web/app.js').read_text(encoding='utf-8')).replace('__DATA__', payload)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as output:
        for name, source in data['files'].items():
            info = zipfile.ZipInfo('ddd-code-examples/' + name, (2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            # ZIP_STORED avoids differences between zlib versions on build hosts.
            output.writestr(info, source.encode('utf-8'))
        for name in ('LICENSE', 'LICENSE-CONTENT', 'THIRD_PARTY_NOTICES.md'):
            info = zipfile.ZipInfo('ddd-code-examples/' + name, (2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            output.writestr(info, (ROOT / name).read_bytes())
    return {'index.html': text.encode('utf-8'), 'ddd-code-examples.zip': archive.getvalue(), '.nojekyll': b''}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='fail if docs/ differs from the source build')
    args = parser.parse_args()
    output = ROOT / 'docs'
    if not args.check:
        output.mkdir(exist_ok=True)
    changed = []
    for name, content in render().items():
        path = output / name
        if args.check:
            if not path.exists() or path.read_bytes() != content:
                changed.append(name)
        else:
            path.write_bytes(content)
    if changed:
        parser.exit(1, 'Generated files are stale: ' + ', '.join(changed) + '\nRun: python3 scripts/build.py\n')
    print('Site build is current.' if args.check else 'Built docs/index.html and code archive.')


if __name__ == '__main__':
    main()
