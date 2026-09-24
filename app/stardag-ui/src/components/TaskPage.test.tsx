import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider } from "../context/BreadcrumbContext";

vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({ activeEnvironment: { id: "env-1" } }),
}));
vi.mock("./TaskDetail", () => ({ TaskDetail: () => null }));

import { TaskPage } from "./TaskPage";

describe("TaskPage", () => {
  it("says task search returns in a later release", () => {
    render(
      <BreadcrumbProvider>
        <TaskPage taskId={null} onOpenTask={() => {}} />
      </BreadcrumbProvider>,
    );
    expect(screen.getByLabelText("Task id")).toBeInTheDocument();
    expect(
      screen.getByText(/Search over task parameters returns in a later release\./),
    ).toBeInTheDocument();
  });
});
