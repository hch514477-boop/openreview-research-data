"""Check normalized IDs, raw provenance, links and PDF headers offline."""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from src.common import load, now, save


def validate() -> dict:
    errors, warnings = [], []
    tables = {}
    for name in ('papers', 'notes', 'edits'):
        path = config.PROCESSED / f'{name}.jsonl'
        if not path.exists():
            errors.append(f'Missing {path}')
            tables[name] = []
            continue
        rows = []
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            try:
                rows.append(json.loads(line))
            except ValueError:
                errors.append(f'{name}:{number}: invalid JSON')
        tables[name] = rows
        ids = [r.get('id') for r in rows]
        if None in ids or len(ids) != len(set(ids)):
            errors.append(f'{name}: missing or duplicate IDs')
    papers = {p['id']: p for p in tables['papers'] if p.get('id')}
    note_ids = set(papers) | {n.get('id') for n in tables['notes']}
    for name, rows in tables.items():
        for row in rows:
            paper = papers.get(row.get('paper_id'))
            if not paper or row.get('forum_id') != paper.get('forum_id') or row.get('year') != paper.get('year'):
                errors.append(f'{name} {row.get("id")}: invalid paper/forum/year link')
            raw = config.ROOT / row.get('raw_path', '__missing__')
            if name == 'papers':
                raw = raw / 'submission.json'
            if not raw.is_file():
                errors.append(f'Missing raw file: {raw}')
            else:
                original = load(raw)
                originals = [original] if isinstance(original, dict) else original
                if row.get('id') not in {o.get('id') for o in originals}:
                    errors.append(f'{name} {row.get("id")}: absent from raw file')
            if name == 'papers' and row.get('pdf_path'):
                pdf = config.ROOT / row['pdf_path']
                if not pdf.is_file() or not pdf.read_bytes().startswith(b'%PDF-'):
                    errors.append(f'Invalid PDF: {pdf}')
            if name == 'notes' and row.get('replyto') and row['replyto'] not in note_ids:
                warnings.append(f'Public reply parent unavailable: {row["id"]} -> {row["replyto"]}')
            if name == 'edits' and row.get('note_id') not in note_ids:
                errors.append(f'Unknown edit target: {row.get("id")}')
    if not papers:
        warnings.append('No real papers available; empty output is structurally valid but collection feasibility is unverified.')
    result = {'checked_at': now(), 'passed': not errors, 'counts': {k: len(v) for k, v in tables.items()},
              'errors': errors, 'warnings': warnings}
    save(config.REPORT.parent / 'validation.json', result)
    return result


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    result = validate()
    logging.info('%s', result)
    raise SystemExit(0 if result['passed'] else 1)
