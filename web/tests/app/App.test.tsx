import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { App } from "@/app/App";
import { GatewayUnreachableError } from "@/api/errors";

import { FakeGateway, json } from "../gateway";

describe("App", () => {
  it("shows that the Gateway is connected", async () => {
    const gateway = new FakeGateway().on(
      "GET /api/sessions",
      json({ items: [], next_cursor: null }),
    );
    render(<App client={gateway.client} />);
    expect(screen.getByRole("heading", { name: "Mertina" })).toBeInTheDocument();
    expect(await screen.findByText("Gateway 已连接")).toBeInTheDocument();
  });

  it("explains how to start the Gateway and reconnects on request", async () => {
    const gateway = new FakeGateway().on(
      "GET /api/sessions",
      new GatewayUnreachableError("down"),
      json({ items: [], next_cursor: null }),
    );
    render(<App client={gateway.client} />);
    expect(await screen.findByText("未连接 Gateway")).toBeInTheDocument();
    expect(screen.getByText(/uv run mertina gateway/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重新连接" }));
    expect(await screen.findByText("Gateway 已连接")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新连接" })).not.toBeInTheDocument();
  });
});
