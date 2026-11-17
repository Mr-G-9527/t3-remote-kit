# 手机 remote session 的 SessionStart hook

> **为什么**：手机上的 agent 和桌面 agent 面对的物理条件不同（屏幕小、没人盯着、随时可能
> 断线）。如果它按桌面的节奏干活——长回复刷屏、跳过拍板直接改共享文件——在手机上就很难受。
> 让它**知道自己是在手机上**，靠 hook，不靠召回（recall 是概率性的，hook 是确定性的）。

## 判定信号

`CLAUDE_CODE_ENTRYPOINT == "t3-code"` —— 意思是**谁启动了这个进程**（T3 Code 拉起的 harness），
**不是**「当前用哪个客户端说话」。桌面 CLI 在手机 App 里被远程连上时，entrypoint 仍是 `cli`。

## 装法（项目级 `.claude/settings.json`）

```json
{ "hooks": { "SessionStart": [ { "hooks": [ { "type": "command",
    "command": "python -c \"import os; print('[T3 mobile] ...') if os.environ.get('CLAUDE_CODE_ENTRYPOINT')=='t3-code' else None\""
  } ] } ] } }
```

要点：

- **判定写在 hook 里，规则文本内联打印**——不要只打印一个 memory 名然后赌召回。
- 用**绝对路径**的解释器（如 `C:\\Python\\python.exe`），避免 PATH 变动导致静默失效。
- 装在**项目级** settings.json（不是用户级）：用户级 settings 会被整写覆盖。
- 改 hook 本体后跑一次自检（打印一条探针消息，确认命中/未命中两种情况都对）。

## 内联的规则（示例，可按你的习惯改）

1. **一屏内**：长内容落盘，给完整绝对路径，不刷屏。
2. **拍板单**：决策给「选项 + 推荐 + 理由」，一行一个。
3. **两轴分流**（可压缩 × 可逆）：发散或不可逆任务硬触发**软拒 = 旋转门**——
   标 `[软拒]` + 理由 + 改形状的替代方案；用户声明并给出理由即开门进降级模式。
4. **动共享文件前先看 mtime**：另一台设备可能正在写同一个文件。
5. 不可逆动作先回问。

## 维护纪律（最容易踩的一条）

规则文本会同时存在于**两个地方**：

- hook 里的内联文本
- 一份「事实源」文件（CLAUDE.md 的一节，或一条 memory）

**改规则必须两处同改。** 只改一处 → 注入内容与事实源漂移，而且没人会发现。

同款实现见 `skills/peer-session-comms/SKILL.md` 与 `skills/session-handoff-t3/SKILL.md`；
对讲通道见 `docs/peer-channel.md`。
