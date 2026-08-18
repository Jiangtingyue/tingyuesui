# 大西瓜 JTYHome v8.9.8

本地优先的多模型 AI 对话 / 陪伴 Web 应用。主程序是 **FastAPI + Jinja + HTML/CSS/JS**，默认只监听本机 `127.0.0.1:5175`，因此可以直接用浏览器打开，不依赖 Xcode 原生 App。

> 当前仓库来自 `JTYHome-v8.9.8-DESKTOP-SLIM-CACHEFIX.zip` 的展开版本。原发行说明保存在 [`RELEASE_NOTES.md`](RELEASE_NOTES.md)。

## 直接在 Mac 浏览器打开

### 1. Python 版本

项目要求 **Python 3.11 或 3.12**（`pyproject.toml`：`>=3.11,<3.13`）。

### 2. 一键安装并启动

在项目目录执行：

```bash
/bin/bash ./install_mac.command
```

安装器会：

- 优先选择 `python3.12` / `python3.11`；
- 创建 `.venv`；
- 安装 `requirements.txt`；
- 启动本机 FastAPI 服务；
- 服务就绪后打开浏览器。

浏览器地址：

```text
http://127.0.0.1:5175/
```

这条浏览器启动路线 **不需要 Xcode，也不需要 macOS App Sandbox 的 network server entitlement**。

### 3. 已安装依赖后再次启动

```bash
/bin/bash ./start_jtyhome.command
```

或者：

```bash
.venv/bin/python app.py --browser default
```

不想自动打开浏览器：

```bash
.venv/bin/python app.py --no-browser
```

## 如果启动失败

先只看第一处真实错误，不要同时改多套配置。可直接运行：

```bash
.venv/bin/python app.py --no-browser
```

默认端口是 `5175`。如果端口被占用，可临时改成：

```bash
.venv/bin/python app.py --port 5176 --browser default
```

## 模型 / API

程序支持多条模型线路，包括 Anthropic、OpenAI、OpenRouter、DeepSeek，以及 Claude Code P 模式。API Key 不应提交进 Git；程序默认把凭据写入本机数据目录中的 `.jtyhome.env`。

常用凭据名包括：

```text
OPENROUTER_API_KEY
OPENAI_API_KEY
ANTHROPIC_API_KEY
DEEPSEEK_API_KEY
ELEVENLABS_API_KEY
DASHSCOPE_API_KEY
```

Claude Code P 模式使用本机 Claude Code 的订阅 OAuth；先在同一台电脑完成 `claude auth login`。

## 本地数据

源码和私人数据分开。默认优先使用项目目录下的：

```text
.jtyhome-data/
```

如果源码目录不可写，macOS 会回退到：

```text
~/Library/Application Support/JTYHome/
```

也可以显式设置：

```bash
export JTYHOME_DATA_DIR="$HOME/Library/Application Support/JTYHome"
```

仓库不应提交聊天数据库、API Key、附件、个人记忆或设备配对码。

## 主要目录

```text
app.py                  FastAPI 入口 / API / 页面服务
gateway.py              模型网关与流式调用
config.py               Provider、模型与本地配置
attachment_service.py   文件 / ZIP / PDF 等附件处理
cache_keepalive.py       Prompt Cache keepalive
browser_launcher.py      本地浏览器启动
runtime_paths.py         私人数据目录
providers/               Provider 适配层
templates/               Jinja 页面
static/                  CSS / JS / 图片 / 字体
vendor/                  本地第三方能力
```

## 当前版本里的缓存修复

v8.9.8 的当前发行包保留了两处针对 Anthropic 缓存连续性的修复：

1. cache keepalive 继承主会话 thinking shape，不再为了 keepalive 强制关闭 thinking；诊断使用独立 cache lane。
2. embodied prelude 继承主会话 thinking shape，只限制输出长度，避免改变请求形状导致缓存前缀失效。

完整历史变更见 [`RELEASE_NOTES.md`](RELEASE_NOTES.md)。

## 开发检查

不安装依赖也可以先做 Python 语法检查：

```bash
python3 -m compileall -q .
```

本发行包已通过该语法检查。完整运行仍需要安装 `requirements.txt`。

---

**Version:** 8.9.8  
**Default local URL:** `http://127.0.0.1:5175/`
