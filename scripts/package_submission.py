#!/usr/bin/env python3
"""Create the contest ZIP containing exactly Ссылка.txt and Паспорт.pdf."""
import argparse
from pathlib import Path
from urllib.parse import urlsplit
import zipfile

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]


def build(surname, repository_url, passport, output_dir):
    if (not surname or surname != surname.strip() or len(surname) > 100
            or any(c in surname for c in '/\\:*?"<>|\r\n\t')
            or surname in {'.', '..'} or surname.endswith('.')):
        raise ValueError('Use the surname as registered, without path separators or filename control characters')
    parsed = urlsplit(repository_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not parsed.path.endswith('/tree/main')
            or any(c.isspace() for c in repository_url)):
        raise ValueError('Provide a public HTTPS URL of the main branch, ending in /tree/main')
    data = passport.read_bytes()
    reader = PdfReader(passport)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 4 or len(data) > 15_000_000:
        raise ValueError('Passport must be an unencrypted PDF, at most 4 pages and 15 MB')
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / (surname + '.zip')
    link = (repository_url + '\n').encode('utf-8')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('Ссылка.txt', link)
        archive.writestr('Паспорт.pdf', data)
    with zipfile.ZipFile(target) as archive:
        if (archive.namelist() != ['Ссылка.txt', 'Паспорт.pdf'] or archive.testzip() is not None
                or archive.read('Ссылка.txt') != link or archive.read('Паспорт.pdf') != data):
            raise ValueError('Archive verification failed')
    if target.stat().st_size > 18_000_000:
        raise ValueError('Submission ZIP exceeds 18 MB')
    print(f'PASS {target}: 2 files; {len(reader.pages)} passport pages; {target.stat().st_size} bytes')
    print('Public clone access and Ubuntu runtime validation must be checked separately.')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--surname', required=True)
    parser.add_argument('--repository-url', default='https://github.com/cyberghost913/mtc-engineer-hack-devops/tree/main')
    parser.add_argument('--passport', type=Path, default=ROOT / 'docs/Паспорт.pdf')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'artifacts/submission')
    args = parser.parse_args()
    build(args.surname, args.repository_url, args.passport, args.output_dir)
