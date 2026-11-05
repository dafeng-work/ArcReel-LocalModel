import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ImagePlus, X } from "lucide-react";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import { useReferenceVideoStore } from "@/stores/reference-video-store";
import { Button } from "@/components/ui/button";
import type { ReferenceVideoUnit } from "@/types";

interface UnitFirstFramePickerProps {
  unit: ReferenceVideoUnit;
  projectName: string;
  episode: number;
  /** 与面板主 CTA 同口径的禁用集：在跑/保存/还原期间不让首帧写回与生成冲突。 */
  disabled?: boolean;
}

interface ImageCandidate {
  key: string;
  label: string;
  asset: string;
}

/**
 * 混合模式单元的「首帧资产」附挂控件。
 *
 * 只出现在 mixed 项目：附挂与否决定该单元的生成路线——附挂 → 首帧（i2v 桶，
 * 参考图被忽略），不附挂 → 参考（r2v 桶）。候选取项目内既有图片资产
 * （角色参考图 / 场景与道具场景图），不新增上传通道；保存走 store.patchUnit
 * （PATCH unit 端点），服务端闸：非 mixed 设值 → 400，mixed 空串 = 清除。
 */
export function UnitFirstFramePicker({ unit, projectName, episode, disabled }: UnitFirstFramePickerProps) {
  const { t } = useTranslation("dashboard");
  const project = useProjectsStore((s) => s.currentProjectData);
  const patchUnit = useReferenceVideoStore((s) => s.patchUnit);
  const [saving, setSaving] = useState(false);
  const [selectedAsset, setSelectedAsset] = useState("");

  const candidates = useMemo<ImageCandidate[]>(() => {
    if (!project) return [];
    const out: ImageCandidate[] = [];
    for (const [name, character] of Object.entries(project.characters ?? {})) {
      if (character.reference_image) {
        out.push({ key: `character:${name}`, label: `${name} · ${character.reference_image}`, asset: character.reference_image });
      }
    }
    for (const [name, scene] of Object.entries(project.scenes ?? {})) {
      if (scene.scene_sheet) {
        out.push({ key: `scene:${name}`, label: `${name} · ${scene.scene_sheet}`, asset: scene.scene_sheet });
      }
    }
    for (const [name, prop] of Object.entries(project.props ?? {})) {
      if (prop.prop_sheet) {
        out.push({ key: `prop:${name}`, label: `${name} · ${prop.prop_sheet}`, asset: prop.prop_sheet });
      }
    }
    return out;
  }, [project]);

  const attached = unit.first_frame_asset ?? null;

  const saveFirstFrame = useCallback(
    async (asset: string) => {
      setSaving(true);
      try {
        // 空串 = 清除（mixed 语义），非空 = 附挂；两种都走同一 PATCH 字段。
        await patchUnit(projectName, episode, unit.unit_id, { first_frame_asset: asset });
      } finally {
        setSaving(false);
      }
    },
    [patchUnit, projectName, episode, unit.unit_id],
  );

  if (project?.generation_mode !== "mixed") return null;

  return (
    <div className="rounded-lg border border-border p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold text-subtle-foreground">{t("unit_first_frame_title")}</span>
        {/* 生效路线徽标：附挂与否是 mixed 单元定桶的唯一输入，显式标出省得用户猜。 */}
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
            attached ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"
          }`}
        >
          {attached ? t("unit_first_frame_route_first_frame") : t("unit_first_frame_route_reference")}
        </span>
      </div>
      {attached ? (
        <div className="flex items-center gap-2">
          <img
            src={API.getFileUrl(projectName, attached, null)}
            alt=""
            className="h-12 w-12 shrink-0 rounded border border-border object-cover"
          />
          <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground" title={attached}>
            {attached}
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-2 text-xs"
            disabled={disabled || saving}
            onClick={() => void saveFirstFrame("")}
          >
            <X className="size-3" aria-hidden="true" />
            {t("unit_first_frame_clear")}
          </Button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <select
            value={selectedAsset}
            onChange={(e) => setSelectedAsset(e.target.value)}
            className="h-8 min-w-0 flex-1 rounded border border-input bg-background px-2 text-xs"
            disabled={disabled || saving}
          >
            <option value="">{t("unit_first_frame_select_placeholder")}</option>
            {candidates.map((c) => (
              <option key={c.key} value={c.asset}>
                {c.label}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            className="h-8 gap-1 px-2 text-xs"
            disabled={disabled || saving || !selectedAsset}
            onClick={() => void saveFirstFrame(selectedAsset)}
          >
            <ImagePlus className="size-3" aria-hidden="true" />
            {t("unit_first_frame_attach")}
          </Button>
        </div>
      )}
    </div>
  );
}
