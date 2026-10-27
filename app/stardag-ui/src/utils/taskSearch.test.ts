import { describe, expect, it } from "vitest";
import {
  classifyQuery,
  EMPTY_SEARCH,
  formatSearch,
  isFullTaskId,
  normaliseIdPrefix,
  parseSearch,
  toTaskFilters,
  withTag,
  withoutTag,
} from "./taskSearch";

const TASK_ID = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";

describe("URL state", () => {
  it("round-trips every filter through the query string", () => {
    const state = {
      text: "train",
      namespace: "examples.ml",
      name: "Train",
      tags: ["baseline", "prod"],
      status: "completed" as const,
    };
    const query = formatSearch(state);
    expect(query).toBe(
      "?q=train&namespace=examples.ml&name=Train&tag=baseline&tag=prod&status=completed",
    );
    expect(parseSearch(query)).toEqual(state);
  });

  it("formats the empty search as no query string", () => {
    expect(formatSearch(EMPTY_SEARCH)).toBe("");
    expect(parseSearch("")).toEqual(EMPTY_SEARCH);
  });

  it("keeps the root namespace apart from no namespace filter", () => {
    const root = { ...EMPTY_SEARCH, namespace: "" };
    expect(formatSearch(root)).toBe("?namespace=");
    expect(parseSearch("?namespace=").namespace).toBe("");
    expect(parseSearch("?q=x").namespace).toBeNull();
  });

  it("drops an unknown status and dedupes and sorts tags", () => {
    const state = parseSearch("?status=bogus&tag=b&tag=a&tag=b&tag=%20");
    expect(state.status).toBeNull();
    expect(state.tags).toEqual(["a", "b"]);
  });

  it("adds and removes tag filters, sorted and without duplicates", () => {
    let state = withTag(EMPTY_SEARCH, "zeta");
    state = withTag(state, " alpha ");
    expect(withTag(state, "alpha")).toBe(state);
    expect(state.tags).toEqual(["alpha", "zeta"]);
    expect(withoutTag(state, "zeta").tags).toEqual(["alpha"]);
  });
});

describe("the search box", () => {
  it("reads words as free text", () => {
    expect(classifyQuery("train model")).toEqual({ kind: "text", q: "train model" });
    // Hex-only words stay text unless they are id-long.
    expect(classifyQuery("face")).toEqual({ kind: "text", q: "face" });
    expect(classifyQuery("  ")).toEqual({ kind: "empty" });
  });

  it("reads something id-like as a task-id prefix", () => {
    expect(classifyQuery("df0c")).toEqual({ kind: "id", idPrefix: "df0c" });
    expect(classifyQuery("DF0C8B03")).toEqual({ kind: "id", idPrefix: "df0c8b03" });
    expect(classifyQuery(TASK_ID)).toEqual({ kind: "id", idPrefix: TASK_ID });
    // Too short to be worth an id search.
    expect(classifyQuery("df0")).toEqual({ kind: "text", q: "df0" });
  });

  it("forces the id reading with id:", () => {
    expect(classifyQuery("id: abc")).toEqual({ kind: "id", idPrefix: "abc" });
  });

  it("re-dashes an id pasted without dashes", () => {
    expect(normaliseIdPrefix(TASK_ID.replace(/-/g, ""))).toBe(TASK_ID);
    expect(normaliseIdPrefix("df0c8b03fab2")).toBe("df0c8b03-fab2");
    expect(normaliseIdPrefix("df0c8b03-")).toBe("df0c8b03-");
    expect(normaliseIdPrefix("not hex")).toBeNull();
    expect(isFullTaskId(TASK_ID.toUpperCase())).toBe(true);
    expect(isFullTaskId("df0c8b03")).toBe(false);
  });
});

describe("filters → GET /tasks", () => {
  it("maps text to q and every filter to its parameter", () => {
    expect(
      toTaskFilters({
        text: "train",
        namespace: "",
        name: "Train",
        tags: ["prod"],
        status: "failed",
      }),
    ).toEqual({
      q: "train",
      namespace: "",
      name: "Train",
      tags: ["prod"],
      status: "failed",
    });
  });

  it("maps an id-like text to id_prefix, not q", () => {
    expect(toTaskFilters({ ...EMPTY_SEARCH, text: "df0c8b03" })).toEqual({
      idPrefix: "df0c8b03",
    });
  });

  it("sends nothing for the empty search", () => {
    expect(toTaskFilters(EMPTY_SEARCH)).toEqual({});
  });
});
