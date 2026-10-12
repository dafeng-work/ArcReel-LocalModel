import { useTranslation } from "react-i18next";
import { useEffect, useMemo, useState } from "react";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { InlineWarning } from "@/components/shared/InlineWarning";

/** G3 工作流步骤 → env 映射编辑：与 ``lib.config.dispatcher_schema.STEP_IDS`` 对齐。
 *  每个 step 配一行下拉，env 候选来自 props.available_envs（``/system/config`` 的 options.available_envs）。
 *  父组件把 raw JSON 字符串传给本组件做"格式化"展示 + 反序列化校验；保存时把字典序列化回 JSON。 */

export const WORKFLOW_STEP_IDS: ReadonlyArray<string> = [
  "project_input",
  "selling_points",
  "episode_plan",
  "script_plan_content",
  "script_plan_review",
  "final_script",
  "asset_sheets",
  "script_structure",
  "storyboard",
  "video",
  "edit",
];

interface Props {
  /** 父组件持有的 JSON 字符串。空串 = 用内置默认（仅在 exclusive 模式下生效）。 */
  value: string;
  onChange: (next: string) => void;
  availableEnvs: string[];
  /** 父组件传入默认 mapping（仅在展示时使用），用户编辑后会被覆盖。 */
  defaultMapping?: Record<string, string>;
}

interface ParseResult {
  ok: boolean;
  parsed: Record<string, string>;
  errorKey?: "env_mapping_invalid_json" | "env_mapping_key_unknown" | "env_mapping_env_unknown";
  errorParams?: Record<string, string>;
}

export function parseEnvMappingJson(raw: string, availableEnvs: ReadonlyArray<string>): ParseResult {
  if (!raw.trim()) {
    return { ok: true, parsed: {} };
  }
  let parsedUnknown: unknown;
  try {
    parsedUnknown = JSON.parse(raw);
  } catch {
    return { ok: false, parsed: {}, errorKey: "env_mapping_invalid_json" };
  }
  if (!parsedUnknown || typeof parsedUnknown !== "object" || Array.isArray(parsedUnknown)) {
    return { ok: false, parsed: {}, errorKey: "env_mapping_invalid_json" };
  }
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(parsedUnknown as Record<string, unknown>)) {
    if (typeof v !== "string") {
      return { ok: false, parsed: {}, errorKey: "env_mapping_invalid_json" };
    }
    if (!WORKFLOW_STEP_IDS.includes(k)) {
      return { ok: false, parsed: {}, errorKey: "env_mapping_key_unknown", errorParams: { key: k } };
    }
    if (availableEnvs.length > 0 && !availableEnvs.includes(v)) {
      return { ok: false, parsed: {}, errorKey: "env_mapping_env_unknown", errorParams: { env: v } };
    }
    out[k] = v;
  }
  return { ok: true, parsed: out };
}

function serializeEnvMapping(parsed: Record<string, string>): string {
  const keys = Object.keys(parsed).sort();
  if (keys.length === 0) {
    return "";
  }
  const sorted: Record<string, string> = {};
  for (const k of keys) {
    sorted[k] = parsed[k];
  }
  return JSON.stringify(sorted);
}

export function EnvMappingSection({ value, onChange, availableEnvs, defaultMapping }: Props) {
  const { t } = useTranslation("dashboard");
  const fallback = useMemo<Record<string, string>>(() => defaultMapping ?? {}, [defaultMapping]);

  const [local, setLocal] = useState<Record<string, string>>({});

  // 当父组件 raw 变化时同步本地状态；空串恢复默认；非空用解析结果。
  useEffect(() => {
    const parsed = parseEnvMappingJson(value, availableEnvs);
    if (parsed.ok) {
      if (Object.keys(parsed.parsed).length === 0) {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- 父组件 value 变化时与本地状态同步：标准受控→非受控同步模式。
        setLocal({ ...fallback });
      } else {
        setLocal({ ...parsed.parsed });
      }
    }
    // 解析失败时不覆盖 local——保留用户最后一次有效输入
  }, [value, availableEnvs, fallback]);

  const handleStepChange = (stepId: string, nextEnv: string) => {
    const updated = { ...local, [stepId]: nextEnv };
    setLocal(updated);
    onChange(serializeEnvMapping(updated));
  };

  const parseError = (() => {
    if (!value.trim()) {
      return null;
    }
    const result = parseEnvMappingJson(value, availableEnvs);
    if (result.ok) {
      return null;
    }
    return result;
  })();

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-0.5">
        <h2 className="text-lg font-medium">{t("dashboard:env_mapping_section_title")}</h2>
        <p className="text-sm text-muted-foreground">{t("dashboard:env_mapping_section_help")}</p>
      </div>

      {parseError ? (
        <InlineWarning
          message={
            parseError.errorParams
              ? t(`dashboard:${parseError.errorKey}`, parseError.errorParams)
              : t(`dashboard:${parseError.errorKey}`)
          }
        />
      ) : null}

      <div className="rounded-lg border border-border bg-card">
        {WORKFLOW_STEP_IDS.map((stepId) => {
          const labelId = `env-mapping-${stepId}-label`;
          const current = local[stepId] ?? fallback[stepId] ?? "";
          return (
            <div
              key={stepId}
              className="flex items-center justify-between gap-6 border-b border-border px-4 py-2 last:border-b-0"
            >
              <Label id={labelId} htmlFor={`env-mapping-${stepId}`}>
                <code className="font-mono text-xs">{stepId}</code>
              </Label>
              <Select
                items={(availableEnvs.length > 0 ? availableEnvs : (current ? [current] : [])).map((env) => ({
                  value: env,
                  label: env,
                }))}
                value={current}
                onValueChange={(next) => handleStepChange(stepId, next ?? "")}
              >
                <SelectTrigger id={`env-mapping-${stepId}`} aria-labelledby={labelId} className="w-56">
                  <SelectValue placeholder={t("dashboard:env_mapping_empty_hint")} />
                </SelectTrigger>
                <SelectContent>
                  {(availableEnvs.length > 0 ? availableEnvs : current ? [current] : []).map((env) => (
                    <SelectItem key={env} value={env}>
                      {env}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          );
        })}
      </div>
    </div>
  );
}
