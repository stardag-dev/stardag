import { afterEach, describe, expect, it, vi } from "vitest";
import { formatAbsoluteTime, formatDuration, formatRelativeTime } from "./time";

// Fixed local instant, so the assertions below read as wall-clock time in
// whatever zone the suite happens to run in.
const NOW = new Date(2026, 7, 9, 14, 32, 7); // 2026-08-09 14:32:07 local

function at(offsetMs: number): string {
  return new Date(NOW.getTime() - offsetMs).toISOString();
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

afterEach(() => {
  vi.useRealTimers();
});

function freeze() {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
}

describe("formatDuration", () => {
  it("breaks out days once an interval passes 24h", () => {
    freeze();
    // The case that motivated this: a build abandoned two weeks ago used
    // to render as "371h 41m".
    expect(formatDuration(at(15 * DAY + 11 * HOUR + 41 * MINUTE), null)).toBe(
      "15d 11h 41m",
    );
  });

  it("keeps the finer units below a day", () => {
    freeze();
    expect(formatDuration(at(45_000), null)).toBe("45s");
    expect(formatDuration(at(12 * MINUTE + 3_000), null)).toBe("12m 3s");
    expect(formatDuration(at(2 * HOUR + 7 * MINUTE), null)).toBe("2h 07m");
  });

  it("switches units exactly at the day boundary", () => {
    freeze();
    expect(formatDuration(at(DAY - MINUTE), null)).toBe("23h 59m");
    expect(formatDuration(at(DAY), null)).toBe("1d 0h 00m");
  });
});

describe("date rendering", () => {
  it("uses YYYY-MM-DD rather than a locale-dependent order", () => {
    freeze();
    expect(formatRelativeTime(at(30 * DAY))).toBe("2026-07-10");
  });

  it("renders absolute timestamps as YYYY-MM-DD HH:MM:SS", () => {
    expect(formatAbsoluteTime(NOW.toISOString())).toBe("2026-08-09 14:32:07");
  });

  it("dates in the viewer's timezone, not UTC", () => {
    // 23:30 local on the 9th is the 10th in UTC for anyone east of
    // Greenwich; the date shown must be the one on the viewer's wall.
    const lateEvening = new Date(2026, 7, 9, 23, 30, 0);
    expect(formatAbsoluteTime(lateEvening.toISOString())).toBe("2026-08-09 23:30:00");
  });

  it("still prefers relative wording inside a week", () => {
    freeze();
    expect(formatRelativeTime(at(3 * DAY))).toBe("3d ago");
    expect(formatRelativeTime(at(5 * HOUR))).toBe("5h ago");
  });

  it("has one rendering for a missing timestamp", () => {
    expect(formatRelativeTime(null)).toBe("—");
    expect(formatAbsoluteTime(null)).toBeUndefined();
  });
});
