# cbp7501-consumer

> [English documentation](README.en.md)

CBP Form 7501（美国海关进口报关单）与加拿大 CBSA BSF864 PDF 解析服务。从 RabbitMQ 接收任务，下载 PDF 并按 `CbpFormType` 选择解析器，将结果回传至输出队列。

## 功能

- **RabbitMQ 消费者**（`consumer.py`）：异步处理解析任务，支持并发、断线重连与错误回传；按 `CbpFormType` 路由解析器
- **PDF 解析器 v1**（`parse_7501.py`）：`CbpFormType=10`（默认），针对常见 7501 版式提取表头、行项目、HTSUS、关税等信息
- **加拿大解析器**（`parse_cad.py`）：`CbpFormType=20`，解析 CBSA BSF864，输出与 7501 结果同构的 schema；金额为 CAD（见下方币种说明）

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

**`CbpFormType=20`（加拿大 BSF864）** 时 `data` 的 `header` / `line_items` / `line_item_count` 结构与 7501 **完全一致**，便于下游统一处理。语义差异需注意：

- `header.currency` 为 `"CAD"`，`header.invoice_currency` 取表单字段 40（发票币种）
- `totals.*_usd` 系列键名沿用 7501，但**其中的数值是 CAD**，使用前必须先判断 `header.currency`
- `totals.other_fees_usd` = `grand_total - duty`，实际是 GST/PST 等税费合计
- `line_items[].fees[]` 承载 GST/PST/消费税等税目，`code` 为 BSF864 字段号（如 `"90"` 为 GST），`rate` 恒为 `null`
- `header.consignee` 为空白占位；进口商信息在 `header.importer_of_record`
- 7501 专属字段（`surety_number`、`bond_type`、`mpf_usd`、`hmf_usd` 等）恒为空字符串或 `null`

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
