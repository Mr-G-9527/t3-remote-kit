# t3-remote-kit

> 让一台 T3 Code 线程和一台本机 Claude Code session **真的能对话、真的能交班**。

**English abstract** — T3 Code (pingdotgg/t3code, MIT) is a *control plane* for AI agents: one
phone app / web UI in front of many local agent harnesses. Using it bare leaves four practical
gaps: peer addressing (session names drift, pids get reused), cross-directory delivery (two
harness config dirs cannot see each other), state handoff when you switch surfaces, and a
mobile agent that does not know it is on a phone. This repo is the glue layer: role-based
addressing, a registry bridge, a handoff protocol, a mobile session hook, and a health probe.
Dependency-free, Windows-first, MIT.

---

## 0. 先花 30 秒理解 T3 Code

T3 Code **不是** AI 模型，也不是 harness（Claude Code、Codex 这类真正干活的程序）。
它是一层**控制面**——像车间调度台加上遥控器：

```
     手机 App / 网页 / 桌面 App          ← 你在哪都能看、能点、能发话
              │
       t3 server（本机 127.0.0.1:3773）  ← 线程、审批、产物、PR 事件
              │
    ┌─────────┼─────────┐
 Claude Code  Codex  …                    ← 每个 T3 线程里跑一个真正的 harness
 （真正读代码、改文件、跑命令的那个）
```

所以「在手机上开一个线程」= 你在手机上遥控一个跑在你电脑里的 Claude Code。
好处是**线程是持久的**：本地 session 进程一死就消失，T3 线程还在。
坏处是——**遥控器有了，频道可能对不上**：这条线程里那个 agent，和你本地终端里那个 agent，
互相看不见、叫不到名字，遥控器上的进度也不会自己变成对方能接手的状态。

**这套东西就是补那几个缺口。**

---

## 1. 它解决什么问题

| # | 痛点（裸用 T3 时） | 为什么会这样 | 本套怎么解 |
|---|---|---|---|
| P1 | **叫不到人**：你想让手机线程里的 agent 去问本地 agent 一个实现细节，找不到地址 | session 名不是标识——首条消息自动命名、`/rename` 会改、archive 就没了；pid 会复用 | **身份 = 角色**：角色表 `config/peers.json` 记「仓 + 入口类型」，`peer_resolve.py` 现解析出当前名字（名字只是发送那一刻的把手） |
| P2 | **送不到**：手机线程用 `~/.claude_minimax`，本地用 `~/.claude`，两个 harness 的 peer 注册表互不可见 | Claude Code 的 peer 注册表在 `CLAUDE_CONFIG_DIR` 下，各写各的 | **共享注册表**（Windows junction，把两台「对讲机」插到同一频道）或 **互抄兜底**（`peer_mirror.py`）。底层是机器全局的命名管道，**不用改 provider 配置** |
| P3 | **交班丢状态**：你下班前想把手上的活挪到手机上继续 | 换面 = 新建线程 + 交**状态**（T3 接不了已在跑的进程）；而线程默认 worktree，主工作区未提交的文件它读不到 → **交接静默降级**成「按缺前提开工」 | **五块交班条**（目标/已知事实带证据/已做未做/下一步第一条命令/预判疑问+默认答案）+ **可见性三选一**（默认不加 `--worktree` / 先 commit / 读不到就内联进首条消息）+ **0–3 轮回问**协议 |
| P4 | **手机上的 agent 不知道自己在手机上**：它按桌面节奏写长回复、跳过拍板、直接改共享文件 | 手机上没法滚屏，没法随时盯着 | **SessionStart hook**：命中 `CLAUDE_CODE_ENTRYPOINT=t3-code` 就内联手机纪律（一屏内 / 拍板单 / 软拒旋转门 / 动共享文件前看 mtime）。规则不赌召回——**内联文本**，不只给一个 memory 名 |
| P5 | **机制坏了没人知道**：某天通道不通了，两边各自猜 | 没有探针时，「叫不到人」和「机制坏了」长得很像 | **`peer_health.py`**：判**机制**健不健（`ok` / `offline` 正常 / `degraded` 陈旧条目 / `error` 机制坏），可挂夜巡；只有机制坏才让监控红 |

---

## 2. 典型使用场景

1. **通勤路上盯一个长跑任务**
   电脑上开线程跑迁移/构建/调研，出门后手机 App 接着看进度、批一把决策。中途你想问本地
   session 一个实现细节 → 手机线程用它自己的 peer 通道问到人（**P1+P2**）。

2. **两台机器之间转交**
   公司电脑上的活儿，回家在笔记本上继续：交班条带着「已知事实 + 证据 + 下一步第一条命令」，
   接手方 0–3 轮内续上，不用你复述三遍（**P3**）。

3. **一次任务拆成多个并行线程**
   每线程一个 worktree，天然隔离；T3 负责线程与产物面板，这套负责「谁在哪个线程、
   名字叫什么、怎么互发消息」（**P1+P2**）。

4. **把活交给另一个 agent / 另一个仓的同事**
   交接协议的产物是文件，不是聊天记录：谁接手、知道什么、下一步做什么，都落盘可查（**P3**）。

5. **远程审批，但要有纪律**
   线程默认 `full-access`（手机端批不了 `approval-required`，会卡死）；只读要求必须写进
   **消息文本**，不能指望权限档兜底——权限档的作用是让它能跑完，不是保证它守规矩（**P4**）。

---

## 3. 比裸用 T3 多了什么

| | 裸用 T3 Code | + 本套 |
|---|---|---|
| 线程可见性 | ✅ 有 | ✅ |
| 产物 / diff / PR 面板 | ✅ 有 | ✅ |
| 手机审批 | ✅ 有（但 `approval-required` 在手机上会卡） | 用 `full-access` + 文本约束 |
| **agent 之间互相寻址** | ❌ 没有跨 harness 的对讲 | 角色寻址 + 注册表桥接 |
| **跨面交接状态** | ❌ 只有线程，没有交接协议 | 五块交班条 + 可见性三选一 + 0–3 轮回问 |
| **手机端行为纪律** | ❌ agent 不知道自己在手机上 | SessionStart hook 内联纪律 |
| **通道可观测** | ❌ | 健康探针（只读、可挂夜巡） |
| 权限模型 | T3 的档位（机制层） | 我们的档位是**流程**概念（谁拍板、证据在哪）——两者互补，不冲突 |

> T3 自带浏览器、PR 事件唤醒、跨机委派、环境链接等能力，本套**不重复造**；这里只补
> 「两个 harness 之间的对讲与交班」这一层胶水。

---

## 4. 里面有什么

| 路径 | 是什么 |
|---|---|
| `tools/peer_resolve.py` | 角色 → 当前活着的 peer 名字（喂给 `SendMessage`）。`--role` / `--role all --json` / `--pid`（解析「自己」）/ `--no-mirror`（只读） |
| `tools/peer_mirror.py` | 两个配置目录的 peer 注册条目互抄（junction 共享后的兜底） |
| `tools/peer_health.py` | 只读健康探针：`ok` / `offline` / `degraded` / `error` |
| `config/peers.json` | 角色真源模板（改成你自己的仓与入口） |
| `skills/peer-session-comms/` | 对讲 skill：选通道、寻址、坑 |
| `skills/session-handoff-t3/` | 交接 skill：五块交班条、可见性、0–3 轮 |
| `skills/t3-code-ops/` | T3 本机运维：起停、provider/model、Windows 自启、排障索引 |
| `docs/peer-channel.md` | 通道机制与生命周期坑 |
| `docs/dual-track.md` | 什么时候开 T3 线程、什么时候留本地 |
| `hooks/t3-mobile-sessionstart.md` | 手机 remote session 的 hook 片段与装法 |

无第三方依赖，Python 3.10+ 标准库。

---

## 5. 五分钟装起来

**Step 1 — 让两边互相看得见**（Windows，二选一）

```powershell
# a) 共享注册表（推荐）：手机侧的 sessions 目录做 junction，指向桌面侧
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude_minimax\sessions" -Target "$env:USERPROFILE\.claude\sessions"
```

```bash
# b) 各自独立：让脚本互抄条目
python tools/peer_mirror.py
```

**Step 2 — 写角色表**：改 `config/peers.json` 的 `cwd_match` 与 `entrypoints`
（`cli` = 桌面 session，`t3-code` = T3 线程；同仓不同入口用入口区分）。

**Step 3 — 接线**（项目 `.claude/settings.json`）：

```json
{ "hooks": { "SessionStart": [ { "hooks": [ { "type": "command",
    "command": "python <repo>/tools/peer_mirror.py" } ] } ] } }
```

**Step 4 — 验证**：

```bash
python tools/peer_health.py        # 期望 ok（offline 也正常：只是暂时没人在线）
python tools/peer_resolve.py --role all --json
```

链路是否真的能送达，脚本验不了——它没有 `SendMessage` 能力，得让 agent 真发一条 ping。

---

## 6. 已验证的坑（Windows）

- **不要用 `os.kill(pid, 0)` 判活**：Windows 上那会 `TerminateProcess`，**直接把对方杀掉**。
  用 `OpenProcess` + `GetExitCodeProcess`（`STILL_ACTIVE`），并用 `procStart`（进程创建时间）
  对齐，防「同一个 pid 换了人」。
- **registry 文件会读到空窗**：对面 harness 定期重写自己的注册表（删除后重写，百毫秒空窗）
  → 手写脚本读它要重试（`peer_resolve.py` 内置重试）。
- **共享注册表 ≠ 共享配置目录**：把 `CLAUDE_CONFIG_DIR` 指到对方那套会重写真配置
  （provider base URL / token / 模型映射互相覆盖，桌面 session 会在两个 provider 之间被切）。
  只共享 `sessions/`。
- **只读场景必须加 `--no-mirror`**：默认会写 registry。在「只读轮次」里跑它属于越权写入。
- **T3 线程默认 worktree**：主工作区**未提交**的文件在线程里读不到 → 交接静默降级。
- **审批档**：手机端批不了 `approval-required` 的线程（会卡住等审批）→ 用 `full-access`，
  只读约束写进消息文本。
- **Windows 上 `t3 service install` 不支持**（上游文档写支持是错的）→ 用 VBS / 计划任务自启，
  见 `skills/t3-code-ops/`。

---

## 7. 适用范围与限制

- **Windows-first**：依赖命名管道、junction、Windows 进程 API。macOS / Linux 未验证
  （`peer_resolve.py` 的判活那段需要改写成 POSIX 版本）。
- **需要至少一个 T3 线程 + 一个本地 Claude Code session**才有意义；单端用不需要装。
- **不提供权限沙箱**：本套只解决「找得到、说得上、交得过去、看得见坏没坏」。
- 上游 T3 Code 本体：[pingdotgg/t3code](https://github.com/pingdotgg/t3code)（MIT）。
  本套是**适配与胶水**，不 fork 上游。

## License

MIT，见 [LICENSE](LICENSE)。
