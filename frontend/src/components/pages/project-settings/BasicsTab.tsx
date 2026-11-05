import { useCallback, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "cn";
import { API } from "@/api";
import { errMsg } from "@/utils/async";

import { DurationTierPicker } from "@/components/shared/DurationTierPicker";
import { EpisodeTargetDurationField } from "@/components/shared/EpisodeTargetDurationField";
import { ROUTE_META, RouteLockBadge } from "@/components/shared/GenerationRouteCards";
import { GridStoryboardBar } from "@/components/shared/GridStoryboardBar";
import { InlineWarning } from "@/components/shared/InlineWarning";
import { SpeechRateField } from "@/components/shared/SpeechRateField";
import { Button } from "@/components/ui/button";

import { gridToggleVisible, type ProjectFacts, type ProjectSettingsForm } from "./project-settings-form";
import { SettingsBlock, TabHeader } from "./SettingsBlock";

const ASPECT_RATIOS = [
  { ratio: "9:16", labelKey: "portrait_9_16", glyph: "h-3.5 w-2" },
  { ratio: "16:9", labelKey: "landscape_16_9", glyph: "h-2 w-3.5" },
] as const;

interface BasicsTabProps {
  value: ProjectSettingsForm;
  savedValue: ProjectSettingsForm;
  onChange: (update: (prev: ProjectSettingsForm) => ProjectSettingsForm) => void;
  facts: ProjectFacts & { sourceLanguage: string | null };
  /** 模式转换直接走 PATCH 端点，转换成功后需要整页重新加载事实。 */
  projectName: string;
  onReload: () => void;
}

/** 「基础」：画面比例、生成方式（只读，含宫格开关与混合模式转换入口）、口播语速估算与目标时长。 */
export function BasicsTab({ value, savedValue, onChange, facts, projectName, onReload }: BasicsTabProps) {
  const { t } = useTranslation("dashboard");
  const aspectId = useId();
  const route = ROUTE_META[facts.generationRoute];

  // 混合模式是单向汇点：storyboard / reference_video 可转入，转入后不可转出，故这里只给「转入」入口。
  const [confirming, setConfirming] = useState(false);
  const [converting, setConverting] = useState(false);
  const [convertError, setConvertError] = useState<string | null>(null);

  const handleConvert = useCallback(async () => {
    setConverting(true);
    setConvertError(null);
    try {
      await API.updateProject(projectName, { generation_mode: "mixed" });
      onReload();
    } catch (error) {
      setConvertError(errMsg(error));
    } finally {
      setConverting(false);
      setConfirming(false);
    }
  }, [projectName, onReload]);

  return (
    <div className="flex flex-col gap-6">
      <TabHeader title={t("project_settings_tab_basics")} />

      <SettingsBlock title={t("aspect_ratio")} titleId={aspectId}>
        <div role="radiogroup" aria-labelledby={aspectId} className="flex gap-2.5">
          {ASPECT_RATIOS.map(({ ratio, labelKey, glyph }) => {
            const selected = value.aspectRatio === ratio;
            return (
              <label
                key={ratio}
                className={cn(
                  "relative flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 transition-colors has-focus-visible:ring-3 has-focus-visible:ring-ring/50",
                  selected ? "border-primary/60 bg-primary/10" : "border-border hover:bg-accent",
                )}
              >
                <input
                  type="radio"
                  name={aspectId}
                  value={ratio}
                  checked={selected}
                  onChange={() => onChange((prev) => ({ ...prev, aspectRatio: ratio }))}
                  className="sr-only"
                />
                <span aria-hidden className={cn("rounded-xs border border-muted-foreground", glyph)} />
                <span className="text-sm text-foreground">{t(labelKey)}</span>
              </label>
            );
          })}
        </div>
        {value.aspectRatio !== savedValue.aspectRatio && <InlineWarning message={t("aspect_ratio_change_warning")} />}
      </SettingsBlock>

      <SettingsBlock title={t("generation_route")} aside={<RouteLockBadge />}>
        <div className="rounded-lg border border-border px-3.5 py-2.5">
          <div className="flex items-baseline gap-2">
            <span className="text-sm font-medium text-foreground">{t(route.nameKey)}</span>
            <span className="text-xs text-muted-foreground">{route.tag}</span>
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">{t(route.descKey)}</p>
        </div>
        {gridToggleVisible(facts) && (
          <GridStoryboardBar
            checked={value.gridStoryboard}
            onToggle={(next) => onChange((prev) => ({ ...prev, gridStoryboard: next }))}
          />
        )}
        {facts.generationRoute !== "mixed" && (
          <div className="mt-2.5">
            {convertError && <InlineWarning message={convertError} />}
            {confirming ? (
              <div className="flex flex-col gap-2">
                <p className="text-xs text-muted-foreground">{t("convert_to_mixed_confirm")}</p>
                <div className="flex items-center gap-2">
                  <Button size="xs" variant="destructive" disabled={converting} onClick={() => void handleConvert()}>
                    {converting ? t("common:loading") : t("convert_to_mixed")}
                  </Button>
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={converting}
                    onClick={() => {
                      setConfirming(false);
                      setConvertError(null);
                    }}
                  >
                    {t("common:cancel")}
                  </Button>
                </div>
              </div>
            ) : (
              <Button size="xs" variant="outline" onClick={() => setConfirming(true)}>
                {t("convert_to_mixed")}
              </Button>
            )}
          </div>
        )}
      </SettingsBlock>

      <SettingsBlock title={t("project_settings_pacing_title")}>
        <div className="flex flex-col gap-5">
          <SpeechRateField
            value={value.speechRate}
            onChange={(next) => onChange((prev) => ({ ...prev, speechRate: next }))}
            sourceLanguage={facts.sourceLanguage}
          />
          {/* 广告项目的整集体量由目标总时长表达，不呈现单集目标时长（服务端亦拒写） */}
          {facts.contentMode === "ad" ? (
            <DurationTierPicker
              value={value.adTargetDuration}
              onChange={(next) => onChange((prev) => ({ ...prev, adTargetDuration: next }))}
            />
          ) : (
            <EpisodeTargetDurationField
              value={value.episodeTargetDuration}
              onChange={(next) => onChange((prev) => ({ ...prev, episodeTargetDuration: next }))}
            />
          )}
        </div>
      </SettingsBlock>
    </div>
  );
}
