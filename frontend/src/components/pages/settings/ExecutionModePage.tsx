import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Save } from "lucide-react";
import { Button } from "@/components/ui/button";
import { InlineWarning } from "@/components/shared/InlineWarning";
import { API } from "@/api";
import { ExecutionModeSection, type ExecutionMode } from "./ExecutionModeSection";
import { EnvMappingSection } from "./EnvMappingSection";
import type { GetSystemConfigResponse } from "@/types/system";

/** G3 单机单卡互斥模式的统一页面：执行模式 + env_mapping，共享一个保存按钮。
 *  加载 /system/config → 编辑本地状态 → 点保存调 PATCH /system/config → 重新拉取。
 */

const DEFAULT_MAPPING: Record<string, string> = {
  project_input: "nsfw",
  selling_points: "nsfw",
  episode_plan: "nsfw",
  script_plan_content: "nsfw",
  script_plan_review: "nsfw",
  final_script: "nsfw",
  asset_sheets: "image-qwen21",
  script_structure: "nsfw",
  storyboard: "nsfw",
  video: "video-ltx23-sulphur",
  edit: "nsfw",
};

export function ExecutionModePage() {
  const { t } = useTranslation("dashboard");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveOk, setSaveOk] = useState(false);
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("multi");
  const [envMapping, setEnvMapping] = useState<string>("");
  const [availableEnvs, setAvailableEnvs] = useState<string[]>([]);
  const [currentHostEnv, setCurrentHostEnv] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res: GetSystemConfigResponse = await API.getSystemConfig();
      setExecutionMode(res.settings.execution_mode ?? "multi");
      setEnvMapping(res.settings.env_mapping ?? "");
      setAvailableEnvs(res.options.available_envs ?? []);
      setCurrentHostEnv(res.options.current_host_env ?? null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 数据加载：mount 时拉一次配置；refresh 闭包是 useCallback([]) 稳定的。
    void refresh();
  }, [refresh]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setSaveError(null);
    setSaveOk(false);
    try {
      await API.updateSystemConfig({ execution_mode: executionMode, env_mapping: envMapping });
      setSaveOk(true);
      await refresh();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }, [executionMode, envMapping, refresh]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 px-6 py-4 text-sm text-muted-foreground">
        <Loader2 aria-hidden className="size-4 animate-spin" />
        <span>{t("dashboard:loading")}</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 px-6 py-4">
      {loadError ? <InlineWarning message={loadError} /> : null}

      <ExecutionModeSection
        value={executionMode}
        onChange={setExecutionMode}
        currentHostEnv={currentHostEnv}
      />

      <EnvMappingSection
        value={envMapping}
        onChange={setEnvMapping}
        availableEnvs={availableEnvs}
        defaultMapping={DEFAULT_MAPPING}
      />

      <div className="flex items-center gap-3">
        <Button type="button" onClick={() => void handleSave()} disabled={saving}>
          {saving ? (
            <Loader2 aria-hidden className="animate-spin" data-icon="inline-start" />
          ) : (
            <Save aria-hidden data-icon="inline-start" />
          )}
          {t("dashboard:save")}
        </Button>
        {saveError ? <InlineWarning message={saveError} /> : null}
        {saveOk ? <span className="text-sm text-primary">{t("dashboard:saved")}</span> : null}
      </div>
    </div>
  );
}
