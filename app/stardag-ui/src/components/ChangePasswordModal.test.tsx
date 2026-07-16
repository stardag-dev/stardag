import { render, screen, waitFor } from "@testing-library/react";
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

async function fillForm(current: string, next: string, confirm: string) {
  const user = userEvent.setup();
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
    // Closes after the brief confirmation
    await waitFor(() => expect(onClose).toHaveBeenCalled(), { timeout: 3000 });
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
