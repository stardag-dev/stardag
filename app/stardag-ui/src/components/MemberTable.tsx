import type { PlanMember } from "../types/task";
import { shortTaskId } from "../utils/ids";
import { qualifiedName } from "../utils/instances";
import { MEMBERSHIP_COLUMN_HELP } from "../utils/membership";
import { MembershipFacts } from "./MembershipFacts";
import { StatusBadge } from "./StatusBadge";
import { Tooltip } from "./ui/Tooltip";

interface MemberTableProps {
  members: PlanMember[];
  selectedTaskId: string | null;
  onSelectTask: (taskId: string) => void;
  page: number;
  pageSize: number;
  onPageChange: (page: number) => void;
}

const HEADER =
  "px-4 py-2 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400";

/** The active plan's members: one row per completion (one instance each). */
export function MemberTable({
  members,
  selectedTaskId,
  onSelectTask,
  page,
  pageSize,
  onPageChange,
}: MemberTableProps) {
  const totalPages = Math.max(1, Math.ceil(members.length / pageSize));
  const rows = members.slice((page - 1) * pageSize, page * pageSize);
  if (members.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-gray-500 dark:text-gray-400">
        No members match.
      </div>
    );
  }
  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-auto">
        <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
          <thead className="sticky top-0 bg-gray-50 dark:bg-gray-800">
            <tr>
              <th className={HEADER}>Task</th>
              <th className={HEADER}>Status</th>
              <th className={HEADER}>
                <Tooltip content={MEMBERSHIP_COLUMN_HELP}>
                  <span>
                    Membership<span aria-hidden="true"> ⓘ</span>
                  </span>
                </Tooltip>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-900">
            {rows.map((member) => (
              <tr
                key={member.instance_id}
                onClick={() => onSelectTask(member.task_id)}
                className={`cursor-pointer ${
                  member.task_id === selectedTaskId
                    ? "bg-blue-50 dark:bg-blue-950/40"
                    : "hover:bg-gray-50 dark:hover:bg-gray-700/50"
                } ${member.excluded_at ? "opacity-60" : ""}`}
              >
                <td className="px-4 py-2">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelectTask(member.task_id);
                    }}
                    className="text-left text-sm font-medium text-gray-900 hover:underline dark:text-gray-100"
                  >
                    {member.task_name
                      ? qualifiedName(member.task_namespace, member.task_name)
                      : "—"}
                  </button>
                  <span
                    className="ml-2 font-mono text-xs text-gray-500 dark:text-gray-400"
                    title={member.task_id}
                  >
                    {shortTaskId(member.task_id)}
                  </span>
                </td>
                <td className="px-4 py-2">
                  <StatusBadge status={member.status} />
                </td>
                <td className="px-4 py-2 text-xs text-gray-600 dark:text-gray-400">
                  <MembershipFacts member={member} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {totalPages > 1 && (
        <div className="flex items-center justify-between border-t border-gray-200 bg-white px-4 py-2 text-sm dark:border-gray-700 dark:bg-gray-800">
          <button
            onClick={() => onPageChange(Math.max(1, page - 1))}
            disabled={page === 1}
            className="rounded-md border border-gray-300 px-3 py-1 text-gray-700 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300"
          >
            Previous
          </button>
          <span className="text-gray-500 dark:text-gray-400">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => onPageChange(Math.min(totalPages, page + 1))}
            disabled={page === totalPages}
            className="rounded-md border border-gray-300 px-3 py-1 text-gray-700 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
