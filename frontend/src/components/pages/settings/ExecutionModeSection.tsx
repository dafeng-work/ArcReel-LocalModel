import { useTranslation } from "react-i18next";
import { Loader2, Play } from "lucide-react";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { InlineWarning } from "@/components/shared/InlineWarning";
import { API } from "@/api";
import type { PreflightTestResponse } from "@/types/system";

/** G3 单机单卡互斥模式的「执行模式」Section —— 单选 Radio + 当前 env 显示 + 测试 preflight 按钮。 */

export type ExecutionMode = "multi" | "exclusive";

interface Props {
  value: ExecutionMode;
  onChange: (next: ExecutionMode) => void;
  currentHostEnv: string | null;
}

export function ExecutionModeSection({ value, onChange, currentHostEnv }: Props) {
  const { t } = useTranslation("dashboard");
  const groupId = useId();
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<PreflightTestResponse | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const runDryRun = async () => {
    setTesting(true);
    setTestError(null);
    setTestResult(null);
    try {
      const res = await API.testPreflight({});
      setTestResult(res);
    } catch (err) {
      setTestError(err instanceof Error ? err.message : String(err));
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-0.5">
        <h2 className="text-lg font-medium">{t("dashboard:execution_mode_section_title")}</h2>
        <p className="text-sm text-muted-foreground">{t("dashboard:execution_mode_section_help")}</p>
      </div>

      <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3">
        <RadioGroup
          value={value}
          onValueChange={(next) => onChange(next === "exclusive" ? "exclusive" : "multi")}
          aria-labelledby={groupId}
        >
          <div className="flex items-start gap-3">
            <RadioGroupItem id={`${groupId}-multi`} value="multi" />
            <Label htmlFor={`${groupId}-multi`}>
              <span className="text-sm font-medium">{t("dashboard:execution_mode_multi")}</span>
            </Label>
          </div>
          <div className="flex items-start gap-3">
            <RadioGroupItem id={`${groupId}-exclusive`} value="exclusive" />
            <Label htmlFor={`${groupId}-exclusive`}>
              <span className="text-sm font-medium">{t("dashboard:execution_mode_exclusive")}</span>
            </Label>
          </div>
        </RadioGroup>
      </div>

      <div className="flex items-center gap-3 rounded-lg border border-border bg-card px-3 py-2 text-sm">
        <span className="text-muted-foreground">
          {currentHostEnv
            ? t("dashboard:execution_mode_current_host_env", { env: currentHostEnv })
            : t("dashboard:execution_mode_host_env_unknown")}
        </span>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void runDryRun()}
          disabled={testing}
        >
          {testing ? (
            <Loader2 aria-hidden className="animate-spin" data-icon="inline-start" />
          ) : (
            <Play aria-hidden data-icon="inline-start" />
          )}
          {testing ? t("dashboard:preflight_test_running") : t("dashboard:preflight_test_button")}
        </Button>
      </div>

      {testError ? <InlineWarning message={testError} /> : null}
      {testResult ? (
        <div className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <div>current_host_env = {testResult.current_host_env ?? "(unknown)"}</div>
          <div>target_env = {testResult.target_env ?? "(none)"}</div>
          <div>available_envs = {testResult.available_envs.join(", ") || "(none)"}</div>
          <div>would_switch = {String(testResult.would_switch)}</div>
          <div>reason = {testResult.reason}</div>
        </div>
      ) : null}
    </div>
  );
}
