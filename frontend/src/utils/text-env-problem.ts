import { ApiRequestError } from "@/api/errors";

/**
 * 文本模型 env 前置闸门失败时的结构化信息（G4）。
 *
 * 后端在 `ApiRequestError.diagnostic` 里下发 `{code, params}`，与 `text_output_truncated`
 * 同一形状；这里按 code 分派成两种可执行的提示。
 */

/** 目标 env 未激活，且有在跑的 GPU 作业，不能自动切换。 */
export interface TextEnvSwitchRequired {
  kind: "env-switch-required";
  /** 宿主当前运行的 env。 */
  currentEnv: string;
  /** 该文本模型需要、但尚未激活的 env。 */
  targetEnv: string;
  /** 正在占用 GPU 的任务数。 */
  count: number;
}

/** 本机网关上游不可达（502/503/504 或连不上）。 */
export interface TextProviderUnavailable {
  kind: "provider-unavailable";
  provider: string;
  model: string;
  /** 上游 HTTP 状态码；上游没给状态码时是异常类型名。 */
  status: string | number;
}

export type TextEnvProblem = TextEnvSwitchRequired | TextProviderUnavailable;

function _str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function _count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 从同步请求的 `ApiRequestError` 里取出 env 闸门问题；不是该类问题返回 null。 */
export function textEnvProblemOfError(err: unknown): TextEnvProblem | null {
  if (!(err instanceof ApiRequestError)) return null;
  const problem = err.diagnostic;
  if (typeof problem !== "object" || problem === null) return null;
  const { code, params } = problem as { code?: unknown; params?: unknown };
  if (typeof params !== "object" || params === null) return null;
  const p = params as Record<string, unknown>;

  if (code === "text_env_switch_required") {
    const currentEnv = _str(p.current_env);
    const targetEnv = _str(p.target_env);
    const count = _count(p.count);
    if (!currentEnv || !targetEnv || count === null) return null;
    return { kind: "env-switch-required", currentEnv, targetEnv, count };
  }

  if (code === "text_provider_unavailable") {
    const provider = _str(p.provider);
    const model = _str(p.model);
    if (!provider || !model) return null;
    const status = typeof p.status === "number" || typeof p.status === "string" ? p.status : null;
    return { kind: "provider-unavailable", provider, model, status: status ?? "?" };
  }

  return null;
}

/** 手动切换 env 的命令：与后端文案、`arcreel-env-gate.sh` 的参数口径一致。 */
export function envSwitchCommand(targetEnv: string): string {
  return `arcreel-env-gate.sh ${targetEnv} -- true`;
}