<!-- markdownlint-disable-file MD033 MD045 -->
<div align="center">

# XianyuBot

**Automated Customer Service, Auto-Fulfillment & Intelligent Operations Assistant for Xianyu**

[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-1.59+-2EAD33.svg?style=flat-square&logo=playwright&logoColor=white)](https://playwright.dev/)
[![Docker](https://img.shields.io/badge/Docker-GHCR%20Image-2496ED.svg?style=flat-square&logo=docker&logoColor=white)](https://github.com/dengyie/xianyu-bot/pkgs/container/xianyu-auto-bot)
[![SQLite](https://img.shields.io/badge/SQLite-Encrypted%20Storage-003B57.svg?style=flat-square&logo=sqlite&logoColor=white)](https://www.sqlite.org/)

[English](./README.md) · [简体中文](./README.zh-CN.md) · [Core Features](#core-features) · [Quick Start](#quick-start) · [Configuration](#configuration-guide) · [FAQ](#frequently-asked-questions)

<br>

<p align="center">
  <b>Terminates manual multi-account customer support fatigue, missed delivery deadlines, and slider verification dropouts on Xianyu.</b><br>
  Built on asynchronous FastAPI, Playwright, and Chrome DevTools Protocol (CDP) tunneling with isolated multi-tenant data partitioning.<br>
  Provides multi-tier LLM keyword auto-reply, instant order fulfillment with card/key vaults, automated jittered item polishing, and remote slider assistance.
</p>

</div>

---

## Overview

**XianyuBot** is an asynchronous, multi-tenant customer service automation, auto-delivery, and account operations engine tailored for Xianyu (Idle Fish). Built on FastAPI, asyncio, and Playwright, it coordinates intelligent keyword/LLM message processing, SKU-bound digital asset delivery, periodic item polishing, physical device fingerprint emulation via Chrome DevTools Protocol (CDP), and remote human verification fallback.

> **Notice**: This project is intended strictly for academic research and educational exploration. Commercial use and activities violating platform terms of service are strictly prohibited.

---

## Core Features

| Module | Description |
|---|---|
| **Intelligent Auto-Reply** | Exact and fuzzy keyword matching combined with contextual LLM inference across configurable priority tiers |
| **Multi-Tenant System** | Isolated user registration, authentication, and role-based data partitioning |
| **Multi-Account Operations** | Manages multiple Xianyu accounts per tenant with independent session lifecycles and background health checks |
| **Automated Delivery** | Listens for completed transactions, mapping orders to automated delivery templates and card/key fulfillment |
| **Periodic Item Polishing** | Bulk on-demand polishing and jittered cron schedules to keep item listings fresh |
| **Automated QR Login** | Headless and headed QR session acquisition powered by Playwright |
| **Risk Control & Slider Solving** | Realistic mouse trajectories, physical host CDP tunneling, and remote human interactive fallback |
| **Containerized Delivery** | Production-ready Docker Compose deployment backed by automated GitHub Actions CI/CD pipelines |
| **Real-Time Telemetry** | Server-Sent Events (SSE) streaming live order events, incoming inquiries, and audit events |

---

## Pre-Flight Deployment Requirements

- **Pull Pre-Built Images from GHCR**: `docker-compose.yml` points directly to GitHub Packages. Do not run `docker compose build` on low-memory VPS instances (1 GB RAM environments will trigger OOM panics).
- **Override Default Credentials**: For Internet-facing setups, you must override `ADMIN_PASSWORD`, `JWT_SECRET_KEY`, and `SECRET_ENCRYPTION_KEY` in `.env`. Changing encryption keys invalidates existing stored session cookies, so persist them before launching.
- **Bind Loopback Only**: The application defaults to `127.0.0.1:9000`. Expose Web interfaces through a secure reverse proxy (Caddy / Nginx) with HTTPS. Never expose ports `5900` (VNC) or `6080` (noVNC) directly to the public Internet.
- **Sensitive Path Isolation**: Directories `data/`, `logs/`, `backups/`, and configuration files `.env` and `global_config.yml` contain encrypted cookies, tokens, and credentials. Ensure they remain excluded from version control and logs are sanitized before reporting issues.
- Refer to [`.env.example`](.env.example) for variable definitions.

---

## Tech Stack

| Layer | Component | Notes |
|---|---|---|
| Web Service | FastAPI + Uvicorn | Python 3.11+ high-throughput async framework |
| Concurrency | asyncio + aiohttp + websockets | Non-blocking socket polling and persistent gateway sessions |
| Storage | SQLite 3 + Fernet (Cryptography) | Embedded zero-dependency database with symmetric secret encryption |
| Interface | Bootstrap 5 + Vanilla JS + SSE | Single-page application console with live event streams |
| Browser Automation | Playwright 1.59 + Patchright | Undetected CDP and automation driver |
| Model Integrations | OpenAI / Claude / Gemini / Ollama / Qwen | OpenAI-compatible streaming dialogue generation |
| Infrastructure | Docker + Docker Compose | Containerized cross-platform deployment |
| Logging | Loguru | Structured, rotating log sinks with retention policies |

---

## Quick Start

### Prerequisites

- Python 3.11 or later
- Chrome / Chromium browser
- Docker & Docker Compose (optional, recommended for production)

### Local Development Setup

```bash
# 1. Clone repository
git clone https://github.com/dengyie/xianyu-bot.git
cd xianyu-bot

# 2. Initialize and activate virtual environment
python3 -m venv venv
# Linux / macOS
source venv/bin/activate
# Windows PowerShell
venv\Scripts\Activate.ps1

# 3. Install hash-pinned dependencies
pip install --require-hashes -r requirements.lock
pip install --no-deps "slidex @ git+https://github.com/dengyie/slidex.git@0d41893"

# 4. Install Playwright browser binaries
playwright install chromium

# 5. Start service
python Start.py
```

Access the dashboard at `http://localhost:8090`. The default administrator username is `admin`. The initial password is read from `ADMIN_PASSWORD` or generated randomly in console output during first boot.

### Docker Compose Deployment

Production images are packaged and published to GitHub Packages (GHCR):

```bash
# 1. Authorize GHCR access (recommended for consistent rate limits)
export GHCR_TOKEN='ghp_...'
./docker-deploy.sh login-ghcr

# 2. Configure environment parameters
cp .env.example .env
chmod 600 .env
# Set ADMIN_PASSWORD, SECRET_ENCRYPTION_KEY, JWT_SECRET_KEY in .env

# 3. Pull latest image and launch
./docker-deploy.sh update

# Or via standard Docker Compose command
docker pull ghcr.io/dengyie/xianyu-auto-bot:latest
docker compose up -d --no-build
```

---

## Project Structure

```text
xianyu-bot/
├── Start.py                    # Application entry point
├── XianyuAutoAsync.py          # Core async message engine and connection coordinator
├── reply_server.py             # FastAPI REST endpoints and static file serving
├── cookie_manager.py           # Multi-account credential manager and health monitor
├── db_manager/                 # Layered SQLite schema and data access objects
├── ai_reply_engine.py          # LLM inference pipeline and prompt orchestrator
├── chat_event_hub.py           # Message bus for real-time customer conversations
├── order_event_hub.py          # Transaction event bus and fulfillment triggers
├── order_status_handler.py     # Finite state machine for order lifecycle management
├── config.py                   # Configuration loader and environment bindings
├── global_config.yml           # Default operational parameters
├── utils/                      # Utilities (stealth sliders, QR helpers, telemetry)
│   ├── qr_login.py             # QR session lifecycle handler
│   ├── xianyu_slider_stealth.py# Humanized slider solver and CDP hooks
│   └── image_uploader.py       # Image asset uploader
├── static/                     # Web dashboard frontend assets
├── tests/                      # Unit and integration test suites
├── Dockerfile                  # Production container definition
└── docker-compose.yml          # Docker Compose production manifest
```

---

## Configuration Guide

### Global Settings (`global_config.yml`)

- `WEBSOCKET_URL`: Xianyu gateway WebSocket endpoint.
- `AUTO_REPLY`: Global auto-reply toggles, rules, and default templates.
- `RISK_CONTROL`: Anti-ban intervals, quiet night mode, and backoff parameters.
- `SLIDER_VERIFICATION`: Concurrency bounds and maximum solver timeouts.
- `TOKEN_REFRESH_INTERVAL`: Frequency of proactive session token updates.

### Production Environment Variables

| Variable | Requirement | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | Recommended | Initial administrative password |
| `XIANYU_REPLY_API_KEY` | Required for callbacks | Secures `/xianyu/reply` using `X-Internal-API-Key` |
| `CAPTCHA_CONTROL_API_KEY` | Required for captcha API | Protects `/api/captcha` endpoints |
| `CAPTCHA_PUBLIC_BASE_URL` | Recommended | Public URL for human slider fallback (e.g., `https://captcha.example.com`) |
| `SEND_MESSAGE_API_KEY` | Required for messaging API | Secures external proactive messaging via `/send-message` |
| `SECRET_ENCRYPTION_KEY` | Required in production | Symmetric key used to encrypt stored cookies and credentials |

---

## Risk Control & Captcha Handling

### Physical Host CDP Mode

When accounts face elevated device-level verification challenges, the bot can delegate slider handling to Google Chrome running on a physical host workstation:

1. Launch Chrome with remote debugging enabled:
   ```bash
   chrome.exe --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\xianyu-cdp" --no-first-run
   ```
2. Run the reverse tunnel daemon (`scripts/cdp_tunnel.ps1`) to bridge the workstation and container.
3. XianyuBot detects the challenge and drives the real device fingerprint via CDP, harvesting valid `x5sec` tokens automatically or allowing manual intervention.

### Remote Human Verification Panel

- When automatic solving fails or risk scores exceed thresholds, the server provisions a temporary interactive link: `/api/captcha/control/{session_id}?token=...`.
- Administrators can open the link in any mobile or desktop browser to slide the verification bar manually without exposing master server keys.
- Completion requires cryptographic validation of the resulting `x5sec` token before the account session is resumed.

---

## Frequently Asked Questions

**Q: WebSocket disconnects frequently or fails to establish?**  
Verify server outbound firewall settings, proxy stability, and ensure the account's mobile session has not been superseded.

**Q: Docker container exits with `exec /app/entrypoint.sh: no such file`?**  
This typically indicates Windows CRLF line endings in shell scripts or an incomplete repository clone. Use the official pre-built image: `docker pull ghcr.io/dengyie/xianyu-auto-bot:latest`.

**Q: How do I update dependencies?**  
Update requirements using `uv` to generate cryptographic hash verification:
```bash
uv pip compile requirements.txt \
  --python-version 3.11 \
  --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes \
  --no-emit-package slidex \
  --output-file requirements.lock
```

---

## Acknowledgments

This project builds upon the shared knowledge of the open-source community:

- [myfish](https://github.com/Kaguya233qwq/myfish) — QR session login architecture
- [XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent) — Automation state machine reference

---

## License & Disclaimer

Released under the [MIT License](LICENSE).

**Disclaimer**: This project is provided "as is" without warranty of any kind. It is intended solely for research and personal automation tasks. Users are solely responsible for ensuring compliance with applicable laws and platform regulations.
