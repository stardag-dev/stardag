import type { EventType } from "../types/task";

// "task_structure_diverged" -> "Structure Diverged", as v1 formatted them.
export function formatEventType(eventType: EventType): string {
  return eventType
    .replace(/^task_/, "")
    .replace(/^build_/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function eventTypeStyle(eventType: EventType): string {
  if (eventType.includes("completed") || eventType.includes("observed_complete")) {
    return "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400";
  }
  if (eventType.includes("failed") || eventType.includes("diverged")) {
    return "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400";
  }
  if (eventType.includes("started") || eventType.includes("resumed")) {
    return "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400";
  }
  if (
    eventType.includes("cancelled") ||
    eventType.includes("skipped") ||
    eventType.includes("excluded")
  ) {
    return "bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400";
  }
  if (eventType.includes("suspended") || eventType.includes("invalidated")) {
    return "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400";
  }
  // Orange, matching StatusBadge's interrupted: the platform ended the
  // execution, so nothing is wrong and nothing is done.
  if (eventType.includes("interrupted") || eventType.includes("preempted")) {
    return "bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400";
  }
  return "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400";
}
