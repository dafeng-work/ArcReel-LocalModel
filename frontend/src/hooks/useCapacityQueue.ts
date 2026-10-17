import { useEffect, useRef } from "react";
import { API } from "@/api";
import { useAssistantStore } from "@/stores/assistant-store";
import { voidCall } from "@/utils/async";

/**
 * 排队快照的轮询间隔。
 *
 * 取 1500ms 而非与服务端一致的 1000ms：客户端不需要比服务端更灵敏（服务端本来就每
 * 1s 重判一次容量），慢一拍换来轮询次数少三成，而位次本来就是弱一致的展示值。
 */
const POLL_INTERVAL_MS = 1500;

/**
 * 并发排队状态的轮询 Hook。
 *
 * **为什么需要轮询而不是 SSE**：排队发生在建会话入口那一刻，会话尚不存在，因而没有
 * 会话 SSE 通道可借——`runtime_status` 走的是每会话绑定的 `managed.channel`。客户端
 * 手里只有那一个阻塞中的 `POST /assistant/sessions/send`，拿不到任何推送。
 *
 * **激活条件**：只在「发送在途且有 clientKey」时轮询。没在发送就没有排队可言；
 * 没有 clientKey 就拿不回自己的位次（服务端按 client_key 登记等待者）。
 *
 * **失败静默**：拉取失败只等下一拍，不弹错误、不写 store。排队反馈是辅助信息，
 * 不该盖过真正的发送失败——那一条走的是 `setError` 那条路。
 *
 * **亮灭判据**：只有服务端回报 `position != null`（确实在排队）才写 store，否则写
 * `null`。容量一开始就够的那条发送路径不该让横幅闪一下。
 */
export function useCapacityQueue(
  projectName: string | null,
  sending: boolean,
  clientKey: string | null
): void {
  // 逐个 selector 取 action：无 selector 的 useAssistantStore() 会订阅整个 store，
  // 而这个 Hook 挂在会话面板上，订阅整店会让每轮轮询都重渲染整棵子树。
  const setQueueStatus = useAssistantStore((s) => s.setQueueStatus);
  // 首帧不发一次空跑：enabled 由 false 变 true 才是「刚点发送」，那一刻立即取一次。
  const prevEnabledRef = useRef(false);

  const enabled = Boolean(projectName) && sending && clientKey != null;

  useEffect(() => {
    if (!enabled || !projectName || clientKey == null) return;
    let alive = true;

    const poll = async () => {
      try {
        const status = await API.getAssistantCapacity(projectName, clientKey);
        if (!alive) return;
        // position 为 null = 服务端那边没在排队（已放行或压根没排过）
        setQueueStatus(status.position != null ? status : null);
      } catch {
        // 静默：下一拍再试，不打断用户
      }
    };

    // 刚进入发送：立即取一次，不等第一个间隔
    if (!prevEnabledRef.current) voidCall(poll());
    prevEnabledRef.current = enabled;

    const timer = setInterval(() => {
      voidCall(poll());
    }, POLL_INTERVAL_MS);

    return () => {
      alive = false;
      clearInterval(timer);
      prevEnabledRef.current = false;
    };
  }, [enabled, projectName, clientKey, setQueueStatus]);

  // 停止发送即清掉排队状态，否则横幅会滞留到下一次发送
  useEffect(() => {
    if (!enabled) setQueueStatus(null);
  }, [enabled, setQueueStatus]);
}
