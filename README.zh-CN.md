<!-- markdownlint-disable-file MD033 MD045 -->
<div align="center">

# XianyuBot

**基于 FastAPI 与 CDP / Playwright 的多账号闲鱼自动化客服、自动履约与智能运营系统**

[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-1.59+-2EAD33.svg?style=flat-square&logo=playwright&logoColor=white)](https://playwright.dev/)
[![Docker](https://img.shields.io/badge/Docker-GHCR%20Image-2496ED.svg?style=flat-square&logo=docker&logoColor=white)](https://github.com/dengyie/xianyu-bot/pkgs/container/xianyu-auto-bot)
[![SQLite](https://img.shields.io/badge/SQLite-Encrypted%20Storage-003B57.svg?style=flat-square&logo=sqlite&logoColor=white)](https://www.sqlite.org/)

[English](./README.md) · [简体中文](./README.zh-CN.md) · [核心功能](#核心功能) · [快速开始](#快速开始) · [配置说明](#配置说明) · [常见问题](#常见问题)

<br>

<p align="center">
  <b>彻底终结多账号手工盯盘回复疲惫、订单夜间漏发超时与复杂风控滑块掉线困扰。</b><br>
  严格基于异步 FastAPI、Playwright 与 Chrome DevTools Protocol (CDP) 隧道构建，支持租户数据强隔离。<br>
  提供多阶关键词与 LLM 智能融合客服、商品卡密全自动履约、定时微扰商品擦亮与物理设备指纹远程滑块协同。
</p>

</div>

---

## 项目概述

**XianyuBot** 是一款面向闲鱼平台的自动化客服与运营辅助系统，支持多租户与多账号统一调度。系统整合了智能自动回复、基于规则与商品维度的自动发货履约、定时自动擦亮、AI 大模型会话生成、真实设备指纹滑块验证（CDP 隧道 / 远程人工协同）等企业级特性。

> 本项目仅供技术研究与学习交流使用，严禁用于任何商业目的或违反平台服务协议的场景。

---

## 核心功能

| 功能模块 | 说明 |
|---|---|
| **智能回复** | 关键词精确/模糊匹配 + AI 大模型上下文回复，支持灵活的优先级分层流转 |
| **多用户系统** | 独立注册与登录机制，租户数据强隔离 |
| **多账号管理** | 单用户可挂载多个闲鱼账号，支持独立会话启停与状态监控 |
| **自动发货** | 监控成交订单，根据商品规则与模板全自动完成发货通知与卡密交付 |
| **商品擦亮** | 支持一键全量擦亮及每日可配置的定时分散擦亮任务 |
| **扫码登录** | 基于 Playwright 无头/有头浏览器自动化获取与同步登录二维码 |
| **滑块风控协同** | 真实轨迹算法生成，支持 CDP 物理设备指纹接管与远程安全人工验证兜底 |
| **容器化交付** | 提供标准 Docker Compose 编排，镜像依托 GitHub Actions 自动化构建与投递 |
| **实时状态流** | 基于 Server-Sent Events (SSE) 实时推送会话、订单、风控与审计日志 |

---

## 部署前必读

- **镜像只从 GHCR 拉取**：`docker-compose.yml` 默认锁定预编译镜像，无 `build:` 定义；禁止在 1GB 内存级别的轻量云 VPS 上本地构建镜像（`docker-deploy.sh build` 会主动拦截以防 OOM 卡死）。
- **默认凭证仅限本地测试**：正式联网环境必须在 `.env` 中显式覆写 `ADMIN_PASSWORD`、`JWT_SECRET_KEY` 以及 `SECRET_ENCRYPTION_KEY`（修改密钥会导致历史持久化 Cookie 密文失效，请在首次配置时固定）。
- **端口默认仅绑定回环网络**（`127.0.0.1:9000`）：公网访问必须通过 HTTPS 反向代理（如 Caddy、Nginx），严禁直接将 `5900`（VNC）与 `6080`（noVNC）暴露至公网。
- **敏感数据防泄漏隔离**：`data/`、`logs/`、`backups/`、`global_config.yml` 与 `.env` 均已被 Git 忽略；请勿在 Issue、PR 或截图中泄露含有 Cookie、Token、数据库或 API Key 的未脱敏日志。
- 详细密钥注入指南可参考 [`.env.example`](.env.example)。

---

## 技术栈

| 层次 | 选型组件 | 说明 |
|---|---|---|
| 服务后端 | FastAPI + Uvicorn | Python 3.11+ 高性能异步 Web 框架 |
| 异步协议 | asyncio + aiohttp + websockets | 高并发网络 I/O 与长连接维护 |
| 数据持久化 | SQLite 3 + Cryptography (Fernet) | 零外部依赖嵌入式存储，账号凭据强加密 |
| 界面展示 | Bootstrap 5 + 原生 JS + SSE | 轻量响应式控制台，实时推送仪表盘 |
| 自动化驱动 | Playwright 1.59 + Patchright | 反指纹检测的现代化浏览器驱动框架 |
| 智能大模型 | OpenAI / Claude / Gemini / Ollama / 通义千问 | 兼容标准 OpenAI 协议的多模型流式生成接口 |
| 交付运行 | Docker + Docker Compose | 规范化多架构镜像部署容器 |
| 审计追踪 | Loguru | 结构化滚动日志，自动分级与过期清理 |

---

## 快速开始

### 环境准备

- Python 3.11 或更高版本
- Chrome / Chromium 浏览器（或已配置的 Playwright 运行环境）
- Docker & Docker Compose（可选，推荐容器化方案）

### 本地部署

```bash
# 1. 克隆代码仓库
git clone https://github.com/dengyie/xianyu-bot.git
cd xianyu-bot

# 2. 创建并激活 Python 虚拟环境
python3 -m venv venv
# Linux / macOS
source venv/bin/activate
# Windows PowerShell
venv\Scripts\Activate.ps1

# 3. 安装哈希锁定的生产依赖
pip install --require-hashes -r requirements.lock
pip install --no-deps "slidex @ git+https://github.com/dengyie/slidex.git@0d41893"

# 4. 安装 Playwright 浏览器内核
playwright install chromium

# 5. 启动服务
python Start.py
```

服务就绪后，通过浏览器访问 `http://localhost:8090`。系统管理员默认账号为 `admin`；初始密码由环境变量 `ADMIN_PASSWORD` 指定，若未设置则会在首次启动控制台日志中随机生成。

### Docker 一键部署

生产镜像统一由 GitHub Actions 构建发布至 GitHub Packages (GHCR)。

```bash
# 1. 登录 GHCR 镜像仓库（推荐，确保私有包或拉取配额受信任）
export GHCR_TOKEN='ghp_...'
./docker-deploy.sh login-ghcr

# 2. 配置环境变量
cp .env.example .env
chmod 600 .env
# 编辑 .env 文件设置 ADMIN_PASSWORD, SECRET_ENCRYPTION_KEY, JWT_SECRET_KEY

# 3. 拉取镜像并启动容器
./docker-deploy.sh update

# 或直接通过 Docker Compose 原生指令启动
docker pull ghcr.io/dengyie/xianyu-auto-bot:latest
docker compose up -d --no-build
```

---

## 项目结构

```text
xianyu-bot/
├── Start.py                    # 主程序入口调度
├── XianyuAutoAsync.py          # 核心业务引擎（长连接、消息路由与发货触发）
├── reply_server.py             # FastAPI Web API 与静态资源托管
├── cookie_manager.py           # 多账号凭据调度与健康巡检
├── db_manager/                 # 领域数据库分层访问模块
├── ai_reply_engine.py          # AI 大语言模型意图推断与回复流水线
├── chat_event_hub.py           # 会话事件分发中心
├── order_event_hub.py          # 订单状态监听与事件总线
├── order_status_handler.py     # 订单状态机流转控制
├── config.py                   # 环境变量解析与运行时配置
├── global_config.yml           # 全局业务参数配置模板
├── utils/                      # 工具集（滑块风控、QR 编解码、指标采集等）
│   ├── qr_login.py             # 扫码登录会话管理
│   ├── xianyu_slider_stealth.py# 拟人滑块轨迹与反指纹混淆
│   └── image_uploader.py       # 图片资产上传封装
├── static/                     # Web 前端单页应用（SPA）控制台
├── tests/                      # 单元测试与烟雾自动化测试套件
├── Dockerfile                  # 容器化镜像构建定义
└── docker-compose.yml          # Docker Compose 生产编排文件
```

---

## 配置说明

### 全局业务配置 (`global_config.yml`)

- `WEBSOCKET_URL`: 闲鱼实时长连接网关地址
- `AUTO_REPLY`: 自动回复全局开关与兜底文案配置
- `RISK_CONTROL`: 平台风控应对策略（夜间静默模式、请求指数退避）
- `SLIDER_VERIFICATION`: 验证码处理并发限制与单任务超时阈值
- `TOKEN_REFRESH_INTERVAL`: 账号身份令牌后台保活与刷新频次

### 核心生产环境变量

| 变量名称 | 约束要求 | 职能定义 |
|---|---|---|
| `ADMIN_PASSWORD` | 强烈建议显式设定 | 系统超级管理员初始化密码；未提供时会在初次启动日志输出随机串 |
| `XIANYU_REPLY_API_KEY` | 使用回调接口时必需 | 保护 `/xianyu/reply`，供内部通过 `X-Internal-API-Key` 鉴权调用 |
| `CAPTCHA_CONTROL_API_KEY` | 使用远程协同验证时必需 | 保护 `/api/captcha` 控制端点；人工协同支持临时会话 Token |
| `CAPTCHA_PUBLIC_BASE_URL` | 建议设定 | 人工验证面板外部公开 URL（如 `https://captcha.yourdomain.com`） |
| `SEND_MESSAGE_API_KEY` | 使用主动发信接口时必需 | 保护外部调用主动消息推送 `/send-message` 端点 |
| `SECRET_ENCRYPTION_KEY` | 必须保持固定 | 对称加密持久化储存的 Cookie 与数据库敏感字段 |

---

## 风控与滑块验证

### 真实设备 CDP 模式

针对高强度设备风控场景，支持将滑块验证流转至宿主机真实物理 Chrome 浏览器：

1. 宿主机 Chrome 开启远程调试：
   ```bash
   chrome.exe --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\xianyu-cdp" --no-first-run
   ```
2. 运行内置反向隧道守护脚本（`scripts/cdp_tunnel.ps1`）连通容器。
3. 容器检测到风控挑战时，通过 CDP 驱动真实硬件指纹完成拖拽；若未通过则无缝降级转交人工面板。

### Cookie 导入与远程人工协同

- 支持从 Web 管理端直接导入完整会话 Cookie。
- 自动滑块通过时强制核验 `x5sec` 签名有效性，未取得有效凭证则不判定为成功。
- 当自动重试达到风控阈值时，系统生成带临时 Token 的轻量交互页面，管理员或运维人员可随时通过手机/桌面端远程完成滑动确认。

---

## 常见问题

**Q: WebSocket 提示连接中断或频繁断开？**  
请检查服务器的出站防火墙策略与网络代理质量，并确认对应闲鱼账号的会话 Cookie 未在手机端被顶下线。

**Q: Docker 启动提示 `exec /app/entrypoint.sh: no such file`？**  
Windows 平台拉取代码可能因 Git CRLF 转换导致换行符异常，或者本地未同步完整 submodules。推荐直接使用 `docker pull ghcr.io/dengyie/xianyu-auto-bot:latest` 预构建镜像运行。

**Q: 如何更新依赖锁文件？**  
本项目使用 uv 统一管理精确哈希锁：
```bash
uv pip compile requirements.txt \
  --python-version 3.11 \
  --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes \
  --no-emit-package slidex \
  --output-file requirements.lock
```

---

## 致谢

本项目基于开源社区生态二次开发，特别鸣谢以下项目的灵感与实践参考：

- [myfish](https://github.com/Kaguya233qwq/myfish) — 二维码会话登录机制
- [XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent) — 自动化消息状态机设计

---

## 开源许可证与免责声明

本项目基于 [MIT License](LICENSE) 协议发布。

**免责声明**：本项目按“现状”（AS-IS）提供，仅限用于个人学术与技术测试。严禁将本项目用于任何违反相关法律法规、侵犯第三方合法权益或违背电商平台服务协议的商业化运营活动。因使用本系统所导致的账号限制、业务损失或法律纠纷，均由使用者自行承担，与本项目贡献者无关。
