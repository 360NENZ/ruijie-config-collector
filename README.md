# 锐捷交换机配置采集工具

> **Ruijie Switch Configuration Collector**
> 通过 Web 管理接口批量登录锐捷交换机，自动采集 `show running-config` 并归档。

<p align="left">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
  <img alt="Author" src="https://img.shields.io/badge/Author-360NENZ-orange">
</p>

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
- 按照前端 JS（`base.dao.js` / `login.js`）中 `getPasswordEncode()` 的真实算法生成 auth token
- 从 Web CLI XML 响应的 `<mode-tip>` 字段中提取设备主机名（如 `DXL-5007#` → `DXL-5007`）
- 所有设备的主机名与 IP 映射统一写入 `maps.json`，追加更新，不覆盖历史记录
- 将 `show running-config` 输出保存为 `configs/<Hostname>_<IP>.text`
- 支持单个 IP、CIDR 网段、末段范围三种目标指定方式，可混用
- 多线程并发采集，线程安全写入 `maps.json`
- 支持直接粘贴浏览器抓包的 token，也支持填写明文用户名/密码自动生成
- `--decode-token` 实用工具：反解 auth token 还原用户名和密码

---

## 工作原理

### 接口说明

锐捷交换机 Web 管理界面提供两个关键接口：

| 接口 | 方法 | 说明 |
|------|------|------|
| `http://<IP>/login.do` | POST | 登录，Form Data: `auth=<token>` |
| `http://<IP>/web_cli.do` | POST | 执行 CLI 命令，返回 XML |

### Auth Token 算法（源自 `base.dao.js` / `login.js`）

前端 JavaScript 的 `getPasswordEncode()` 函数定义如下：

```javascript
getPasswordEncode: function(password) {
    pa = "aFhTRUpVVmxWYVYxWlZN";
    for (i = 0; i < 5; i++) {
        password = base64.encode(password);   // 标准 Base64 编码
        password = pa + password;             // 前缀拼接
    }
    return password;
}
```

登录时，传入的初始值为 `"username:password"`（即 `_makeAuth` 拼接后的字符串）。

**等价 Python 实现：**

```python
PREFIX = "aFhTRUpVVmxWYVYxWlZN"

def generate_auth_token(username: str, password: str) -> str:
    token = f"{username}:{password}"
    for _ in range(5):
        token = base64.b64encode(token.encode()).decode()
        token = PREFIX + token
    return token
```

**验证示例：**

```
输入: admin:ruijie@123
输出: aFhTRUpVVmxWYVYxWlZNYUZoVFJVcFZWbXhX...VFRGQ1ZVMUVNRDA9
```

> ⚠️ 此前版本（v1）错误地实现为纯 9 轮 Base64，已在 v2 修正。

### Web CLI 响应格式

```xml
<?xml version="1.0" encoding="utf-8"?>
<webcli-print>
  <mode-tip><![CDATA[DXL-5007#]]></mode-tip>   <!-- ← 主机名在此 -->
  <content><![CDATA[
Building configuration...
hostname DXL-5007
...
  ]]></content>
  <return-code>0</return-code>
</webcli-print>
```

`<mode-tip>` 中 `#` 或 `>` 之前的部分为主机名（如 `DXL-5007`）。

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

> 需要 Python 3.10 或以上版本。

### 3. 编辑配置文件

```bash
# 直接修改 config.yml，或复制一份
cp config.yml my-config.yml
```

**最小配置（明文用户名/密码）：**

```yaml
auth:
  username: "admin"
  password: "ruijie@123"

targets:
  ips:
    - 172.31.99.83
```

**使用浏览器抓包的 token（推荐用于已有 token 的场景）：**

```yaml
auth:
  token: "aFhTRUpVVmxWYVYxWlZNYUZoVFJVcFZWbXhX..."

targets:
  subnets:
    - 172.31.99.0/24
```

### 4. 运行

```bash
# 默认使用当前目录的 config.yml，输出到当前目录
python main.py

# 指定配置文件和输出目录
python main.py -c my-config.yml -o /data/backups

# 预览目标 IP 列表（不实际连接）
python main.py --dry-run

# 开启详细日志（DEBUG 级别）
python main.py -v
```

### 5. 查看输出

```
./
├── maps.json                            ← 所有设备的主机名与 IP 映射
└── configs/
    └── DXL-5007_172.31.99.83.text       ← running-config 文本
```

---

## 配置文件说明

```yaml
# ── 认证 ─────────────────────────────────────────────────────────────────────
auth:
  # 方式 1：从浏览器抓包直接粘贴 auth token（优先使用）
  # token: "aFhTRUpVVmxWYVYxWlZN..."

  # 方式 2：明文凭据，工具按 getPasswordEncode() 算法自动生成 token
  username: "admin"
  password: "ruijie@123"

# ── 目标设备 ──────────────────────────────────────────────────────────────────
targets:
  ips:            # 单个 IP 列表
    - 192.168.1.1
  subnets:        # CIDR 子网（自动枚举所有主机地址）
    - 192.168.1.0/24
  ranges:         # IP 范围（末段）
    - 10.0.0.1-254

# ── 请求参数 ──────────────────────────────────────────────────────────────────
request:
  timeout: 10        # 超时秒数
  verify_ssl: false  # 是否校验 HTTPS 证书（自签名证书请保持 false）
  concurrent: 5      # 并发线程数
```

---

## 输出文件格式

### `maps.json` — 主机名与 IP 映射

所有成功采集的设备均写入同一个 `maps.json`，键为 IP 地址。
重复运行时会自动更新已有条目，而不是创建新文件。

```json
{
  "172.31.99.83": {
    "hostname":     "DXL-5007",
    "ip":           "172.31.99.83",
    "collected_at": "2026-03-17T10:30:00"
  },
  "172.31.99.84": {
    "hostname":     "DXL-5008",
    "ip":           "172.31.99.84",
    "collected_at": "2026-03-17T10:30:05"
  }
}
```

### `configs/<Hostname>_<IP>.text` — Running-Config

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
  -h, --help              显示帮助信息并退出
  -c FILE, --config FILE  配置文件路径（默认：config.yml）
  -o DIR, --output DIR    输出目录（默认：当前目录）
  -v, --verbose           启用 DEBUG 级别日志
  --dry-run               仅打印目标 IP，不实际连接
  --decode-token TOKEN    反解 Ruijie auth token，输出明文用户名和密码
```

### 反解已有 Token

```bash
python main.py --decode-token "aFhTRUpVVmxWYVYxWlZNYUZo..."
# Username : admin
# Password : ruijie@123
```

---

## 常见问题

**Q：登录失败，提示 "Login failed"**
- 确认设备 IP 可达（`ping` 测试）
- 检查用户名和密码是否正确
- 可以改用从浏览器 DevTools 抓取的原始 auth token（`auth.token` 字段）

**Q：主机名提取失败（hostname = None）**
- 登录成功后 `web_cli.do` 可能超时，可适当增大 `request.timeout`
- 部分固件的 `<mode-tip>` 格式略有不同；可通过 `-v` 查看原始 XML 响应

**Q：如何一次性采集整个 /24 网段？**
```yaml
targets:
  subnets:
    - 172.31.99.0/24
```
不可达的 IP 超时后自动跳过，不影响其他设备。

**Q：如何提升采集速度？**
调大 `request.concurrent`（建议不超过 20，避免触发交换机的连接速率限制）。

**Q：多次运行会不会产生重复文件？**
- `maps.json` 会原地更新已有条目
- `configs/<Hostname>_<IP>.text` 会覆盖同名文件（最新配置）

---

## English Summary

This tool automates the batch retrieval of running configurations from
**Ruijie** (锐捷) managed switches that expose a web management interface.

### Algorithm

The auth token is generated by the front-end JS (`base.dao.js` / `login.js`)
via `getPasswordEncode()`: 5 rounds of standard Base64-encode followed by
prepending the fixed string `"aFhTRUpVVmxWYVYxWlZN"`. Starting value is
`"<username>:<password>"`.

```python
PREFIX = "aFhTRUpVVmxWYVYxWlZN"
token  = f"{username}:{password}"
for _ in range(5):
    token = base64.b64encode(token.encode()).decode()
    token = PREFIX + token
```

### How it works

1. **Login** – POST to `/login.do` with `auth=<token>`.
2. **Web CLI** – POST to `/web_cli.do` with `command=show+running-config`.
3. **Parse** – Extract hostname from `<mode-tip>` XML field (`DXL-5007#` → `DXL-5007`).
4. **Save** – Append to `maps.json` and write `configs/<Hostname>_<IP>.text`.

### Quick start

```bash
pip install -r requirements.txt
# Edit config.yml with your credentials and targets
python main.py
```

Run `python main.py --help` for all options.

---

## 许可证 / License

MIT License — 详见 [LICENSE](LICENSE) 文件。  
See the [LICENSE](LICENSE) file for full terms.

---

*作者 / Author: [360NENZ](https://github.com/360NENZ)*
