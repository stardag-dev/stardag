import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChangePasswordModal } from "./ChangePasswordModal";

const mockGetAccessToken = vi.fn();
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({
    authMode: "local",
    getAccessToken: mockGetAccessToken,
  }),
}));

vi.mock("../api/auth", () => ({
  changePasswordLocal: vi.fn(),
}));

import { changePasswordLocal } from "../api/auth";

async function fillForm(
  current: string,
  next: string,
  confirm: string,
  user = userEvent.setup(),
) {
  await user.type(screen.getByLabelText("Current password"), current);
  await user.type(screen.getByLabelText("New password"), next);
  await user.type(screen.getByLabelText("Confirm new password"), confirm);
  await user.click(screen.getByRole("button", { name: "Change password" }));
}

describe("ChangePasswordModal", () => {
  beforeEach(() => {
    mockGetAccessToken.mockResolvedValue("session-token-123");
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders the password fields when open", () => {
    render(<ChangePasswordModal isOpen={true} onClose={vi.fn()} />);

    expect(screen.getByLabelText("Current password")).toBeInTheDocument();
    expect(screen.getByLabelText("New password")).toBeInTheDocument();
    expect(screen.getByLabelText("Confirm new password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change password" })).toBeInTheDocument();
  });

  it("renders nothing when closed", () => {
    render(<ChangePasswordModal isOpen={false} onClose={vi.fn()} />);

    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
  });

  it("rejects a too-short new password client-side", async () => {
    render(<ChangePasswordModal isOpen={true} onClose={vi.fn()} />);

    await fillForm("old-password", "short", "short");

    expect(
      await screen.findByText("New password must be at least 8 characters"),
    ).toBeInTheDocument();
    expect(changePasswordLocal).not.toHaveBeenCalled();
  });

  it("rejects mismatched new passwords client-side", async () => {
    render(<ChangePasswordModal isOpen={true} onClose={vi.fn()} />);

    await fillForm("old-password", "new-password-1", "new-password-2");

    expect(await screen.findByText("New passwords do not match")).toBeInTheDocument();
    expect(changePasswordLocal).not.toHaveBeenCalled();
  });

  it("submits and shows a confirmation, then closes", async () => {
    vi.mocked(changePasswordLocal).mockResolvedValue(undefined);
    const onClose = vi.fn();

    // Capture the 1.5s auto-close callback instead of waiting real time.
    // Other setTimeout calls (userEvent, waitFor) pass through untouched.
    let closeCallback: (() => void) | undefined;
    const nativeSetTimeout = window.setTimeout.bind(window);
    const setTimeoutSpy = vi.spyOn(window, "setTimeout").mockImplementation(((
      handler: TimerHandler,
      timeout?: number,
      ...args: unknown[]
    ) => {
      if (timeout === 1500) {
        closeCallback = handler as () => void;
        return 0;
      }
      return nativeSetTimeout(handler, timeout, ...args);
    }) as typeof window.setTimeout);

    try {
      render(<ChangePasswordModal isOpen={true} onClose={onClose} />);

      await fillForm("old-password", "new-password", "new-password");

      expect(
        await screen.findByText("Password changed successfully."),
      ).toBeInTheDocument();
      expect(changePasswordLocal).toHaveBeenCalledWith(
        "old-password",
        "new-password",
        "session-token-123",
      );

      // The close was scheduled with the confirmation delay; run it now
      expect(onClose).not.toHaveBeenCalled();
      expect(closeCallback).toBeDefined();
      act(() => closeCallback!());
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      setTimeoutSpy.mockRestore();
    }
  });

  it("ignores close attempts while the request is in flight", async () => {
    // Keep the request pending for the duration of the test
    vi.mocked(changePasswordLocal).mockImplementation(() => new Promise(() => {}));
    const onClose = vi.fn();
    render(<ChangePasswordModal isOpen={true} onClose={onClose} />);

    const user = userEvent.setup();
    await fillForm("old-password", "new-password", "new-password", user);

    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("shows the API error on wrong current password (401)", async () => {
    vi.mocked(changePasswordLocal).mockRejectedValue(
      new Error("Current password is incorrect"),
    );
    const onClose = vi.fn();
    render(<ChangePasswordModal isOpen={true} onClose={onClose} />);

    await fillForm("wrong-password", "new-password", "new-password");

    expect(
      await screen.findByText("Current password is incorrect"),
    ).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    // Form is still usable for a retry
    expect(screen.getByRole("button", { name: "Change password" })).not.toBeDisabled();
  });
});
