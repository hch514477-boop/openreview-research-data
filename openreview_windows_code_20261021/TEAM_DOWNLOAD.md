# 组员按年份补采 PDF 和补充材料

每人负责一个不同年份，例如 2021。使用自己的合法 OpenReview 账号；不共享密码、Token，也不通过切换身份或 IP 绕过限速。分工不会消除账号、网络或接口的服务端限速，遇到限制仍须等待。

## 获取代码与原始 JSON

安装 Git 和 Python 3.11 后，在终端运行：

```text
git clone --filter=blob:none --sparse https://github.com/hch514477-boop/openreview-research-data.git
cd openreview-research-data
git sparse-checkout set openreview_windows_code_20261021 data/raw/ICLR_2021
cd openreview_windows_code_20261021
```

将 `ICLR_2021` 换成分配到的年份。必须等对应年份数据上传完毕后再获取；本脚本只下载已保存 JSON 中引用的材料，不发现新论文。

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
.\.venv\Scripts\python.exe -u scripts/download_iclr_attachments.py --raw-root ../data/raw --years 2021 --authenticate --max-requests 100000 --request-delay 10 --wait-on-429 --min-free-gb 20
```

邮箱和密码仅在本机终端交互输入；密码不回显、不保存。不要把凭据写进命令或发给其他人。

## Ubuntu / Debian

在相同代码目录中，使用已安装的 Python 3.11：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/download_iclr_attachments.py --raw-root ../data/raw --years 2021 --dry-run
.venv/bin/python -u scripts/download_iclr_attachments.py --raw-root ../data/raw --years 2021 --authenticate --max-requests 100000 --request-delay 10 --wait-on-429 --min-free-gb 20
```

## 结果与继续运行

- 论文 PDF：仓库根目录 `data/raw/ICLR_2021/<paper_id>/paper.pdf`。
- 其他材料：同一论文目录的 `attachments/<note_id>/`，具体位置与状态见 `resources.json`、`metadata.json`。
- 汇总报告：代码目录 `reports/iclr_attachments_report.json` 和 `.md`；不要把 dry-run 报告当成下载结果。
- Ctrl+C 后重新运行同一命令，已下载且来源、格式、SHA-256 均匹配的文件跳过。
- 单进程串行下载，默认请求间隔 10 秒。429 按服务端冷却信息等待；403/认证失败停止。不要在同一台机器为同一年启动多个进程，也不要同时修改同一批原始 JSON。
- 默认保留 20 GiB 空闲空间，单文件上限 512 MiB；到达限制会停止并记录状态。请求预算耗尽可重新运行同一命令继续。
- 本脚本不下载 Edits 或外部代码仓库，不运行、不解压下载材料；无补充材料引用不等于独立证明没有补充材料。
- 下载结果先本地保存，交回方式由项目负责人统一安排；不要直接提交密码、运行缓存或整批 PDF 到 GitHub。

更多参数及历史版本限制见 [下载说明](scripts/README_iclr_attachments.md)。代码中包含被复用的采集辅助模块，仅运行上述下载入口即可；不需要启动其他采集器。
