/**
 * 生成模式工具 — mirrors lib/project/project_manager.py。
 *
 * 生成模式三值 `storyboard | reference_video | mixed`。mixed 为参考家族成员（逐单元自选路线，
 * 不支持批量排队），单向汇点：storyboard / reference_video 项目可单向转入，mixed 不可转出；
 * 宫格是分镜图生视频内的装配选项（`grid_storyboard` 布尔），不改变喂给视频模型的输入契约。
 */

export type GenerationRoute = "storyboard" | "reference_video" | "mixed";

const ROUTES: readonly string[] = ["storyboard", "reference_video", "mixed"];

/**
 * 把未类型化的项目字段解析成生成模式值。
 *
 * 生成模式在持久化数据中必填且恒为三值之一。项目数据未加载或磁盘文件被外部改坏时按
 * storyboard 呈现，让页面可用而不是崩在取文案上。
 */
export function normalizeRoute(value: unknown): GenerationRoute {
  return ROUTES.includes(value as string) ? (value as GenerationRoute) : "storyboard";
}

/**
 * 宫格是否生效 — mirrors lib/project/project_manager.py:grid_storyboard_enabled()。
 * 参考生视频上残留的 `grid_storyboard=true` 不激活宫格。
 */
export function gridStoryboardEnabled(
  project: { generation_mode?: GenerationRoute | null; grid_storyboard?: boolean } | null | undefined,
): boolean {
  if (!project) return false;
  return normalizeRoute(project.generation_mode) === "storyboard" && project.grid_storyboard === true;
}

/**
 * 条目数的文案 key — 名词按生成模式定，与创作类型无关：分镜图生视频报「分镜数」、
 * 参考生视频报「视频单元数」。所有展示条目数的位置读同一份映射，不各自写三元判断。
 *
 * `withStatus` 取带状态后缀的那条（「N 分镜 · 制作中」），否则取裸计数那条。
 */
const ITEM_COUNT_NOUNS: Record<GenerationRoute, string> = {
  storyboard: "storyboard_count",
  reference_video: "video_unit_count",
  // 混合模式条目同样是视频单元，计数名词与参考生视频一致。
  mixed: "video_unit_count",
};

export function itemCountKey(
  route: GenerationRoute,
  { withStatus = false }: { withStatus?: boolean } = {},
): string {
  const noun = ITEM_COUNT_NOUNS[route];
  return withStatus ? `${noun}_and_status` : noun;
}
