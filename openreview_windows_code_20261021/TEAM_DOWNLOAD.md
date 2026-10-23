# 按年份补采 PDF 和补充材料

使用自己的合法 OpenReview 账号；不共享密码、Token。

## 获取代码与原始 JSON

安装 Git 和 Python 3.11 后，在终端运行：

```text
git clone --filter=blob:none --sparse https://github.com/hch514477-boop/openreview-research-data.git
cd openreview-research-data
git sparse-checkout set openreview_windows_code_20261021 data/raw/ICLR_2021
cd openreview_windows_code_20261021
```

将 `ICLR_2021` 和命令中的 `--years 2021` 换成分配到的年份。

## Windows CMD / PowerShell

在上面的代码目录中：

```text
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/download_iclr_attachments.py --raw-root ../data/raw --years 2021 --dry-run
```

先试两篇：

```text
.\.venv\Scripts\python.exe -u scripts/download_iclr_attachments.py --raw-root ../data/raw --years 2021 --limit 2 --authenticate --max-requests 10 --request-delay 10 --min-free-gb 20
```

确认结果后补采该年份：

```text
.\.venv\Scripts\python.exe -u scripts/download_iclr_attachments.py --raw-root ../data/raw --years 2021 --authenticate --max-requests 100000 --workers 5 --request-delay 1 --min-free-gb 20
```

邮箱和密码仅在本机终端交互输入；密码不回显、不保存。不要把凭据写进命令或发给其他人。

## Ubuntu / Debian

在相同代码目录中，使用已安装的 Python 3.11：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/download_iclr_attachments.py --raw-root ../data/raw --years 2021 --dry-run
.venv/bin/python -u scripts/download_iclr_attachments.py --raw-root ../data/raw --years 2021 --authenticate --max-requests 100000 --workers 5 --request-delay 1 --min-free-gb 20
```

## 结果与继续运行

- 论文 PDF：仓库根目录 `data/raw/ICLR_2021/<paper_id>/paper.pdf`。
- 投稿补充材料：论文目录内的 `supplementary_material.zip` 等文件，按原字段名保存；讨论附件位于 `attachments/<note_id>/`。具体位置与最终状态见 `resources.json`、`metadata.json`。
- 上述整年命令使用 5 个论文工作线程，全局附件请求间隔至少 1 秒；不是每线程每秒各发一次。未传 `--workers` 时仍默认单线程、间隔 10 秒。
- 并行模式遇到 429 会停止新请求，等待在途任务落盘；已发出的请求可能继续返回。冷却到期后重新运行同一命令继续，保留本地状态目录。并行模式不能加 `--wait-on-429`；该选项仅用于单线程。403/认证失败也停止。
- API v1 的补充材料直接调用官方 SDK `get_attachment(id=note_id, field_name=field)`，不再先请求会返回 404 的附件哈希路径。下载到的是当前字段附件，历史版本一致性未核实；v1 并不保证没有限流。
- 默认保留 20 GiB 空闲空间，单文件上限 512 MiB；到达限制会停止并记录状态。请求预算耗尽可重新运行同一命令继续。
- 本脚本不下载 Edits 或外部代码仓库，不运行、不解压下载材料；无补充材料引用不等于独立证明没有补充材料。

更多参数及历史版本限制见 [下载说明](scripts/README_iclr_attachments.md)。代码中包含被复用的采集辅助模块，仅运行上述下载入口即可。

