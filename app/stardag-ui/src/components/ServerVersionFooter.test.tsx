import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ServerVersionFooter } from "./ServerVersionFooter";

describe("ServerVersionFooter", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a numeric server version with a v prefix", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ server_version: "0.1.0+1.gabc", api_version: "0.0.1" }),
        { status: 200 },
      ),
    );

    render(<ServerVersionFooter />);
    expect(await screen.findByText("Stardag server v0.1.0+1.gabc")).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][0] as string).toContain("/api/v1/version");
  });

  it("shows a non-numeric version (e.g. dev) without a v prefix", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ server_version: "dev", api_version: "0.0.1" }), {
        status: 200,
      }),
    );

    render(<ServerVersionFooter />);
    expect(await screen.findByText("Stardag server dev")).toBeInTheDocument();
  });

  it("renders nothing while loading", () => {
    fetchMock.mockReturnValueOnce(new Promise(() => {}));
    const { container } = render(<ServerVersionFooter />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the fetch fails", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network down"));
    const { container } = render(<ServerVersionFooter />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing on a non-OK response", async () => {
    fetchMock.mockResolvedValueOnce(new Response("nope", { status: 500 }));
    const { container } = render(<ServerVersionFooter />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
