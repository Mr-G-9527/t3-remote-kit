---
name: session-handoff-t3
description: 把当前 session 的活交接给 T3 Code thread（桌面/手机同一线程继续）。触发词：交给 T3 / 交接给 T3 / 移交给 T3 / 换到 T3 继续 / 这个活转 T3 / handoff to t3 / 让 T3 接手 / 换阵地。
---

# 交接当前 session → T3 Code thread

**这个 skill 干什么**：把本 session 的状态与上下文交给一个 T3 thread，之后你在电脑网页或手机
App 上接着同一个 thread 干活。

**前提认知**：T3 **不能接管**本进程。T3 thread 里跑的是另一个 Claude Code 实例，交接的是
**状态**，不是进程。验收标准是「那边能不能精准接着干」，不是「同一个 pid」。

分工判据与边界见 `docs/dual-track.md`；T3 机器侧配置见 `skills/t3-code-ops/SKILL.md` §0。

---

## 步骤

### 1. 收集五块（不许凭印象编）

| 块 | 要求 |
|---|---|
| 目标 | 一句话 |
| 已知事实 | 路径 / 命令 / 结论，**带证据**（文件名:行号、命令输出） |
| 已做 / 未做 | 分开，别混 |
| 下一步 | **第一条可执行命令**（不是「继续调研」） |
| 预判疑问 | 对方大概率会问的 3–5 个问题 + **本 session 已定的默认答案** |

第 5 块是「0 轮自解」的关键：能自己答的不许占用回问轮次。

### 2. 写交接文件

路径 `docs/handoff/<YYYY-MM-DD>-<slug>.md`。**不许**用根目录 `CONTEXT.md`。

### 3. 决定可见性（**静默失败高发点**）

T3 thread 的 workspace 默认 `worktree`（独立 checkout）。坑的准确边界：

- **交接文件未提交** → worktree 里读不到 → 交接静默降级成「按缺前提开工」
- **交接文件已 commit** → worktree 从当前 HEAD 拉，文件**可见**

三条路，任选其一，**必须显式决定**：

1. **默认**：thread 跑在项目目录（不加 `--worktree`）→ 未提交文件直接可见
2. 用 worktree → **交接文件必须先 commit**
3. 读不到（ENOENT）→ **兜底**：把五块内容内联进 thread 第一条消息，不许静默开工

### 4. 建 thread

```bash
t3 thread start "<repo 绝对路径>" "<第一条消息>" \
  --title "<短标题>" --provider claudeAgent --model <manifest 里的真实 slug>
```

- `--provider` **必须用真值**（UI 建实例时自动生成的 `claudeAgent_2` 之类是废 id，会报
  "disabled for project"）。真值查 `~/.t3/userdata/statev2.sqlite` 的
  `orchestration_v2_projection_provider_sessions.provider_instance_id`。
- `--model` **必须**是本地 `~/.t3/userdata/model-manifest.json` 里真有的 slug；官方模型名
  在自建网关上会 403。
- 权限默认 `full-access`：手机端批不了 `approval-required`（线程会卡在审批上等）。
- 首条消息**只引用交接文件路径 + 一句指令**，不复述内容：

```text
读 docs/handoff/<file>.md，按其中「验证清单 / 下一步」执行并汇报。
```

### 5. 轮询 + 回问（0–3 轮，人不盯着也要能走完）

接手方先**自解预判疑问**（0 轮），只对**阻塞项**在 thread 内回问（1–3 轮）。
**超过 3 轮仍未对齐 = 交接失败**：说明五块没写够，不是接手方的问题——
回去补交班条（已知事实 + 证据 + 下一步命令），别加长对话。

### 6. 收尾

- 把交接结论**落进长期文档**（规则/契约类），别只留在 thread 里。
- 记录哪些**未验证**（没测过的行为不要写成结论）。
- thread 侧的越权写入（只读轮次里写文件）当场纠正并写进硬约束。

---

## 硬约束

1. **写入类动作需要当轮明确授权**。只读轮次里新建报告文件 = 越权。
2. **一次观测 ≠ 永久结论**。「worktree 可见性坑从此不成立」是过度断言；准确说法是
   「仅当交接文件未提交时不成立」。
3. **别推荐与主张互相矛盾的前提**。例如既主张改用 thread id 寻址，又说「寻址成立与否
   只差 SendMessage 可用性这一条证据」——先自洽。
4. **别给失效指针标「不影响执行」**。如果那条命令正是接手方要跑的第一条，它错了就是错了。
