---
name: t3-code-ops
description: T3 Code 在本机的运维与排障——起停、provider/模型配置、手机接入、worktree 线程、以及全部已踩过的坑。触发词：T3 / T3 Code / 手机 remote / 远程控制 agent / tailscale serve / 选模型 403 / git action failed / this T3 home is unavailable。
---

# T3 Code 本机运维

T3 是**控制台**，不是 harness。它把本机已装的 Claude Code / Codex 等 harness 统一到一个界面，
手机 / 浏览器 / 桌面都能操作。LLM 和 key 仍然是你自己的。

> 本文件是**机器侧事实源**：改动后请同步本节。

## 0. Canonical 状态（按你的机器填）

| 项 | 值 |
|---|---|
| 安装 | npm 全局 `t3`；二进制 `~/.t3/runtime/versions/<ver>/t3.exe` |
| 自启 | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\T3 Code Server.vbs` → 隐藏运行 `t3.exe serve --no-browser --port 3773` |
| 监听 | `127.0.0.1:3773`（仅回环，靠 serve / 隧道转发出去） |
| 远端 | `tailscale serve --bg --https=443 http://127.0.0.1:3773` → `https://<your-host>`；或 T3 Connect（cloudflared 常驻，手机推送依赖它） |
| 数据 | `~/.t3/userdata/`：`statev2.sqlite`、`logs/`、`secrets/`、`model-manifest.json`、`settings.json` |
| Provider | Claude（v2.1.325 → litellm）；其他自建网关按需 |
| provider 实例 id | `claudeAgent`；UI 建实例时自动生成的 `claudeAgent_2` 之类是**废 id** |
| 干活模型 | 以 `~/.t3/userdata/model-manifest.json` 里真实存在的 slug 为准 |
| 健康检查 | **已设 0（关闭）**——见 §3：它会把 manifest 的本地改动冲掉 |

**项目注册与默认值**（真源 = `~/.t3/userdata/settings.json` → `projectSettingsOverrides`）：
每个项目可设默认 workspace（`New worktree` / `Current checkout`）与默认权限档。
生产数据仓别用隔离 worktree（会丢 `.env`/依赖），用「不隔离目录 + 权限闸」控风险。

⚠ **改 `settings.json` 必须在停服状态下改**（server 内存副本会覆盖磁盘）：先停进程、备份、
改完再起。设置位置：`Settings → Projects → <项目>` 改 workspace；`Settings → Threads →
Default permissions` 改权限档。

**分工与交接**：`docs/dual-track.md`（哪类任务开 T3、交接协议、0–3 轮回问规则）；
执行 skill `skills/session-handoff-t3/SKILL.md`。

## 1. 每天会用的命令

```bash
t3 status                 # 服务 / Connect / LAN 三态
t3 auth session list      # 手机等客户端 session 及 expires
t3 thread list --project "<仓路径>"
t3 thread show <thread-id> --messages 20
t3 thread send <thread-id> "..."      # 往线程里追加消息
t3 pair --tailscale --ttl 2h --label <名>   # 生成手机配对链接
t3 project add "<路径>" --title "<名>"      # T3 不扫硬盘，每个仓要显式加
```

**模型名不能用猜的**：`t3 thread start ... --provider claudeAgent --model <slug>`，
slug 必须来自本地 `model-manifest.json`。

## 2. 健康检查三件套

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3773/
curl -s -o /dev/null -w "%{http_code}\n" https://<your-host>/
powershell -NoProfile -Command "if (Get-Process cloudflared -ErrorAction SilentlyContinue) {'connect ok'}"
powershell -NoProfile -Command "tailscale serve status"
```

## 3. 排障索引（症状 → 根因 → 修法）

| 症状 | 根因 | 修法 |
|---|---|---|
| `t3: fetch failed` | npm stub 用 Node 原生 fetch，不认 https_proxy | `NODE_USE_ENV_PROXY=1 https_proxy=http://127.0.0.1:<proxy> t3 ...` |
| `BootServiceUnsupportedError` | **Windows 不支持 `t3 service install`**（上游 docs 写支持是错的） | 用 VBS 自启，见 §0 |
| VBS 报「未结束的字符串常量」 | cscript 按 ANSI 读脚本，中文 UTF-8 被截断 | 别用 VBS 装中文；改 Python 脚本 + `pythonw.exe` |
| picker 选模型 → 403 | T3 把官方 slug 原样发给 `ANTHROPIC_BASE_URL`，自建网关不认 | 改 `model-manifest.json` 注入网关真实模型名，见 §4 |
| picker 里自建模型消失 | `model-manifest.json` 被上游 manifest 覆盖 | `settings.json` → `enableProviderUpdateChecks: false`。**`Health check interval = 0` 只管 provider 健康探针，管不了 manifest 的 TTL 重拉**。代价：上游新模型不再自动出现，需要时手动加 |
| `--provider <id>` 报 "disabled for project" | 用了建实例时 UI 自动建议的 id，实际保存的 id 不是它 | 从 `statev2.sqlite` 的 `orchestration_v2_projection_provider_sessions.provider_instance_id` 取真实值 |
| `this T3 home is unavailable` | 桌面 exe 与 CLI server 抢 `~/.t3` | 卸载桌面 exe + 清 `HKCU\Software\Classes\t3code` |
| worktree 建失败 `git could not reach the remote` | server 由 VBS 拉起，无代理环境变量 | `git config --global http.https://github.com.proxy <proxy>` |
| 配对 `credential is invalid` | 用了 `t3code://` 深链，被 Windows 桌面 app 截走并消费掉一次性 token | 改用 `https://<host>/pair#token=` 手动粘贴 |
| thread 改名（手机 UI） | CC 层 `/rename` 与 T3 thread 标题不互通 | T3 app: thread 列表长按 → More → Rename；或让 agent 调 `t3_thread_update` |
| `t3 up --connect` 后没 cloudflared | Windows 无后台服务，前台进程一退隧道就没了 | 重启 server |
| 重启 server 后 cloudflared 没回来 | 隧道客户端随 server 进程死，**约 15–20 秒后**由新 server 进程拉起 | 等 20 秒再看进程；日志出现 `Registered tunnel connection` 才算通 |
| 停服改 settings.json 后配置丢了 | server 运行中会用自己的内存副本覆盖磁盘 | 改前停进程、备份 `settings.json.bak-*`、改完重启 |
| T3 内 agent 自称"手机 remote session" | SessionStart hook 只看 `CLAUDE_CODE_ENTRYPOINT == 't3-code'`（=**谁启动了这个进程**），不是"当前用哪个客户端说话" | entrypoint ≠ 客户端 |
| 删 worktree 报 `Permission denied` | T3 为该线程拉起的 harness 进程占着句柄 | 先停掉那个进程，确认父进程是 `t3.exe`（**不是自己的 session**）再删 |
| `html_preview` / `html_render` 报错装不上浏览器 | 首次调用要下 `chrome-headless-shell`（127MB），受限网络下不来 | 改用本机已有 Chromium 截图；Windows 有最小窗口宽（~511px），查横向溢出看 `scrollWidth` vs `clientWidth`，别看截图 |
| 手机连不上 | session 过期（30 天）/ 本机未登录 Windows / server 没起 | `t3 auth session list` → 看 expires |

**日志位置**（排障首选，App 只给一句话的错误，日志给根因）：

```
~/.t3/userdata/logs/server.trace.ndjson    # 主日志，按 traceId 串起来
~/.t3/userdata/logs/relay-client.ndjson    # Connect 隧道
```

## 4. 让 picker 认自建网关的模型

网关不可改时，改**本地清单**：

1. 备份 `~/.t3/userdata/model-manifest.json`
2. `manifest.providers.<providerId>.models[]` 追加：
   ```json
   {"slug":"<网关真实模型名>","name":"<显示名>","aliases":[],
    "status":"current","profile":"sonnet-5-5"}
   ```
3. 停服状态下改 `settings.json`（加 `enableProviderUpdateChecks: false`），重启。

## 5. 手机接入

1. `t3 pair --tailscale --ttl 2h --label <名>` 拿到配对链接。
2. 手机 App 里手动粘贴 `https://<host>/pair#token=<token>`（**别用 `t3code://` 深链**）。
3. 远端形态二选一：T3 Connect（推送优先）或 Tailscale serve（纯流量）。

## 6. 不要做的事

- 不要把 `CLAUDE_CONFIG_DIR` 指到别处来"共享"——只共享 `sessions/` 注册表（见
  `docs/peer-channel.md`）。
- 不要用 `approval-required` 开给手机的线程：手机端批不了，线程会卡死。
- 不要在 server 运行时改 `settings.json`。
- 不要把 `~/.t3/userdata/secrets/` 或任何 token 提交进仓库。

## 7. 线程 / worktree 心智模型

- 一个 thread = 一个 worktree（默认）+ 一个 harness 进程。
- thread 里的文件来自 **HEAD**，不是你的工作区 → 未提交内容要么不共享，要么先 commit。
- worktree 删不掉 = 有进程占着句柄（见 §3）。

## 8. 跨 session 对讲（手机线程 ↔ 桌面 session）

见 `skills/peer-session-comms/SKILL.md` 与 `docs/peer-channel.md`。
