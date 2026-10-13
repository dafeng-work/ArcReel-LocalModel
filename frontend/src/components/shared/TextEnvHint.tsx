import { useTranslation } from "react-i18next";
import { Link } from "wouter";

import { providerSettingsPath } from "@/app-routes";
import { buttonVariants } from "@/components/ui/button";
import { envSwitchCommand, type TextEnvProblem } from "@/utils/text-env-problem";

const CUSTOM_PROVIDER_PREFIX = "custom-";

/** 自定义供应商在设置页的位置；内置供应商没有这一页，返回 null。 */
function customProviderSettingsPath(provider: string): string | null {
  if (!provider.startsWith(CUSTOM_PROVIDER_PREFIX)) return null;
  const id = provider.slice(CUSTOM_PROVIDER_PREFIX.length);
  if (!/^\d+$/.test(id)) return null;
  return providerSettingsPath({ custom: Number(id) });
}

/**
 * 文本模型 env 前置闸门失败时的出路。与 `OutputTruncationHint` 同一形态：主错误文案之上
 * 再给一条能直接照做的操作 —— 要么把手动切 env 的命令原样给出，要么跳去供应商设置改 env 绑定。
 */
export function TextEnvHint({ problem }: { problem: TextEnvProblem }) {
  const { t } = useTranslation("dashboard");

  if (problem.kind === "env-switch-required") {
    return (
      <div className="flex flex-col gap-1 text-muted-foreground">
        <p className="m-0">
          {t("text_env_switch_required_detail", {
            currentEnv: problem.currentEnv,
            targetEnv: problem.targetEnv,
            count: problem.count,
          })}
        </p>
        <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{envSwitchCommand(problem.targetEnv)}</code>
      </div>
    );
  }

  const settingsPath = customProviderSettingsPath(problem.provider);
  return (
    <div className="flex flex-col items-start gap-1.5">
      <p className="m-0 text-muted-foreground">
        {t("text_provider_unavailable_detail", {
          provider: problem.provider,
          model: problem.model,
          status: problem.status,
        })}
      </p>
      {settingsPath !== null ? (
        <Link href={`~${settingsPath}`} className={buttonVariants({ variant: "outline", size: "sm" })}>
          {t("text_provider_unavailable_open_settings")}
        </Link>
      ) : null}
    </div>
  );
}