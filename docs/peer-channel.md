# 跨 session 对讲通道（peer channel）

> **何时读**：要给另一个 session 发消息（手机上的线程 ↔ 桌面的 session）。
> **真源**：`config/peers.json`（角色 = 仓 + 角色 + 入口类型）。
> **实现**：`tools/peer_resolve.py` / `peer_mirror.py` / `peer_health.py`。

## 为什么不是「记住对方的名字」

session 名会变：自动生成的（首条消息当名字）、或 `/rename` 改的；pid 会复用；thread id
也会随线程重建变。所以**名字不能当标识**。稳定身份是**角色**：

| 角色 | 判据 | 说明 |
|---|---|---|
| `desktop` | cwd 含你的仓名 + entrypoint `cli` | 桌面 Claude Code session |
| `mobile` | cwd 含你的仓名 + entrypoint `t3-code` | T3 线程（手机 / 网页 / 桌面 App） |

同仓不同入口时，**入口类型就是区分器**。名字只是**发送那一刻的把手**：解析时从调用方自己的
registry 里取当前名字。

## 怎么用

```bash
# 1. 解析：拿到当前该发给谁（打印出来的名字直接喂给 SendMessage）
python tools/peer_resolve.py --role mobile --no-mirror

# 2. 用你的 SendMessage 工具发给那个名字
```

`--role all --json` 列出全部活着的 peer。退出码：0 找到 / 1 该角色当前没活着的 session /
2 配置或角色名有问题。

**只读场景一律加 `--no-mirror`**：默认会跑 `peer_mirror` 互抄 registry（有写副作用）；
`--no-mirror` 跳过互抄、不写任何文件，代价是可能看到尚未同步的旧条目。
起因是 agent 在只读轮次里不敢跑解析（它会改 harness 配置目录）→ 「rename 之后费力找名字」
这一步卡住。

解析**自己**的名字用 `--pid "$CLAUDE_PID"`：跨仓时角色匹配不上，同角色多 session 时会挑错人。

## 底层是怎么通的

Claude Code 的 peer 注册表在 `CLAUDE_CONFIG_DIR` 下：

- 桌面 CLI session → `~/.claude/sessions/<pid>.json`
- 手机 T3 线程（隔离配置）→ `<隔离配置目录>/sessions/<pid>.json`

**推荐做法：让这两个 `sessions` 目录是同一个目录**（Windows 目录联接）：

```powershell
New-Item -ItemType Junction -Path "<隔离配置目录>\sessions" -Target "$env:USERPROFILE\.claude\sessions"
```

两边 harness 写进同一份注册表，于是各自原生就能发现对方，不再需要抄条目。
回滚：删掉 junction，把备份目录移回原位。

`tools/peer_mirror.py` 保留为兜底：两个目录不再共享时（junction 被删、出现第三个配置目录），
它还能互抄条目。共享状态下它自动跳过（同一文件不重复写）。

条目里带 `messagingSocketPath`（Windows 命名管道，机器全局），这是跨配置目录能送达的原因，
**不用改 provider 配置**。

## 生命周期与坑（都实测过）

1. **对面 harness 会定期重写自己的 registry**，删除后重写，中间有百毫秒空窗。抄来的条目
   只短期有效 → 规矩：**发送前先跑一次 `peer_resolve.py`**（它内部会先镜像）。
2. **registry 文件可能读不到**（重写窗口）。`peer_resolve.py` 内部带重试；手写脚本读它
   要自己重试。
3. **pid 会复用**。校验用 `procStart`（进程创建时间 FILETIME）对齐，防「同一个 pid 换了人」。
4. **不要用 `os.kill(pid, 0)` 判活**：Windows 上那会 `TerminateProcess`，直接把对方杀掉。
   用 `OpenProcess` + `GetExitCodeProcess`（`STILL_ACTIVE`）。
5. **不要把 peer 的 token / key 复制进仓库或外发**。条目只在两个 config 目录之间互抄。

## 双方各要做的一件事

- 桌面侧：`.claude/settings.json` 的 SessionStart 与 UserPromptSubmit 钩子各跑一次
  `python tools/peer_mirror.py`（共享状态下是空操作，junction 被拆时是自愈）。
- 线程侧：同样的 SessionStart 钩子，跑同一个脚本（只读运行，允许写自己的 config 目录）。

## 健康检查

`python tools/peer_health.py` — 只读探针，判**机制**健不健，不判谁在线：
`ok` / `offline`（没人在线，正常）/ `degraded`（注册表有陈旧条目）/ `error`（机制坏）。
适合挂夜巡：**只有机制坏才让巡检红**。

消息链路本身（`SendMessage` 能不能送达）不在这个探针里 —— 脚本没有 `SendMessage` 能力，
那一步只能由 agent 真发一条 ping 来验。
