# cbp7501-consumer

CBP Form 7501（美国海关进口报关单）PDF 解析服务。从 RabbitMQ 接收任务，下载 PDF 并解析为结构化 JSON，再将结果回传至输出队列。

## 功能

- **RabbitMQ 消费者**（`consumer.py`）：异步处理解析任务，支持并发、断线重连与错误回传
- **PDF 解析器 v1**（`parse_7501.py`）：针对常见 7501 版式提取表头、行项目、HTSUS、关税等信息
- **PDF 解析器 v2**（`parse_7501_v2.py`）：通用版解析器，适配不同进口商/承运人格式，可独立 CLI 使用

## 项目结构

```
cbp7501-consumer/
├── consumer.py          # RabbitMQ 消费者入口
├── parse_7501.py        # 7501 PDF 解析器（v1，consumer 默认使用）
├── parse_7501_v2.py     # 7501 PDF 解析器（v2，通用版）
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
pip install pika pdfplumber
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
# v1 解析器
python parse_7501.py files/input/MRKU5394955.pdf files/output/result.json

# v2 解析器（通用版）
python parse_7501_v2.py files/input/MRKU5394955.pdf files/output/result_v2.json
```

未指定输出路径时，结果打印到标准输出。

## 消息格式

### 输入队列（`INPUT_QUEUE`）

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
  "URL": "https://example.com/pre-signed/7501.pdf"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `ID` | 是 | 任务 ID，原样回传 |
| `URL` | 是 | 7501 PDF 下载地址（支持预签名 OBS 链接） |
| `FileNumber` | 否 | 业务单号，出错时一并回传 |

### 输出队列（`OUTPUT_QUEUE`）

**成功：**

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
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
  "status": "error",
  "message": "Download failed: ..."
}
```

## 解析结果说明

`data` 字段主要包含：

- **header**：表头信息（Entry Number、Entry Type、港口、日期、承运人、原产国等）
- **line_items**：行项目列表（商品描述、HTSUS、数量、关税税率与金额等）
- **line_item_count**：行项目数量

## 处理流程

```
INPUT_QUEUE → 下载 PDF → parse_7501.parse() → OUTPUT_QUEUE → ACK
                  ↓ 失败
              OUTPUT_QUEUE（status: error）→ ACK
```

无论成功或失败，消费者都会对原消息执行 **ACK**，避免任务堆积。

## 注意事项

- 请勿将 `.env`（含真实密码）提交到版本库；`.env.example` 仅作参考
- `files/input/`、`files/output/` 下的样例文件仅供本地测试
- v1 与 v2 解析器针对 PDF 版式差异做了兼容，若遇新格式可优先尝试 `parse_7501_v2.py`

## 许可证

未指定开源许可证，使用前请联系仓库维护者。
