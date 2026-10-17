import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API } from "@/api";
import { useCapacityQueue } from "@/hooks/useCapacityQueue";
import { useAssistantStore } from "@/stores/assistant-store";
import type { AgentCapacityStatus } from "@/types";

function status(overrides: Partial<AgentCapacityStatus> = {}): AgentCapacityStatus {
  return { active: 1, max_concurrent: 1, waiting: 1, position: 1, queued_seconds: 3, ...overrides };
}

function Harness(props: { projectName: string | null; sending: boolean; clientKey: string | null }) {
  useCapacityQueue(props.projectName, props.sending, props.clientKey);
  return null;
}

beforeEach(() => {
  useAssistantStore.setState(useAssistantStore.getInitialState(), true);
});

afterEach(() => vi.restoreAllMocks());

describe("useCapacityQueue", () => {
  it("发送在途且有 clientKey 时立即拉一次并亮横幅", async () => {
    const spy = vi.spyOn(API, "getAssistantCapacity").mockResolvedValue(status());

    render(<Harness projectName="demo" sending clientKey="ck-1" />);

    await waitFor(() => expect(spy).toHaveBeenCalledWith("demo", "ck-1"));
    await waitFor(() => {
      expect(useAssistantStore.getState().queueStatus?.position).toBe(1);
    });
  });

  it("position 为 null 时不亮横幅（服务端说没在排队）", async () => {
    // 容量一开始就够的发送路径：服务端会回报 position=null，此时不该闪一下横幅
    vi.spyOn(API, "getAssistantCapacity").mockResolvedValue(status({ position: null, waiting: 0 }));

    render(<Harness projectName="demo" sending clientKey="ck-1" />);

    await waitFor(() => expect(API.getAssistantCapacity).toHaveBeenCalled());
    expect(useAssistantStore.getState().queueStatus).toBeNull();
  });

  it("没有 clientKey 时完全不轮询（拿不回自己的位次）", () => {
    const spy = vi.spyOn(API, "getAssistantCapacity").mockResolvedValue(status());

    render(<Harness projectName="demo" sending clientKey={null} />);

    expect(spy).not.toHaveBeenCalled();
    expect(useAssistantStore.getState().queueStatus).toBeNull();
  });

  it("没在发送时不轮询", () => {
    const spy = vi.spyOn(API, "getAssistantCapacity").mockResolvedValue(status());

    render(<Harness projectName="demo" sending={false} clientKey="ck-1" />);

    expect(spy).not.toHaveBeenCalled();
  });

  it("拉取失败时静默——不写 store、不弹错，下一拍再试", async () => {
    vi.spyOn(API, "getAssistantCapacity").mockRejectedValue(new Error("network down"));

    render(<Harness projectName="demo" sending clientKey="ck-1" />);

    await waitFor(() => expect(API.getAssistantCapacity).toHaveBeenCalled());
    expect(useAssistantStore.getState().queueStatus).toBeNull();
    expect(useAssistantStore.getState().error).toBeNull();
  });

  it("停止发送即清掉排队状态，横幅不留残影", async () => {
    vi.spyOn(API, "getAssistantCapacity").mockResolvedValue(status());
    const { rerender } = render(<Harness projectName="demo" sending clientKey="ck-1" />);

    await waitFor(() => {
      expect(useAssistantStore.getState().queueStatus?.position).toBe(1);
    });

    rerender(<Harness projectName="demo" sending={false} clientKey="ck-1" />);

    await waitFor(() => expect(useAssistantStore.getState().queueStatus).toBeNull());
  });

  it("卸载后停轮询，不再发请求", async () => {
    const spy = vi.spyOn(API, "getAssistantCapacity").mockResolvedValue(status());
    const { unmount } = render(<Harness projectName="demo" sending clientKey="ck-1" />);

    await waitFor(() => expect(spy).toHaveBeenCalled());
    const callsBefore = spy.mock.calls.length;
    unmount();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1700));
    });
    expect(spy.mock.calls.length).toBe(callsBefore);
  });
});
