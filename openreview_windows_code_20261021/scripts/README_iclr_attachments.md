# ICLR 2013—2025 PDF 与补充材料补采

入口：`scripts/download_iclr_attachments.py`；实现：`src/iclr_attachments.py`。
只处理已保存的 Submission 和 Forum Notes，不枚举会议、不请求 /notes、/invitations、/notes/edits 或 /references。

## CMD / PowerShell

```powershell
cd 本地路径\openreview_windows_code_20261021
```

先只读检查：

```powershell
..\.venv311\Scripts\python.exe -u scripts/download_iclr_attachments.py --raw-root 本地路径\data\raw --years 2013-2025 --dry-run
```

先对 3 篇已有论文补采：

```powershell
..\.venv311\Scripts\python.exe -u scripts/download_iclr_attachments.py --raw-root 本地路径\data\raw --years 2024 --limit 3 --authenticate --max-requests 20 --request-delay 10 --min-free-gb 20
```

确认小批量结果后，补采所有已保存的年份：

```powershell
..\.venv311\Scripts\python.exe -u scripts/download_iclr_attachments.py --raw-root 本地路径\data\raw --years 2013-2025 --authenticate --max-requests 100000 --request-delay 10 --wait-on-429 --min-free-gb 20
```

两个 API 的认证会分别提示输入；密码使用 getpass，不保存。

## 下载范围与版本

- `paper.pdf`：Submission 中明确保存的 OpenReview 文件路径。
- 投稿补充材料：例如 supplementary_material、appendix 等字段，只要实际指向 OpenReview 文件，就按原字段名和扩展名保存。
- 讨论附件：保存到 `attachments/<note_id>/<field>.<extension>`。
- 复用现有官方 SDK 认证和受限 HTTP 传输。OpenReview 文件按已保存的 `/pdf/<id>` 或 `/attachment/<id>` 原始路径请求，避免按当前 Note 字段下载到后来替换的文件。官方 API v2 路径说明：https://docs.openreview.net/reference/api-v2/openapi-definition

## 状态与限制

- 429 尊重 Retry-After/resetTime，开启 --wait-on-429 后等待再恢复；缺少恢复时间时使用现有保守一小时冷却。403/认证失败停止，不绕过。
- 默认每个文件上限 512 MiB，可通过 --max-attachment-mb 调整。默认保留 20 GiB 空闲磁盘；达到空间阈值、大小上限或请求预算会记录状态并停止，不视为成功。
- 临时文件落盘后原子替换；替换已有无效文件前使用现有 .snapshots 备份。Submission、Notes、Invitations、Edits 不改写。
- 只更新 resources.json、metadata.json 的附件状态及现有完成标志，不改变 json_collection_complete。缺表单时 main_collection_complete 仍可能是 false，请看 attachments_supplement.status 判断本次文件补采结果。
- 无法识别的外部主 PDF 地址会标记 unavailable；没有补充资料字段表示没有发现引用，不代表独立核实论文从无补充资料。
- 报告：代码目录 reports/iclr_attachments_report.json 与 .md；阶段进度、脱敏请求日志：data/collection/iclr_attachments/。


## Ubuntu

将上述入口和实现两个新文件同步到服务器对应的 scripts/、src/，保留服务器已有模块。
在项目目录、已激活 Python 3.11 环境中，将 --raw-root 换为服务器实际数据目录，例如：

```bash
python -u scripts/download_iclr_attachments.py --raw-root /home/ecs-user/openreview-research-data/data/raw --years 2013-2025 --authenticate --max-requests 100000 --request-delay 10 --wait-on-429 --min-free-gb 20
```


