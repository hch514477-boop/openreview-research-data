"""Normalize saved API objects and generate an evidence-based availability report."""
import json
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from src.common import TIMES, invitations, kind, load, now, value


def records() -> tuple[list, list, list]:
    papers, notes, edits = {}, {}, {}
    supplement_file = config.ROOT / 'data/edits/state.json'
    supplements = load(supplement_file).get('targets', {}) if supplement_file.exists() else {}
    for path in sorted(config.RAW.glob('ICLR_*/*/metadata.json')):
        directory = path.parent
        meta = load(path)
        paper = load(directory / 'submission.json')
        base = {'source': meta['source'], 'year': meta['year'], 'paper_id': paper['id'],
                'forum_id': paper.get('forum') or paper['id']}
        raw_path = directory.relative_to(config.ROOT).as_posix()
        targets = [target for target in supplements.values()
                   if target['paper_id'] == paper['id'] and target['year'] == meta['year']]
        if targets:
            meta['edits_supplement'] = {target['note_id']: target for target in targets}
        papers[paper['id']] = {**base, 'id': paper['id'], 'title': value(paper, 'title'),
                              'abstract': value(paper, 'abstract'), 'keywords': value(paper, 'keywords'),
                              'venue': value(paper, 'venue'), 'venueid': value(paper, 'venueid'),
                              'authors': value(paper, 'authors'),
                              'timestamps': {k: paper.get(k) for k in TIMES},
                              'raw_path': raw_path,
                              'pdf_path': raw_path + '/paper.pdf' if meta['pdf']['status'] == 'ok' else None,
                              'collection_status': meta}
        # Ignore stale files from earlier successful requests if the latest failed.
        public_notes = load(directory / 'notes.json') if meta['notes']['status'] == 'ok' else []
        for note in public_notes:
            notes[note['id']] = {**base, 'id': note['id'], 'forum_id': note.get('forum'),
                                 'replyto': note.get('replyto'), 'invitations': invitations(note),
                                 'type': kind(note), 'content': note.get('content'),
                                 'signatures': note.get('signatures'),
                                 'timestamps': {k: note.get(k) for k in TIMES},
                                 'raw_path': raw_path + '/notes.json'}
        sources = [(raw_path + '/edits.json', load(directory / 'edits.json'))]
        for target in targets:
            if target.get('raw_path') and target['status'] in ('success', 'empty'):
                sources.append((target['raw_path'], load(config.ROOT / target['raw_path'])))
        for origin, batch in sources:
            for edit in batch:
                edits[edit['id']] = {**base, 'id': edit['id'], 'note_id': edit.get('note', {}).get('id'),
                                     'invitations': invitations(edit), 'content': edit.get('note', {}).get('content'),
                                     'timestamps': {k: edit.get(k) for k in TIMES},
                                     'raw_path': origin, 'raw_edit': edit}
    return list(papers.values()), list(notes.values()), list(edits.values())


def write_report(papers: list, notes: list, edits: list) -> None:
    lines = ['# OpenReview 采集可行性报告', '', f'生成时间（UTC）：{now()}', '',
             '统计依据为本地原始文件和实际运行记录；0 不代表数据不存在。', '',
             '| 检查项目 | ICLR 2024 | ICLR 2025 |', '|---|---:|---:|']
    metrics = {}
    for year in config.YEARS:
        ps = [p for p in papers if p['year'] == year]
        ns = [n for n in notes if n['year'] == year]
        runpath = config.RAW / f'ICLR_{year}' / 'run.json'
        run = load(runpath) if runpath.exists() else {}
        metrics[year] = [len(ps), sum(p['pdf_path'] is not None for p in ps),
                         *[len({n['paper_id'] for n in ns if n['type'] == t}) for t in
                           ('official_review', 'author_response', 'meta_review', 'decision')],
                         len({e['paper_id'] for e in edits if e['year'] == year}),
                         sum(p['collection_status']['notes']['status'] != 'ok' for p in ps)]
        if run.get('status') == 'failed':
            # Discovery failure has no known paper IDs: do not invent a failure count.
            metrics[year][-1] = '未确定（列表请求失败）'
    labels = ['成功保存投稿数', '获取 PDF 的论文数', '有 Official Review 的论文数',
              '有 Author Response 的论文数', '有 Meta Review 的论文数', '有 Decision 的论文数',
              '可获取修改历史的论文数', '采集失败的论文数（投稿或讨论）']
    for i, label in enumerate(labels):
        lines.append(f'| {label} | {metrics[2024][i]} | {metrics[2025][i]} |')
    lines.extend(['', '## 运行与缺失情况', ''])
    for year in config.YEARS:
        directory = config.RAW / f'ICLR_{year}'
        run = load(directory / 'run.json') if (directory / 'run.json').exists() else {}
        lines.append(f'### ICLR {year}')
        lines.extend(['', f"最近运行状态：{run.get('status', 'not_run')}；请求目标：{run.get('requested', '未执行')}。", ''])
        history = directory / 'runs.jsonl'
        if history.exists():
            for line in history.read_text(encoding='utf-8').splitlines():
                attempt = json.loads(line)
                lines.append(f"- {attempt['started_at']}：{attempt['status']}；" + '; '.join(attempt['errors']))
        ps = [p for p in papers if p['year'] == year]
        for category in ('official_review', 'author_response', 'meta_review', 'decision'):
            present = {n['paper_id'] for n in notes if n['year'] == year and n['type'] == category}
            lines.append(f'- {category}：已保存投稿中 {len(ps) - len(present)} 篇未观察到；需结合 notes 状态区分请求失败与返回为空。')
        statuses = Counter(s['status'] for p in ps for s in p['collection_status']['edits'].values())
        lines.extend([f'- Edits 按 Note 的状态：{dict(statuses)}', ''])
    lines.extend(['## 数据解释和限制', '',
                  '- 投稿列表被 ChallengeRequiredError 阻断不代表数据不公开；其他官方接口保存的局部数据，以 metadata 中的获取状态和完整性限制为准。',
                  '- no_records_found 仅表示公开接口返回空列表；access_restricted 表示 401/403；404 为 unsupported_or_unavailable；其他错误为 request_failed。无法仅凭这些状态判定“未公开”。',
                  '- Edits 包括创建和修改操作，获取一条 Edit 不保证存在多个论文版本或完整修改历史；仅下载当前 PDF，不下载历史 PDF。',
                  '- 作者回复依 Invitation 或 /Authors 签名识别，属于结构标签，不分析内容；未知记录保留为 other。',
                  '- 采用有限候选池和公开 replyCount 优先级，轮换原始 venue 标签；样本不具代表性，也不保证结果类别齐全。',
                  '- Reject、Withdraw 等结果保留原始 venue/venueid 和 Decision content，不进行合并或推断。', '',
                  '## 字段差异与关联', ''])
    fields = {}
    for year in config.YEARS:
        paths = list((config.RAW / f'ICLR_{year}').glob('*/submission.json'))
        fields[year] = set().union(*(set(load(p).get('content', {})) for p in paths)) if paths else set()
        lines.append(f'- {year} 投稿 content 字段：{sorted(fields[year]) if paths else "暂无论文，不能判断字段差异"}。')
    if all(fields.values()):
        lines.append(f'- 仅 2024 出现：{sorted(fields[2024] - fields[2025])}；仅 2025 出现：{sorted(fields[2025] - fields[2024])}。')
    lines.extend(['', '关联使用原始 paper_id、forum_id、replyto、note_id 与 raw_path。',
                  '执行 `python src/validate.py` 会生成 reports/validation.json；只有其中 passed=true 且论文数非零才说明已验证真实数据关联。',
                  '扩大采集前：先解决公开 API 访问问题，重跑各 2 篇并校验，再执行各 20 篇；抽查回复标签和历史记录完整性。', ''])
    config.REPORT.parent.mkdir(parents=True, exist_ok=True)
    config.REPORT.write_text('\n'.join(lines), encoding='utf-8')


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    tables = records()
    config.PROCESSED.mkdir(parents=True, exist_ok=True)
    for name, rows in zip(('papers', 'notes', 'edits'), tables):
        target = config.PROCESSED / f'{name}.jsonl'
        temporary = target.with_suffix('.jsonl.tmp')
        temporary.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
        temporary.replace(target)
        logging.info('%s: %s', name, len(rows))
    write_report(*tables)


if __name__ == '__main__':
    main()
