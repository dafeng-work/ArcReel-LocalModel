# G3 单机单卡互斥模式 — 配置与运维手册

> 适用范围：本机单卡（Ubuntu 24.04 + R9700 + `arcreel-env-gate.sh` + `r9700-env`）的 ArcReel 单机部署。
> 若 fork-ArcReel 容器内已绑挂 `/home/dafeng/ai-stack`，在「全局设置 → 执行模式」切到 **exclusive** 即可启用。

## 是什么

**执行模式 = exclusive**：工作流执行到某个步骤时，**先看本地宿主机 env 是否是该步骤需要的 env**；若不是，触发 `arcreel-env-gate.sh` 切 env、等就绪，再把工作流继续 dispatch 下去。

**执行模式 = multi（默认）**：与上游 ArcReel 一致——多个模型可同时 ready，不做 env 切换。这是零回归模式。

## 启用步骤

### 1. 确认宿主 env 已被 `arcreel-env-gate.sh` 管住

```bash
/home/dafeng/ai-stack/bin/arcreel-env-gate.sh video-ltx23-sulphur -- true
```

切完就绪约 30-60s。返回 0 = env 已切好；返回非 0 看 stderr 末尾。

### 2. 容器内 `/home/dafeng/ai-stack` 必须 bind mount

`deploy/docker-compose.yml` 的 arcReel 服务需暴露：

```yaml
volumes:
  - /home/dafeng/ai-stack:/home/dafeng/ai-stack
extra_hosts:
  - host.docker.internal:host-gateway
privileged: true   # bwrap 沙箱探测在本机需要
```

参见本工作区 `AGENTS.md`「部署适配约定」节。

### 3. 在 Web UI 切到 exclusive

`/settings?section=execution-mode` → **「单机互斥」** → 保存。

第一次保存会自动写一条 `system_settings.execution_mode = "exclusive"`，以及一条空 `system_settings.env_mapping`（表示使用内置默认 mapping）。

### 4. 验证

点 **「测试 preflight（dry-run）」** 按钮，调 `POST /system/config/test-preflight`，返回结构化结果：

- `current_host_env`：当前宿主 env（读 `run/current.env`）
- `available_envs`：宿主机可用的 env 名集合
- `would_switch`：是否需要切换
- `reason`：自然语言描述

### 5. 默认 env_mapping

未配 `env_mapping` 时使用内置默认（与 plan-review 阶段确认）：

| step | env |
|---|---|
| `project_input` / `selling_points` / `episode_plan` / `script_plan_content` / `script_plan_review` / `final_script` / `script_structure` / `storyboard` / `edit` | `nsfw` |
| `asset_sheets` | `image-qwen21`（子模型 uc-q8） |
| `video` | `video-ltx23-sulphur` |

可以在 Web UI 覆盖每一个 step 的 env 映射（每个 step 一个下拉），保存即生效。

## 故障排查

### env-gate 脚本找不到（exit 127）

```
EnvSwitchGateMissing: env-gate script not found at /home/dafeng/ai-stack/bin/arcreel-env-gate.sh
```

→ 检查容器 bind mount：`/home/dafeng/ai-stack/bin/arcreel-env-gate.sh` 在容器内是否可达。

### env 切换超时（>150s）

```
env_switch_timeout
```

→ 查 `/home/dafeng/ai-stack/logs/env_switch_<ts>_<env>.log`（fork 写 stderr 末 2KB 进该文件）：
- GPU OOM：检查 `r9700-env status` 与显存占用
- 模型未加载完：env-gate 已 wait_ready，但本模块在 ready_probe 再补一轮 30 次 × 5s 探测
- 磁盘满：`models/llm/Qwen3.8-27B-*.gguf` 可能未加载

### 切换成功但推理仍超时

切到 env 后推理仍打挂 → 跟 env 切换无关，是 G2 层的另一类问题：
- 单卡 + 大模型 + 长上下文 → 物理组合限制
- 临时降级：把 `execution_mode` 切回 `multi`，等下一轮 fork-ArcReel 上游加入会话内 LLM 切换支持（v2 范围）

### 切换日志位置

- 每次切换生成一份 `$STACK_ROOT/logs/env_switch_<unix_ts>_<env>.log`，含 stdout / stderr 全文与 exit_code
- 当前 env 名读自 `$STACK_ROOT/run/current.env`

## 与 G1 资源组锁的关系

G1 锁（`provider_id:media_type`）在调度器侧生效 — 让 ArcReel 调度器感知共享独占资源，避免两个视频任务同时抢 GPU。

G3 preflight 是**步骤级**前置闸门，在 dispatch 之前先把 env 切对。

二者**不冲突**：

| 触发时机 | G1 | G3 |
|---|---|---|
| 任务队列 enqueue | ✓ | — |
| `next_action` dispatch 前 | — | ✓ |

G1 让"同时跑两个视频任务"不会撞车；G3 让"切到 video 步骤前先切到 video-ltx23-sulphur"。

## 不做的事

- **不做会话内 LLM 切换**（v2 范围）。单次 Agent 会话的 LLM env 绑死在 `agent_anthropic_credentials` 激活的那一条；中途不切。
- **不做自动选 env**。用户必须显式在 Web UI 配置 `env_mapping`。
- **不做通用 schema / 凭据引用表 / 白名单占位符**（前轮通用方案弃用）。
- **不动 alembic** —— `system_settings` 已是键值表，新增 `execution_mode` / `env_mapping` 零迁移。
- **不动 G1 锁** —— 它已经在调度器侧生效。

## 验收清单

| # | 项 | 命令 / 动作 | 期望 |
|---|---|---|---|
| 1 | 零回归：默认 `multi` 行为与改动前 100% 一致 | alembic head 对比 | 仍是 `g1exclusive_resource_group` |
| 2 | 文本步骤不触发 preflight | `prepare_script_plan` 在 nsfw 下 dispatch | 1s 内成功 |
| 3 | 视频步骤触发 preflight | 在 nsfw 下 dispatch 视频批量 | 看到「正在切换到 video-ltx23-sulphur（预计 45s）」→ ~45s 后 dispatch 成功 |
| 4 | G1 锁仍生效 | 第二次 dispatch 同 `provider_id:media_type` | 调度器排队 |
| 5 | 降级路径 | 从 exclusive 切回 multi | preflight 旁路 |
| 6 | 多语言 | 切换 zh / en / vi | 全局设置页 + 错误提示均正确成文 |
| 7 | 测试 preflight | 点 Web UI 按钮 | 返回 current_host_env + available_envs + would_switch + reason |
