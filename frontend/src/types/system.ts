export interface SystemConfigSettings {
  default_video_backend: string;
  default_video_backend_i2v?: string;
  default_video_backend_r2v?: string;
  default_image_backend: string;
  default_image_backend_t2i?: string;
  default_image_backend_i2i?: string;
  default_text_backend: string;
  default_audio_backend?: string;
  narration_voice?: string;
  narration_speed?: number | null;
  text_backend_simple: string;
  text_backend_complex: string;
  video_generate_audio: boolean;
  video_poll_timeout_seconds: number;
  anthropic_api_key: { is_set: boolean; masked: string | null };
  anthropic_base_url: string;
  anthropic_model: string;
  anthropic_default_haiku_model: string;
  anthropic_default_opus_model: string;
  anthropic_default_sonnet_model: string;
  claude_code_subagent_model: string;
  agent_session_cleanup_delay_seconds: number;
  agent_max_concurrent_sessions: number;
  /** 市场源 GitHub raw 代理前缀；空串即直连。 */
  market_github_proxy_prefix?: string;
  /** G3 单机单卡互斥适配：执行模式。"multi" 与上游 ArcReel 一致；"exclusive" 启用工作流步骤级 env 前置闸门。 */
  execution_mode?: "multi" | "exclusive";
  /** G3：step-id → 宿主机 env 名映射的 JSON 字符串；空串 = 用 lib.config.dispatcher_schema 的默认 mapping。 */
  env_mapping?: string;
}

export interface SystemConfigOptions {
  video_backends: string[];
  image_backends: string[];
  text_backends: string[];
  audio_backends?: string[];
  provider_names?: Record<string, string>;
  /** "provider_id/model_id" → 按 Accept-Language 成文的模型名；缺键即退回 model id。 */
  model_names?: Record<string, string>;
  /** G3：宿主机上可用的 env 名集合（扫描 /home/dafeng/ai-stack/env/*.env）。 */
  available_envs?: string[];
  /** G3：当前宿主 env 名（读 run/current.env；读不到为 null）。 */
  current_host_env?: string | null;
}

/** 新建 TTS 项目的预填值；未配置任何音频供应商时 audio_backend 为 null。 */
export interface NarrationDefaultsResponse {
  audio_backend: string | null;
  narration_voice: string;
  narration_speed: number | null;
}

export interface TtsModelCapabilitiesResponse {
  supports_speed: boolean;
}

export interface GetSystemConfigResponse {
  settings: SystemConfigSettings;
  options: SystemConfigOptions;
}

/** 任务类型桶键（docs/adr/0054）。代码内部术语，界面文案不直接呈现。 */
export type GenerationTypeBucket = "t2i" | "i2i" | "i2v" | "r2v";

/** 单一 media_type 的候选：默认层全量 + 各任务类型桶按能力过滤后的子集。 */
export interface MediaCandidates {
  default: string[];
  buckets: Partial<Record<GenerationTypeBucket, string[]>>;
}

export interface ModelCandidatesResponse {
  image: MediaCandidates;
  video: MediaCandidates;
  /** 供应商 id → 按 Accept-Language 成文的名字；内置与自定义同一口径。 */
  provider_names: Record<string, string>;
  /** "provider_id/model_id" → 按 Accept-Language 成文的模型名；覆盖全部媒体类型的候选。 */
  model_names: Record<string, string>;
}

export interface SystemVersionReleaseInfo {
  version: string;
  tag_name: string;
  name: string;
  body: string;
  html_url: string;
  published_at: string;
}

export interface GetSystemVersionResponse {
  current: { version: string };
  latest: SystemVersionReleaseInfo | null;
  has_update: boolean;
  checked_at: string;
  update_check_error: string | null;
}

/** 首次使用引导的「已看过」状态 —— 实例级，未设置视为未看过。 */
export interface OnboardingStatus {
  seen: boolean;
}

export interface SystemConfigPatch {
  default_video_backend?: string;
  default_video_backend_i2v?: string;
  default_video_backend_r2v?: string;
  default_image_backend?: string;
  default_image_backend_t2i?: string;
  default_image_backend_i2i?: string;
  default_text_backend?: string;
  default_audio_backend?: string;
  narration_voice?: string;
  narration_speed?: number | null;
  text_backend_simple?: string;
  text_backend_complex?: string;
  video_generate_audio?: boolean;
  video_poll_timeout_seconds?: number;
  anthropic_api_key?: string;
  anthropic_base_url?: string;
  anthropic_model?: string;
  anthropic_default_haiku_model?: string;
  anthropic_default_opus_model?: string;
  anthropic_default_sonnet_model?: string;
  claude_code_subagent_model?: string;
  agent_session_cleanup_delay_seconds?: number;
  agent_max_concurrent_sessions?: number;
  market_github_proxy_prefix?: string;
  /** G3：执行模式。"multi"（默认，零回归）/ "exclusive"（启用 preflight）。 */
  execution_mode?: "multi" | "exclusive";
  /** G3：env_mapping 字符串（空串表示用默认）。 */
  env_mapping?: string;
}

/** ``POST /system/config/test-preflight`` 的响应。 */
export interface PreflightTestResponse {
  current_host_env: string | null;
  available_envs: string[];
  target_env: string | null;
  would_switch: boolean;
  reason: string;
}

