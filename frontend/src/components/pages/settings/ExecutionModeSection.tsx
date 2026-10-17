import { useTranslation } from "react-i18next";
import { Loader2, Play, TriangleAlert } from "lucide-react";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Switch } from "@/components/ui/switch";
import { InlineWarning } from "@/components/shared/InlineWarning";
import { API } from "@/api";
import type { PreflightTestResponse } from "@/types/system";

/** G3 单机单卡互斥模式的「执行模式」Section —— 单选 Radio + auto 开关 + 当前 env 显示 + 测试 preflight 按钮。 */

export type ExecutionMode = "multi" | "exclusive";

interface Props {
  value: ExecutionMode;
  onChange: (next: ExecutionMode) => void;
  currentHostEnv: string | null;
  /** auto 权限模式开关；true = 每次工具调用前多一次 LLM 安全分类请求。 */
  autoMode: boolean;
  onAutoModeChange: (next: boolean) => void;
  /**
   * 工具返回前把宿主 env 切回 Agent brain 的文本 env。
   * true = preflight 切出去的 wide-nsfw 会被切回，Agent 下一次 LLM 调用不必面对
   * ECONNREFUSED；代价是每个 GPU 工具返回时多一次 30–90s 的切换。
   */
  postToolEnvRestore: boolean;
  onPostToolEnvRestoreChange: (next: boolean) => void;
}

export function ExecutionModeSection({
  value,
  onChange,
  currentHostEnv,
  autoMode,
  onAutoModeChange,
  postToolEnvRestore,
  onPostToolEnvRestoreChange,
}: Props) {
  const { t } = useTranslation("dashboard");
  const groupId = useId();
  const autoId = useId();
  const restoreId = useId();
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

      <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3">
        <div className="flex items-start justify-between gap-3">
          <Label htmlFor={autoId} className="flex flex-col">
            <span className="mb-0.5 text-sm font-medium">{t("dashboard:agent_auto_mode_label")}</span>
            <span className="text-xs text-muted-foreground">{t("dashboard:agent_auto_mode_help")}</span>
          </Label>
          <Switch id={autoId} checked={autoMode} onCheckedChange={onAutoModeChange} />
        </div>
        {/*
          警示文字常驻可见，不能只藏在 help 里：开关是硬命令，ArcReel 不会替单卡部署
          自动降级 —— 开着就是会多一次 LLM 分类请求，忘了关就会重现空转死锁。开启态用
          destructive 配色让后果一眼可见。
        */}
        <p
          className={
            autoMode
              ? "flex items-start gap-1.5 rounded-md bg-destructive/10 px-2 py-1.5 text-xs text-destructive"
              : "flex items-start gap-1.5 rounded-md bg-muted/60 px-2 py-1.5 text-xs text-muted-foreground"
          }
        >
          <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0" data-icon="inline-start" />
          <span>{autoMode ? t("dashboard:agent_auto_mode_warn_on") : t("dashboard:agent_auto_mode_warn_off")}</span>
        </p>
      </div>

      {/*
        与 auto 开关同构，但**语义相反**：那个开关开着会空转死锁（要关），
        这个开关关着会 ECONNREFUSED（要开）。故开启态不用 destructive，改用普通
        警示色；关闭态才提示后果，因为「关了会坏」是用户更容易漏掉的那一边。
      */}
      <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3">
        <div className="flex items-start justify-between gap-3">
          <Label htmlFor={restoreId} className="flex flex-col">
            <span className="mb-0.5 text-sm font-medium">{t("dashboard:post_tool_env_restore_label")}</span>
            <span className="text-xs text-muted-foreground">{t("dashboard:post_tool_env_restore_help")}</span>
          </Label>
          <Switch
            id={restoreId}
            checked={postToolEnvRestore}
            onCheckedChange={onPostToolEnvRestoreChange}
          />
        </div>
        <p
          className={
            postToolEnvRestore
              ? "flex items-start gap-1.5 rounded-md bg-muted/60 px-2 py-1.5 text-xs text-muted-foreground"
              : "flex items-start gap-1.5 rounded-md bg-destructive/10 px-2 py-1.5 text-xs text-destructive"
          }
        >
          <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0" data-icon="inline-start" />
          <span>
            {postToolEnvRestore
              ? t("dashboard:post_tool_env_restore_warn_on")
              : t("dashboard:post_tool_env_restore_warn_off")}
          </span>
        </p>
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
