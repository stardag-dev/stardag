import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { BuildFailureReason } from "./BuildFailureReason";

// A real one, abbreviated: the reactive scheduler's reasons name the blocked
// task, the blocker, its status and age, the owning build, why nothing will
// move it, and the remedy.
const REASON =
  "Build cannot progress: it has nothing runnable or running of its own, and 1 " +
  "of its task(s) are blocked by an upstream that nothing is going to move " +
  "(status counts: {'pending': 1}). Blocked by: task abc is blocked by " +
  "pipelines.Ingest (def), FAILED for 1m under build 019fed54 — a result rather " +
  "than a revocation, so this build's fail_mode owns the outcome. Re-trigger " +
  "this build to reset the blocker and run it here.";

describe("BuildFailureReason", () => {
  it("shows the recorded reason for a failed build", () => {
    render(<BuildFailureReason status="failed" message={REASON} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Why this build failed")).toBeInTheDocument();
    // The whole reason, not a truncation — the remedy is at the end of it.
    expect(
      screen.getByText(/Re-trigger this build to reset the blocker/),
    ).toBeInTheDocument();
  });

  it("says nothing when the build is not failed", () => {
    // A build resumed after failing is running again. Pairing that status with
    // the previous round's reason is worse than showing no reason, which is why
    // the API stops reporting it — but the component must not depend on that.
    render(<BuildFailureReason status="running" message={REASON} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says nothing for a whitespace-only reason", () => {
    // The server excludes blank reasons, so this guards the slip rather than
    // today's behaviour: a heading with nothing under it is worse than silence.
    render(<BuildFailureReason status="failed" message={"  \n "} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says nothing when no reason was recorded", () => {
    // A server predating `latest_error_message` omits the field. An empty
    // banner headed "Why this build failed" would be worse than no banner.
    render(<BuildFailureReason status="failed" message={null} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    render(<BuildFailureReason status="failed" />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("collapses to a dated record once the roots have completed anyway", async () => {
    // The reason is history at this point, not a call to action — the build's
    // work got done elsewhere. It must stay reachable, though: it is the only
    // account of why this build stopped, and the message embeds the status
    // counts *as they were*, which is exactly what makes an unlabelled one
    // misleading.
    const user = userEvent.setup();
    render(
      <BuildFailureReason
        status="failed"
        message={REASON}
        failedAt="2026-08-06T20:24:37Z"
        superseded
      />,
    );

    // Not shouting: no alert role, and the reason is not on screen yet.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/Re-trigger this build to reset the blocker/)).toBeNull();

    // But it is one click away, and dated so it reads as a past event.
    const toggle = screen.getByRole("button", {
      name: /Why it was recorded as failed/,
    });
    expect(toggle).toHaveTextContent(/2026/);
    await user.click(toggle);
    expect(
      await screen.findByText(/Re-trigger this build to reset the blocker/),
    ).toBeInTheDocument();
  });
});
