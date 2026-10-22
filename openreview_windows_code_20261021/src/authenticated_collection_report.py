"""Evidence-based report for the fixed two-year, four-paper authentication run."""
from collections import Counter

import config
from src.common import kind, load, now, save


def write_report(runs: list, validation: dict | None = None, tests: dict | None = None,
                 authentication_error: str | None = None) -> None:
    evidence = {'created_at': now(), 'runs': runs, 'validation': validation, 'tests': tests,
                'authentication_error': authentication_error}
    directory = config.ROOT / 'data/authenticated_collection'
    save(directory / ('run_' + now().replace(':', '-').replace('+', '_') + '.json'), evidence)
    save(directory / 'latest.json', evidence)
    lines = ['# 认证采集 MVP：ICLR 2024—2025', '', f'生成时间（UTC）：{evidence["created_at"]}', '',
             '范围固定为每年 2 篇。本报告只统计本次选中的论文；既有单篇测试不会混入本次目标数。', '']
    if authentication_error:
        lines.extend([f'认证未完成：{authentication_error}。',
                      '两个年份的认证列表和采集均未执行；不得把此前单篇成功写为本次 4 篇成功。', ''])
    statistics = {}
    for year in config.YEARS:
        run = next((r for r in runs if r['year'] == year), None)
        stats = Counter()
        if run:
            for item in run['papers']:
                meta = item['metadata']
                stats['papers'] += 1
                stats['pdf'] += meta['pdf']['status'] == 'ok'
                raw = config.RAW / f'ICLR_{year}' / item['id']
                if meta['notes']['status'] == 'ok':
                    stats.update(kind(n) for n in load(raw / 'notes.json'))
                for status in meta['edits'].values():
                    if status.get('reason', '').startswith('MVP scope:'):
                        stats['edits_outside_scope'] += 1
                    else:
                        stats['edits_' + status['status']] += 1
                    stats['edit_records'] += status.get('count', 0)
        statistics[year] = stats
    lines.extend(['| 检查项 | ICLR 2024 | ICLR 2025 |', '|---|---:|---:|'])
    for label, key in [('成功保存投稿数', 'papers'), ('有效 PDF 数', 'pdf'), ('Official Review 条数', 'official_review'),
                       ('Author Response 条数', 'author_response'), ('Meta Review 条数', 'meta_review'),
                       ('Decision 条数', 'decision'), ('成功获取的 Edit 条数', 'edit_records'),
                       ('Edits 有记录的目标 Note 数', 'edits_ok'), ('Edits 成功但空的目标 Note 数', 'edits_no_records_found'),
                       ('Edits 受限的目标 Note 数', 'edits_access_restricted'),
                       ('Edits 网络/请求失败的目标 Note 数', 'edits_request_failed'),
                       ('Edits 限速的目标 Note 数', 'edits_rate_limited'),
                       ('Edits 因端点受限/限速而未测试的目标 Note 数', 'edits_not_tested'),
                       ('本次不查询 Edits 的其他讨论 Note 数', 'edits_outside_scope')]:
        lines.append(f'| {label} | {statistics[2024][key]} | {statistics[2025][key]} |')
    lines.extend(['', '数字 0 为本次保存/观察数量；未运行、受限或空结果均不能证明不存在公开数据。',
                  'Edits 有记录目标数按接口原始返回计；保存条数仅含 readers 明确包含 everyone 的 Edit，未确认公开的记录不保存。', ''])
    for run in runs:
        year = run['year']
        lines.extend([f'## ICLR {year}', '', f'运行状态：{run["status"]}；认证：{run["authenticated"]}。',
                      f'样本来源：{run["discovery_source"]}；覆盖：{run["coverage"]}。',
                      f'发现的投稿 ID：{run.get("discovery_ids", [])}。', ''])
        probes = [r for r in run['requests'] if r['endpoint'] == '/notes' and 'invitation' in r['params']]
        lines.append(f'- 投稿列表实际请求：{probes}。')
        pagination = len(probes) == 2 and all(p.get('http_status') == 200 for p in probes) and len(set(run.get('discovery_ids', []))) == 2
        lines.append(f'- 两页各 1 篇、两个不同 ID 的分页验证：{"通过" if pagination else "未通过或未完成"}。')
        for error in run['errors']:
            lines.append(f'- 运行错误：{error}')
        for item in run['papers']:
            meta = item['metadata']
            lines.append(f'- {item["id"]}：Notes={meta["notes"]["status"]}，PDF={meta["pdf"]["status"]}；Edits 状态={dict(Counter(s["status"] for s in meta["edits"].values()))}。')
            for label, status in [('Notes', meta['notes']), ('PDF', meta['pdf']), *meta['edits'].items()]:
                if status.get('reason') and not status['reason'].startswith('MVP scope:'):
                    lines.append(f'  - {label}：{status["status"]}；{status["reason"]}。')
        failures = [r for r in run['requests'] if r.get('http_status') is None or r['http_status'] >= 400]
        lines.append(f'- 请求失败记录（不含凭据）：{failures}。')
        lines.append('')
    lines.extend(['## 标准化与校验', '',
                  f'现有校验：{validation if validation is not None else "not_tested"}。',
                  f'本次采集后的 pytest：{tests if tests is not None else "not_tested"}。', '',
                  '统一输出 papers.jsonl / notes.jsonl / edits.jsonl，包含既有数据，因此总数可能大于本次 4 篇。',
                  '重建两次 JSONL 比较字节，验证标准化重复运行不会增加记录；真正的网络重采集未为验证去重而额外执行。',
                  '原始论文目录再次采集前复制至其 .snapshots/ 下；原始 ID、Forum、ReplyTo、Invitation、时间戳保持 API 返回值。', '',
                  '## 完整性与后续', '',
                  '只保存认证客户端返回且 readers 含 everyone 的公开 Notes；账号可见的非公开 Notes 不写入公开数据集。',
                  '空 Edits 只表示此次返回空；取到 Edit 也不代表完整修改历史。当前 PDF 不能代替历史 PDF。',
                  '没有返回 Author Response/Decision 等不代表接口失败或该类数据必然存在。',
                  '本次未使用关键词搜索发现样本；列表受限则记录失败并停止该年的采集，没有用局部搜索冒充完整投稿列表。',
                  '只有真实 2+2 采集、两年分页、PDF、关联校验、Schema 与测试全部通过后，才考虑人工确认扩大至每年 20 篇；本脚本固定为 2+2。', ''])
    # Compare actual normalized keys for the selected records in each year.
    import json
    for table in ('papers', 'notes', 'edits'):
        path = config.PROCESSED / f'{table}.jsonl'
        keys = {}
        if path.exists():
            rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            for year in config.YEARS:
                ids = {p['id'] for r in runs if r['year'] == year for p in r['papers']}
                keys[year] = {tuple(sorted(row)) for row in rows if row['paper_id'] in ids}
        same = ('通过' if keys[2024] == keys[2025] else '不一致') if keys.get(2024) and keys.get(2025) else '无双年记录，未验证'
        lines.append(f'- {table} 两年顶层 Schema：{same}。')
    target = config.REPORT.parent / 'authenticated_collection_mvp.md'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
