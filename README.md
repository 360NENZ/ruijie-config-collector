# 锐捷交换机配置采集工具

> **Ruijie Switch Configuration Collector** — 自动批量采集锐捷交换机运行配置

---

## 目录 / Table of Contents

- [功能特性](#功能特性)
- [工作原理](#工作原理)
- [快速开始](#快速开始)
- [配置文件说明](#配置文件说明)
- [输出文件格式](#输出文件格式)
- [命令行参数](#命令行参数)
- [常见问题](#常见问题)
- [English Summary](#english-summary)
- [许可证 / License](#许可证--license)

---

## 功能特性

- 通过锐捷 Web 管理接口（`/login.do` + `/web_cli.do`）自动登录并执行命令
- 从 Web CLI XML 响应的 `<mode-tip>` 字段中提取设备主机名
- 将主机名与 IP 的映射保存为 `<Hostname>_<IP>.json`
- 将 `show running-config` 输出保存为 `configs/<Hostname>_<IP>.text`
- 支持单个 IP、子网（CIDR）、IP 范围三种目标指定方式
- 多线程并发采集，显著缩短大规模采集时间
- 支持直接提供 `auth` token，也支持通过用户名/密码自动生成
- 详细的日志输出与采集摘要报告

---

## 工作原理

锐捷交换机 Web 管理界面提供两个关键接口：

| 接口 | 方法 | 说明 |
|------|------|------|
| `http://<IP>/login.do` | POST | 登录，Form Data: `auth=<token>` |
| `http://<IP>/web_cli.do` | POST | 执行 CLI 命令，返回 XML |

### 认证 Token 生成方式

锐捷 Web 登录将 `用户名:密码` 进行多轮 Base64 编码（通常 9 轮）：

```
token = base64( base64( ... base64("admin:password") ... ) )   ← 共 9 轮
```

你可以直接从浏览器开发者工具（Network → /login.do → Form Data → auth）复制 token，
也可以在 `config.yml` 中填写用户名和密码由工具自动计算。

### Web CLI 响应格式

```xml
<?xml version="1.0" encoding="utf-8"?>
<webcli-print>
  <mode-tip><![CDATA[DXL-5007#]]></mode-tip>   <!-- 主机名在此 -->
  <content><![CDATA[
    Building configuration...
    hostname DXL-5007
    ...
  ]]></content>
  <return-code>0</return-code>
</webcli-print>
```

`<mode-tip>` 中 `#` 或 `>` 之前的部分即为主机名（如 `DXL-5007`）。

---

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/360NENZ/ruijie-config-collector.git
cd ruijie-config-collector
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> Python 3.10 或以上版本。

### 3. 编辑配置文件

```bash
cp config.yml my-config.yml
# 根据实际情况修改目标 IP 和认证信息
vim my-config.yml
```

**最小配置示例（使用用户名/密码）：**

```yaml
auth:
  username: "admin"
  password: "your_password"

targets:
  ips:
    - 172.31.99.83
```

**最小配置示例（使用浏览器抓包的 token）：**

```yaml
auth:
  token: "aFhTRUpVVmxWYVYxWlZN..."

targets:
  subnets:
    - 172.31.99.0/24
```

### 4. 运行

```bash
# 使用默认 config.yml
python main.py

# 指定配置文件和输出目录
python main.py -c my-config.yml -o /data/switch-backups

# 预览目标列表（不实际连接）
python main.py --dry-run

# 开启详细日志
python main.py -v
```

### 5. 查看输出

```
./
├── DXL-5007_172.31.99.83.json       ← 主机名与 IP 映射
└── configs/
    └── DXL-5007_172.31.99.83.text   ← running-config 文本
```

---

## 配置文件说明

```yaml
# ── 认证 ──────────────────────────────────────────────────────────────────
auth:
  # 方式 1：直接粘贴从浏览器抓包得到的 auth token（优先级更高）
  # token: "aFhTRUpVVmxWYVYxWlZN..."

  # 方式 2：填写用户名和密码，工具自动计算 token
  username: "admin"
  password: "your_password"
  encoding_rounds: 9   # Base64 编码轮数，默认 9（大多数固件适用）

# ── 目标设备 ──────────────────────────────────────────────────────────────
targets:
  # 单个 IP 列表
  ips:
    - 192.168.1.1

  # CIDR 子网（自动枚举所有主机地址）
  subnets:
    - 192.168.1.0/24

  # IP 范围（末段）
  ranges:
    - 10.0.0.1-254

# ── 请求参数 ──────────────────────────────────────────────────────────────
request:
  timeout: 10        # 超时秒数
  verify_ssl: false  # 是否校验 HTTPS 证书（交换机自签名证书时请设为 false）
  concurrent: 5      # 并发线程数
```

---

## 输出文件格式

### `<Hostname>_<IP>.json`

```json
{
  "hostname": "DXL-5007",
  "ip": "172.31.99.83",
  "collected_at": "2026-03-17T10:30:00"
}
```

### `configs/<Hostname>_<IP>.text`

纯文本，内容与在交换机 CLI 中执行 `show running-config` 的输出完全一致：

```
Building configuration...
Current configuration: 5713 bytes

version SF29_RGOS 11.4(1)B81P3
hostname DXL-5007
...
end
```

---

## 命令行参数

```
usage: ruijie-collector [-h] [-c FILE] [-o DIR] [-v] [--dry-run] [--decode-token TOKEN]

选项：
  -h, --help              显示帮助信息
  -c, --config FILE       配置文件路径（默认：config.yml）
  -o, --output DIR        输出目录（默认：当前目录）
  -v, --verbose           启用 DEBUG 级别日志
  --dry-run               仅打印目标 IP，不实际连接
  --decode-token TOKEN    解码一个 Ruijie auth token，输出用户名和密码
```

### 解码已有 Token

如果你只有 token 但想知道对应的用户名和密码：

```bash
python main.py --decode-token "aFhTRUpVVmxWYVYx..."
```

---

## 常见问题

**Q：登录失败，提示 "Login failed"**
- 检查 IP 是否可达（ping 测试）
- 确认用户名和密码正确，或直接使用从浏览器抓包得到的 auth token
- 部分旧固件可能需要将 `encoding_rounds` 改为 8 或 10

**Q：`hostname` 为 `None`，配置采集失败**
- 确保该设备已登录成功（查看日志是否出现 `Login OK`）
- 执行 `show running-config` 可能超时，可适当增大 `timeout`

**Q：如何一次性采集整个 /24 网段？**
```yaml
targets:
  subnets:
    - 172.31.99.0/24
```
不可达的 IP 会在超时后自动跳过，不影响其他设备的采集。

**Q：如何提高采集速度？**
增大 `request.concurrent` 的值（建议不超过 20，以免触发交换机的连接限制）。

---

## English Summary

This tool automates the retrieval of running configurations from **Ruijie** (Ruijie Networks / 锐捷) managed switches that expose a web management interface.

### How it works

1. **Login** – POST to `/login.do` with a multi-round Base64-encoded credential token.
2. **Web CLI** – POST to `/web_cli.do` with `command=show+running-config`.
3. **Parse** – Extract the hostname from the `<mode-tip>` XML field (e.g., `DXL-5007#` → `DXL-5007`).
4. **Save** – Write `<Hostname>_<IP>.json` (mapping) and `configs/<Hostname>_<IP>.text` (config).

### Quick start

```bash
pip install -r requirements.txt
# Edit config.yml with your credentials and IP targets
python main.py
```

Run `python main.py --help` for all CLI options.

---

## 许可证 / License

MIT License — 详见 [LICENSE](LICENSE) 文件。  
See the [LICENSE](LICENSE) file for full terms.
