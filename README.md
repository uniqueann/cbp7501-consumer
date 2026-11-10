# cbp7501-consumer

> [English documentation](README.en.md)

CBP Form 7501（美国海关进口报关单）与加拿大 CBSA BSF864 PDF 解析服务。从 RabbitMQ 接收任务，下载 PDF 并按 `CbpFormType` 选择解析器，将结果回传至输出队列。

## 功能

- **RabbitMQ 消费者**（`consumer.py`）：异步处理解析任务，支持并发、断线重连与错误回传；按 `CbpFormType` 路由解析器
- **PDF 解析器 v1**（`parse_7501.py`）：`CbpFormType=10`（默认），针对常见 7501 版式提取表头、行项目、HTSUS、关税等信息
- **加拿大解析器**（`parse_cad.py`）：`CbpFormType=20`，解析 CBSA BSF864，输出 BSF864 原生结构（`schema_version` 2.0），金额单位 CAD

## 项目结构

```
cbp7501-consumer/
├── consumer.py          # RabbitMQ 消费者入口
├── parse_7501.py        # 7501 PDF 解析器（CbpFormType=10）
├── parse_cad.py         # 加拿大 BSF864 解析器（CbpFormType=20）
├── .env.example         # 环境变量示例
├── files/
│   ├── input/           # 本地测试用 PDF / JSON 样例
│   └── output/          # 本地解析结果样例
└── README.md
```

## 环境要求

- Python 3.10+
- RabbitMQ（运行 consumer 时需要）

## 安装

```bash
pip install -r requirements.txt
```

复制环境变量模板并按需修改：

```bash
cp .env.example .env
```

## 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `RABBITMQ_HOST` | RabbitMQ 主机 | `localhost` |
| `RABBITMQ_PORT` | AMQP 端口 | `5672` |
| `RABBITMQ_USER` | 用户名 | `guest` |
| `RABBITMQ_PASS` | 密码 | `guest` |
| `RABBITMQ_VHOST` | 虚拟主机 | `/` |
| `INPUT_QUEUE` | 输入队列 | `cbp7501.parse.request` |
| `OUTPUT_QUEUE` | 输出队列 | `cbp7501.parse.result` |
| `PREFETCH_COUNT` | 并发处理消息数 | `4` |

队列会在启动时自动声明为 **durable**。

## 使用方法

### 启动消费者

```bash
# Linux / macOS
export $(grep -v '^#' .env | xargs)
python consumer.py

# Windows PowerShell（示例）
Get-Content .env | ForEach-Object {
  if ($_ -match '^([^#=]+)=(.*)$') { Set-Item -Path "env:$($matches[1])" -Value $matches[2] }
}
python consumer.py
```

### 本地解析 PDF（命令行）

```bash
# 美国 7501（CbpFormType=10）
python parse_7501.py files/input/MRKU5394955.pdf files/output/result.json

# 加拿大 BSF864（CbpFormType=20）
python parse_cad.py files/input/CAD-1.pdf files/output/CAD-1.json
```

未指定输出路径时，结果打印到标准输出。

## 消息格式

### 输入队列（`INPUT_QUEUE`）

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
  "URL": "https://example.com/pre-signed/7501.pdf",
  "CbpFormType": 10
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `ID` | 是 | 任务 ID，原样回传 |
| `URL` | 是 | PDF 下载地址（支持预签名 OBS 链接） |
| `FileNumber` | 否 | 业务单号，出错时一并回传 |
| `CbpFormType` | 否 | 整型：`10` 美国整柜（默认），`20` 加拿大整柜 |

### 输出队列（`OUTPUT_QUEUE`）

**成功：**

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
  "CbpFormType": 10,
  "status": "ok",
  "data": {
    "header": { "...": "..." },
    "line_items": [ "..." ],
    "line_item_count": 12
  }
}
```

**失败：**

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
  "CbpFormType": 10,
  "status": "error",
  "message": "Download failed: ..."
}
```

## 解析结果说明

**`CbpFormType=10`（7501）** 时 `data` 主要包含：

- **header**：表头信息（Entry Number、Entry Type、港口、日期、承运人、原产国等）
- **line_items**：行项目列表（商品描述、HTSUS、数量、关税税率与金额等）
- **line_item_count**：行项目数量

**`CbpFormType=20`（加拿大 BSF864）** 时 `data` 采用 BSF864 原生结构（`schema_version` 2.0），不再模仿 7501：

```json
{
  "schema_version": "2.0",
  "document":   { "form": "CBSA BSF864", "pages": 9,
                  "printed_at": "2026-11-06 17:02", "notes": "DFSU7422980" },
  "header":     { "type_code": "AB", "office_no": "495", "importer_bn": "756552105RM0001",
                  "gross_weight_kg": 19859.53, "cargo_control_no": "...",
                  "importer": { "name": "...", "street": "...", "city": "...",
                                "zip": "V6C1S4", "country": "CA" } },
  "invoices":   [ { "page": 2, "invoice_no": "...", "invoice_value": 10006.22,
                    "invoice_currency": "USD", "freight": 0.0, "vendor": {...} } ],
  "totals":     { "currency": "CAD", "value_for_duty": 13584.45, "duty": 0.0,
                  "gst": 679.22, "pst_hst": 0.0, "total_duties_and_taxes": 679.22,
                  "value_for_tax": 13584.45, "value_for_tax_derived": true },
  "line_items": [ { "line_no": 1, "hs_code": "9403.60.9090", "hs_code_6": "9403.60",
                    "description": "STORAGE CABINET", "quantity": 578,
                    "value_for_duty": 13584.45, "gst": 679.22, "line_total": 679.22 } ],
  "line_item_count": 1,
  "warnings":   [ { "code": "EMPTY_HEADER_FIELDS", "fields": ["signed_date"] } ]
}
```

要点：

- **`document`**：表单标识、页数、打印时间（来自页脚 `Printed`）、字段 35 备注
- **`header`**：BSF864 字段 1-22 的扁平映射；`importer` 是 `{name, street, city, zip, country}`，`importer_bn` 独立字段（表单未印时为 `null`）
- **`invoices`**：字段 36-43 是**页级**块（一张发票可能跨多页），故独立成数组；行项目用 `invoice_ref` 指向其索引
- **`totals`**：字段 113-130 的完整税目拆分；`*_usd` 后缀已弃用，`currency` 恒为 `"CAD"`
- **`value_for_tax`**：BSF864 申报级**没有**该字段，此值由 `sum(line_items[].value_for_tax)` 派生，并置 `value_for_tax_derived: true`
- **`line_items`**：`hs_code` 为完整 **10 位**（`9403.60.9090`），另给 `hs_code_6`；`line_no` 为整数并保留 `line_no_raw` 原文；各税目为独立扁平字段
- **`warnings`**：区分「表单本身空白」（`EMPTY_HEADER_FIELDS` / `EMPTY_QUANTITY`）与「解析异常」（`TOTAL_MISMATCH` / `MISSING_DECLARATION_TOTAL`），另有 `DERIVED_VALUE` 标注派生值

## 处理流程

```
INPUT_QUEUE → 下载 PDF → CbpFormType?
                              ├─ 10 → parse_7501.parse()
                              └─ 20 → parse_cad.parse()
                                    → OUTPUT_QUEUE → ACK
                  ↓ 失败
              OUTPUT_QUEUE（status: error）→ ACK
```

无论成功或失败，消费者都会对原消息执行 **ACK**，避免任务堆积。

## 注意事项

- 请勿将 `.env`（含真实密码）提交到版本库；`.env.example` 仅作参考
- `files/input/`、`files/output/` 下的样例文件仅供本地测试
- v1 解析器针对常见 PDF 版式做了兼容；如遇不同版式，可在本地调整或扩展解析规则

## 许可证

本仓库为**专有软件，保留所有权利**。未经版权持有人事先书面许可，不得使用、复制、修改、分发或再许可。

在 GitHub 上公开可见仅为展示与协作便利，不构成任何权利许可。详见 [LICENSE](LICENSE)。
