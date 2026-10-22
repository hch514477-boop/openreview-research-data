"""Small shared helpers; raw API objects are never rewritten by normalization."""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

TIMES = ('cdate', 'mdate', 'tcdate', 'tmdate', 'pdate', 'odate', 'ddate')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def value(note: dict, key: str):
    item = note.get('content', {}).get(key)
    return item.get('value') if isinstance(item, dict) and 'value' in item else item


def invitations(note: dict):
    return note.get('invitations') or ([note['invitation']] if note.get('invitation') else None)


def kind(note: dict) -> str:
    names = {(i.rsplit('/', 1)[-1]).lower() for i in invitations(note) or []}
    for token, label in [('withdrawal', 'withdrawal'), ('official_review', 'official_review'), ('meta_review', 'meta_review'),
                         ('decision', 'decision'), ('author_response', 'author_response'),
                         ('rebuttal', 'author_response')]:
        if token in names:
            return label
    if any(s.endswith('/Authors') for s in note.get('signatures', [])):
        return 'author_response'
    if 'official_comment' in names:
        return 'official_comment'
    return 'other'


def setup_logging(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(root / 'collection.log', encoding='utf-8')])
