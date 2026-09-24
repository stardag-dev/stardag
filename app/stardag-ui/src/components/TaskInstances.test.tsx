import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Deployment, TaskInstance } from "../types/task";
import { TaskInstances } from "./TaskInstances";

const DEPLOYMENT: Deployment = {
  id: "dep-1",
  kind: "modal",
  app_name: "etl",
  code_id: "c1",
  image_id: null,
  modal_app_id: null,
  generation: 3,
  deployed_at: "2026-09-24T00:00:00Z",
  activated_at: "2026-09-24T00:00:01Z",
  is_current: true,
};

function instance(id: string, log: string, hash: string): TaskInstance {
  return {
    id,
    deployment_id: "dep-1",
    settings_hash: "a".repeat(64),
    instance_hash: hash,
    body: { __namespace: "demo", __name: "Train", epochs: 3, log },
    expanded_at: "2026-09-24T00:00:02Z",
    created_at: "2026-09-24T00:00:02Z",
  };
}

describe("TaskInstances", () => {
  it("shows each instance under its scope and names what differs", () => {
    render(
      <TaskInstances
        instances={[
          instance("i-2", "debug", "h2".repeat(8)),
          instance("i-1", "info", "h1".repeat(8)),
        ]}
        deploymentsById={new Map([["dep-1", DEPLOYMENT]])}
        planInstanceId="i-1"
      />,
    );
    expect(
      screen.getByText(/2 instances of this completion, differing in log/),
    ).toBeInTheDocument();
    expect(screen.getAllByText("etl gen 3")).toHaveLength(2);
    // The instance hash is printed only beside its scope label.
    expect(screen.getAllByText("instance hash")).toHaveLength(2);
    expect(screen.getByText("in this plan")).toBeInTheDocument();
    // Parameters without the discriminator keys.
    expect(screen.queryByText(/__name/)).not.toBeInTheDocument();
  });
});
