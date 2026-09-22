# DICOM 匿名化工作台（FastAPI + pydicom）

一个完全本地运行的网页 DICOM 检查匿名化工作台：

1. **上传** 一组影像文件（支持多选文件、整个文件夹、ZIP / TAR 包）；
2. **选择匿名化配置**（基础 / 标准 / 严格），即时预览患者 → 检查 → 序列层级与**逐字段变更清单**；
3. 一键执行匿名化并**打包下载 ZIP**，附 TXT 变更报告。

核心能力：

- **患者级稳定伪名**：以患者身份（PatientID，回退姓名/检查）为锚点，基于加盐 SHA-256
  确定性地重写 `PatientName` / `PatientID` 以及 Patient、Study、Series、
  FrameOfReference、Concatenation、SOP 各级 **UID**。同一患者多次上传得到同一伪名，
  且不可逆；
- **遍历序列内引用**：递归处理所有 DICOM Sequence（`ReferencedSeriesSequence`、
  `SourceImageSequence`、`ReferencedImageSequence`、SR `ContentSequence` 等），
  将其中引用的 UID 同步替换，**检查内部关系保持不变**；SOP Class / Transfer Syntax
  等编码 UID 绝不改动；
- **逐字段变更清单**：界面与报告中列出每个标签（Tag、关键字、VR）、变更前示例值、
  变更后内容、影响文件数；
- **零外部依赖**：不需要数据库、不需要任何第三方账号，所有处理发生在本机。

## 单条命令启动

需要 Docker（含 Compose v2）：

```bash
docker compose up --build
```

然后浏览器访问：

```
http://localhost:8000
```

停止：`Ctrl+C`（后台运行用 `docker compose up -d --build`，停止用 `docker compose down`）。

### 不用 Docker 时（本地 Python 3.11+）

一条命令（自动建虚拟环境、装依赖、起服务）：

```bash
./run.sh
```

或手动：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 使用流程

1. 打开 <http://localhost:8000>，拖入文件/文件夹/ZIP，点击 **上传并扫描**；
2. 选择配置卡片，下方自动刷新：
   - **摘要指标**（文件 / 患者 / 检查 / 序列 / 变更字段数 / 含私有标签文件数）；
   - **层级树**：Patient/Study/Series UID 的 `原值 → 伪名值`；
   - **逐字段变更清单 / UID 映射 / 文件清单**三个标签页；
3. 点击 **执行匿名化并打包 ZIP**，下载 `anonymized_xxxxxxxx.zip`
   与 `ANONYMIZATION_REPORT.txt`。

ZIP 内部按伪名层级组织：
`<伪名患者>/<伪名检查>/<伪名序列>/IMG_00001.dcm`，并包含变更报告。

## 匿名化配置

| 配置 | 清理内容 | 保留内容 |
| --- | --- | --- |
| **基础 basic** | 患者姓名/ID/生日/联系方式、主管与操作医师、AccessionNumber、AdmissionID | 机构、设备、检查日期、私有标签 |
| **标准 standard**（默认） | 基础配置 + 机构/科室、医师组、设备序列号/站名/探测器 ID、工作流单号、全部自由文本注释、**全部私有标签 (odd group)** | 年龄、性别、身高体重、检查日期 |
| **严格 strict** | 标准配置 + 年龄/性别/身高体重 + **全部检查与采集日期、时间** + 来源 AE Title | — |

三个配置都会执行：

- PatientName → `ANON^PATIENT^xxxxxxxx`、PatientID → `ANON-XXXXXXXXXXXX`（患者级稳定）；
- Patient/Study/Series/FrameOfReference/Concatenation/SOP Instance UID 全量确定性重写，
  并同步 File Meta Information 中的 `MediaStorageSOPInstanceUID`；
- 递归遍历所有 Sequence 内的 UID 引用，保持引用一致；
- 写入审计标签：`(0012,0062) PatientIdentityRemoved = YES`、
  `(0012,0063) DeidentificationMethod`。

> 注意：这是工程脱敏工具，不等同于经过认证的 DICOM PS3.15 Annex E “Basic Application
> Confidentiality Profile” 完整实现（未做像素烧录识别、波形覆盖等）。对外分发前请用
> DICOM 查看器人工核验图像内容。

## UID 重写原理

```
new_uid = ROOT_<domain> . ( SHA-256( salt | original_uid ) mod 10^25 )
```

- 根前缀采用 DICOM `2.25.` ISO 分支 + 随机组织后缀，总长合规（≤64 字符，仅数字与点）；
- `salt` 是服务端持久化随机盐（首次运行生成，保存在 `$WORK_ROOT/server.salt`，
  权限 `600`）。**同一服务上的同一患者，无论分多少批上传，都映射到同一个伪名**
  （患者级稳定）；删除该盐文件即可让所有历史伪名失效，且没有盐无法由新 UID 反推原值；
- 每个域（patient / study / series / frame / sop / concat）独立映射表，
  不会把 SOP UID 误当成 Study UID。

## REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/profiles` | 配置列表 |
| `POST` | `/api/jobs` | multipart 上传 `files`（可多个/重复），自动解压 zip/tar，返回 `job_id` |
| `GET` | `/api/jobs/{id}/preview?profile=standard` | 层级、UID 映射、逐字段变更、文件清单 |
| `POST` | `/api/jobs/{id}/anonymize` | form 字段 `profile`，执行并打包 |
| `GET` | `/api/jobs/{id}/download` | 下载匿名化 ZIP |
| `GET` | `/api/jobs/{id}/report` | 下载 TXT 变更报告 |
| `DELETE` | `/api/jobs/{id}` | 立即删除任务数据 |

快速验证：

```bash
curl -f http://localhost:8000/api/health
curl -f -F "files=@study.zip" http://localhost:8000/api/jobs
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WORK_ROOT` | `/tmp/dicom-anon-jobs`（容器内 `/data/jobs`） | 任务文件目录 |
| `JOB_TTL_SECONDS` | `3600` | 任务保留时长，启动时清理过期任务 |
| `MAX_UPLOAD_BYTES` | `4294967296`（4 GiB） | 单次上传总大小上限 |

## 运行测试

```bash
pip install pytest
python -m pytest -q
```

测试会用 pydicom 在内存中合成带交叉引用的多序列检查（含私有标签），验证：
身份字段清除、UID 全量重写、序列间引用一致、患者级稳定性、三个配置的差异。

## 技术栈与目录

```
app/
  main.py          FastAPI 路由（上传 / 预览 / 执行 / 下载）
  config.py        三个匿名化配置与标签集合
  anonymizer.py    扫描、确定性 UID 映射、标签重写引擎（递归 SQ）
  pipeline.py      扫描 → 变更清单 → 写出 → ZIP/报告 编排
  storage.py       上传落盘、ZIP/TAR 安全解压、打包、TTL 清理
  static/          原生 HTML/CSS/JS 单页界面（无构建步骤）
Dockerfile · docker-compose.yml · requirements.txt · tests/
```

FastAPI / Uvicorn / pydicom（纯 Python DICOM 读写）/ 原生前端。
