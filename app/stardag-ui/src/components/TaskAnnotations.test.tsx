import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskSummary } from "../types/task";

vi.mock("../api/registry", () => ({ annotateTask: vi.fn() }));

import { annotateTask } from "../api/registry";
import { TaskAnnotations } from "./TaskAnnotations";

const mocked = vi.mocked(annotateTask);

function saved(tags: string[], description: string | null = null): TaskSummary {
  return { task_id: "t-1", tags, description } as TaskSummary;
}

function setup(tags: string[] = ["prod"], description: string | null = null) {
  const onSaved = vi.fn();
  const view = render(
    <TaskAnnotations
      taskId="t-1"
      environmentId="env-1"
      tags={tags}
      description={description}
      onSaved={onSaved}
    />,
  );
  return { onSaved, ...view };
}

beforeEach(() => {
  mocked.mockReset();
});

describe("TaskAnnotations", () => {
  it("adds tags optimistically and saves the whole normalised set", async () => {
    let resolve: (task: TaskSummary) => void = () => {};
    mocked.mockReturnValue(new Promise((r) => (resolve = r)));
    const { onSaved } = setup(["prod"]);
    fireEvent.click(screen.getByRole("button", { name: "+ Add tag" }));
    const input = screen.getByRole("textbox", { name: "New tag" });
    fireEvent.change(input, { target: { value: " baseline , prod" } });
    fireEvent.keyDown(input, { key: "Enter" });
    // Shown before the server answers.
    expect(screen.getByText("baseline")).toBeInTheDocument();
    expect(screen.getByText("Saving…")).toBeInTheDocument();
    await waitFor(() =>
      expect(mocked).toHaveBeenCalledWith("t-1", "env-1", {
        tags: ["baseline", "prod"],
      }),
    );
    resolve(saved(["baseline", "prod"]));
    await waitFor(() =>
      expect(onSaved).toHaveBeenCalledWith(saved(["baseline", "prod"])),
    );
  });

  it("refuses an invalid tag without a request", () => {
    setup([]);
    fireEvent.click(screen.getByRole("button", { name: "+ Add tag" }));
    const input = screen.getByRole("textbox", { name: "New tag" });
    fireEvent.change(input, { target: { value: "x".repeat(65) } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByRole("alert")).toHaveTextContent("at most 64 characters");
    expect(mocked).not.toHaveBeenCalled();
  });

  it("rolls a removal back when the save is refused", async () => {
    mocked.mockRejectedValue(new Error("forbidden"));
    const { onSaved } = setup(["prod", "baseline"]);
    fireEvent.click(screen.getByRole("button", { name: "Remove tag prod" }));
    expect(screen.queryByText("prod")).toBeNull();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not save: forbidden",
    );
    expect(screen.getByText("prod")).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("reports a refused save even when a later one succeeds", async () => {
    mocked
      .mockRejectedValueOnce(new Error("rate limited"))
      .mockResolvedValueOnce(saved(["prod"], "described"));
    const { onSaved } = setup(["prod"], null);
    fireEvent.click(screen.getByRole("button", { name: "+ Add tag" }));
    const input = screen.getByRole("textbox", { name: "New tag" });
    fireEvent.change(input, { target: { value: "extra" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Add a description…" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Description" }), {
      target: { value: "described" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(screen.getByRole("alert")).toHaveTextContent("Could not save: rate limited");
  });

  it("edits a multi-line description and clears it when emptied", async () => {
    mocked.mockImplementation(async (_id, _env, changes) =>
      saved(["prod"], changes.description ?? null),
    );
    const { onSaved, rerender } = setup(["prod"], null);
    fireEvent.click(screen.getByRole("button", { name: "Add a description…" }));
    const box = screen.getByRole("textbox", { name: "Description" });
    fireEvent.change(box, { target: { value: "  line one\nline two  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mocked).toHaveBeenLastCalledWith("t-1", "env-1", {
        description: "line one\nline two",
      }),
    );
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    rerender(
      <TaskAnnotations
        taskId="t-1"
        environmentId="env-1"
        tags={["prod"]}
        description="line one\nline two"
        onSaved={onSaved}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Description" }), {
      target: { value: "   " },
    });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Description" }), {
      key: "Enter",
      ctrlKey: true,
    });
    await waitFor(() =>
      expect(mocked).toHaveBeenLastCalledWith("t-1", "env-1", { description: null }),
    );
  });

  it("cancels a description edit with Escape", () => {
    setup([], "kept");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByRole("textbox", { name: "Description" });
    fireEvent.change(box, { target: { value: "changed" } });
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.getByText("kept")).toBeInTheDocument();
    expect(mocked).not.toHaveBeenCalled();
  });
});
