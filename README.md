# dsh-companion

把 [astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion) 的陪伴内核接进 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)。

原插件是 AstrBot 的 Python 插件（入口 `main.py` 直接 `import astrbot.api`），装不进 DSH。这个项目把它 **host-free 的内核** `companion_v2` 原样接过来，用 DSH 自己的端口替换掉 AstrBot 那一层，**不改内核一行代码**。

## 它是怎么接的

```
DSH(JS)  ──NDJSON 管道──  bridge.py  ──  companion_v2（原样）
   │                          │
   └──── 模型反向请求 ◄───────┘   用 DSH 已配置的模型，不需要第二把 key
```

| 目录 / 文件 | 作用 |
|---|---|
| `companion_v2/` | 上游内核，704 个文件、约 14.4 万行，未改动 |
| `dsh_host/` | DSH 侧端口：事件端口、身份端口、模型 provider、投递 sink |
| `bridge.py` | 常驻 Python 进程，一行一个 JSON，双向收发 |
| `lib/index.js` | DSH 插件本体，注册工具与命令 |

只有上游 `adapters/astrbot/` 里那两个真的 `import astrbot` 的模块（`platform_sender.py`、`quotes.py`）被移除；内核其余部分原样保留，也没有任何代码引用它们。

## 安装

```sh
# 1. 依赖
python -m pip install -r requirements.txt

# 2. 挂进 DSH profile（package.json 的 dsh.profile.bundles 里加一项）
#    "@local/dsh-companion": "link:<本仓库路径>"
```

重启 DSH 生效。

## 用法

工具 `companion_chat`：

| 参数 | 说明 |
|---|---|
| `text` | 你要说的话 |
| `conversation` | 会话标识，默认 `main`；不同值 = 不同的人设 / 记忆线 |
| `persona` | 本轮角色设定（人格提示词），留空沿用上次 |

工具 `companion_status`：看运行状态与最近的对话批次。

命令：`/companion-ping` 查桥是否在线，`/companion-reset` 重置 `main` 会话。

数据落在 `~/.dsh-companion/`（SQLite，记忆 / 状态 / 日程都在里面），可用环境变量 `DSH_COMPANION_DATA` 改。

## Python 选择顺序

1. `DSH_COMPANION_PYTHON` 环境变量
2. `.venv/Scripts/python.exe`（本仓库内）
3. DSH 自带运行时（`DSH_RUNTIME` / `DSH_HOME` / Electron `resourcesPath` 下的 `primary-runtime`）
4. PATH 里的 `python`

## 自检

```sh
node smoke.mjs         # 假装是 DSH 宿主，跑完整一轮对话（模型用假实现）
python e2e_bridge.py   # 直接测桥，校验模型原文能落到回复
python e2e_close.py    # 测优雅退出（exit 0 + 无 traceback）
```

## 换掉的东西

| 原 AstrBot 有 | 这里换成 |
|---|---|
| `AstrBotEventPort`（平台事件） | `dsh_host/ports.py`：DSH 会话作用域 |
| AstrBot 的 provider（OpenAI / Gemini 适配器） | `dsh_host/provider.py`：反向问 DSH 的 `llm` 服务 |
| `AstrBotPlatformSender`（平台发送） | 投递 sink：回给 DSH 工具调用方 |
| AstrBot 的 `Context` / `Star` 生命周期 | `bridge.py` 进程 + `lib/index.js` 的 `apply()` |

## 没有搬过来的

QQ / 微信群收发、QQ 空间、识屏、摄像头、米家设备、Laya 决策——这些要平台 SDK 或本地设备，DSH 侧没有对应能力。内核里这些模块还在，但要等有人给它们写 DSH 端口。

## 许可

`companion_v2/` 是 [menglimi/astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion) 的原始代码，版权归原作者，上游仓库未声明开源许可证——使用、分发或商用前请先取得原作者授权。

本仓库其余部分（`dsh_host/`、`bridge.py`、`lib/`、自检脚本）由本项目作者提供，同样受上述约束：**在内核版权未明确之前，本仓库整体不构成任何形式的授权**。

如果你只是自己本地用，请自行确认符合上游要求。
