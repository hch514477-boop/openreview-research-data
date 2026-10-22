# ICLR 2013—2025 PDF 与补充材料补采

入口：`scripts/download_iclr_attachments.py`；实现：`src/iclr_attachments.py`。
只处理已保存的 Submission 和 Forum Notes，不枚举会议、不请求 /notes、/invitations、/notes/edits 或 /references。
不存在的年份目录记录为 missing_years，不自行补采该年份论文。2015/2016 不会用 Workshop 数据补齐。

## 笔记本 CMD / PowerShell

```powershell
cd E:\GitHub\openreview-research-data\openreview_windows_code_20261021
```

先只读检查（不会登录、联网或修改论文目录）：

```powershell
..\.venv311\Scripts\python.exe -u scripts/download_iclr_attachments.py --raw-root E:\GitHub\openreview-research-data\data\raw --years 2013-2025 --dry-run
```

先对最多 3 篇已有论文补采：

```powershell
..\.venv311\Scripts\python.exe -u scripts/download_iclr_attachments.py --raw-root E:\GitHub\openreview-research-data\data\raw --years 2024 --limit 3 --authenticate --max-requests 20 --request-delay 10 --min-free-gb 20
```

确认小批量结果后，补采所有已保存的年份：

```powershell
..\.venv311\Scripts\python.exe -u scripts/download_iclr_attachments.py --raw-root E:\GitHub\openreview-research-data\data\raw --years 2013-2025 --authenticate --max-requests 100000 --request-delay 10 --wait-on-429 --min-free-gb 20
```

Ctrl+C 停止后，用同一命令恢复。经过有效格式检查、来源引用一致且 SHA-256 匹配的文件跳过，其他缺失/失败文件在新一轮补采中重新尝试。
`--paper-id ID` 可限定单篇；以连字符开头的 ID 使用 `--paper-id=-ID`。
两个 API 的认证会分别提示输入；密码使用 getpass，不保存。不要并行启动多个补采进程，也不要同时对相同论文运行主采集器。

## 下载范围与版本

- `paper.pdf`：Submission 中明确保存的 OpenReview 文件路径。
- 投稿补充材料：例如 supplementary_material、appendix 等字段，只要实际指向 OpenReview 文件，就按原字段名和扩展名保存。
- 讨论附件：保存到 `attachments/<note_id>/<field>.<extension>`。
- 不读取 Edits、不下载历史附件、不访问代码仓库或数据集链接，不解压 ZIP，不执行下载内容。
- 复用现有官方 SDK 认证和受限 HTTP 传输。OpenReview 文件按已保存的 `/pdf/<id>` 或 `/attachment/<id>` 原始路径请求，避免按当前 Note 字段下载到后来替换的文件。官方 API v2 路径说明：https://docs.openreview.net/reference/api-v2/openapi-definition
- 早年 Submission 明确引用 arXiv 时，复用独立无 OpenReview 认证的 arXiv 下载器；无 vN 的链接只代表当前版本，不能声称投稿时版本已核实。
- PDF 按现有头部/EOF 规则检查，支持已实现的单 PDF MIME 包装；这不是全文解析或逐页渲染验证。ZIP 检查文件结构，不声称内部每个文件均已验证。

## 状态与限制

- 单线程，默认每次请求间隔 10 秒；网络错误/5xx 复用现有最多三次有退避的尝试，SDK 自动重试关闭；每次尝试计入预算。
- 429 尊重 Retry-After/resetTime，开启 --wait-on-429 后等待再恢复；缺少恢复时间时使用现有保守一小时冷却。403/认证失败停止，不绕过。
- 默认每个文件上限 512 MiB，可通过 --max-attachment-mb 调整。默认保留 20 GiB 空闲磁盘；达到空间阈值、大小上限或请求预算会记录状态并停止，不视为成功。
- 临时文件落盘后原子替换；替换已有无效文件前使用现有 .snapshots 备份。Submission、Notes、Invitations、Edits 不改写。
- 只更新 resources.json、metadata.json 的附件状态及现有完成标志，不改变 json_collection_complete。缺表单时 main_collection_complete 仍可能是 false，请看 attachments_supplement.status 判断本次文件补采结果。
- 无法识别的外部主 PDF 地址会标记 unavailable；没有补充资料字段表示没有发现引用，不代表独立核实论文从无补充资料。
- 报告：代码目录 reports/iclr_attachments_report.json 与 .md；阶段进度、脱敏请求日志：data/collection/iclr_attachments/。
- 报告含 dry_run 标记；每次运行更新当前报告。Ctrl+C 或中断前成功保存的文件不会丢失。

## Ubuntu 服务器

将上述入口和实现两个新文件同步到服务器对应的 scripts/、src/，保留服务器已有模块。
在项目目录、已激活 Python 3.11 环境中，将 python 前缀换为 `python`，将 --raw-root 换为服务器实际数据目录，例如：

```bash
python -u scripts/download_iclr_attachments.py --raw-root /home/ecs-user/openreview-research-data/data/raw --years 2013-2025 --authenticate --max-requests 100000 --request-delay 10 --wait-on-429 --min-free-gb 20
```

不要上传 Windows 虚拟环境，也不要把 Windows E 盘路径用于服务器。
