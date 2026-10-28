import { useEffect, useId, useRef, useState } from "react";
import { annotateTask } from "../api/registry";
import type {
  TaskAnnotations as TaskAnnotationChanges,
  TaskSummary,
} from "../types/task";
import {
  addTag,
  checkDescription,
  codePointLength,
  MAX_DESCRIPTION_LENGTH,
  MAX_TAGS,
} from "../utils/annotations";
import { TagChip } from "./ui/TagChip";

interface TaskAnnotationsProps {
  taskId: string;
  environmentId: string;
  tags: string[];
  description: string | null;
  // The task as stored after a save, for the parent to take over.
  onSaved: (task: TaskSummary) => void;
  // Filter the task search by a tag, when the page offers it.
  onSearchTag?: (tag: string) => void;
}

const SMALL_BUTTON =
  "rounded-md px-2 py-1 text-xs font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500";
const PRIMARY = `${SMALL_BUTTON} bg-blue-600 text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50`;
const SECONDARY = `${SMALL_BUTTON} text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700`;

/**
 * A completion's tags and description, edited in place
 * (`PATCH /tasks/{id}`). Edits show at once and roll back if the save is
 * refused; tags are validated as the server normalises them, so a refusal
 * is rare. Annotations describe the asset and sit outside every hash:
 * editing them never changes what the task is.
 */
export function TaskAnnotations({
  taskId,
  environmentId,
  tags,
  description,
  onSaved,
  onSearchTag,
}: TaskAnnotationsProps) {
  // Optimistic values while a save is in flight; null = show the props.
  const [pendingTags, setPendingTags] = useState<string[] | null>(null);
  const [pendingDescription, setPendingDescription] = useState<{
    value: string | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [addingTag, setAddingTag] = useState(false);
  const [tagInput, setTagInput] = useState("");
  const [tagError, setTagError] = useState<string | null>(null);
  const [editingDescription, setEditingDescription] = useState(false);
  const [draft, setDraft] = useState("");
  const seqRef = useRef(0);
  const chainRef = useRef<Promise<unknown>>(Promise.resolve());
  const tagInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const descriptionId = useId();

  // A different task: drop every edit in progress.
  const [shownTask, setShownTask] = useState(taskId);
  if (shownTask !== taskId) {
    setShownTask(taskId);
    setPendingTags(null);
    setPendingDescription(null);
    setError(null);
    setAddingTag(false);
    setTagInput("");
    setTagError(null);
    setEditingDescription(false);
  }

  useEffect(() => {
    if (addingTag) tagInputRef.current?.focus();
  }, [addingTag]);
  useEffect(() => {
    if (editingDescription) textareaRef.current?.focus();
  }, [editingDescription]);

  const shownTags = pendingTags ?? tags;
  const shownDescription = pendingDescription ? pendingDescription.value : description;

  // Saves run one after another: each sends the whole tag set, so the last
  // one sent is the one the server keeps. Every success hands the stored
  // task up; only the latest save's outcome clears the optimistic view.
  const save = (changes: TaskAnnotationChanges) => {
    const seq = ++seqRef.current;
    // Only a save with nothing queued before it starts from a clean slate:
    // an earlier save may still be refused.
    if (pendingTags === null && pendingDescription === null) setError(null);
    if (changes.tags !== undefined) setPendingTags(changes.tags);
    if (changes.description !== undefined) {
      setPendingDescription({ value: changes.description });
    }
    chainRef.current = chainRef.current
      .then(() => annotateTask(taskId, environmentId, changes))
      .then(
        (saved) => {
          onSaved(saved);
          if (seq !== seqRef.current) return;
          setPendingTags(null);
          setPendingDescription(null);
        },
        (err: unknown) => {
          // Always say a save was refused, even when a later one is queued:
          // a later success must not silently swallow it.
          setError(
            `Could not save: ${err instanceof Error ? err.message : String(err)}`,
          );
          if (seq !== seqRef.current) return;
          // Roll back to what the server holds.
          setPendingTags(null);
          setPendingDescription(null);
        },
      );
  };

  const submitTag = () => {
    if (!tagInput.trim()) {
      setAddingTag(false);
      setTagError(null);
      return;
    }
    // Several at once, comma separated.
    let next = shownTags;
    for (const raw of tagInput.split(",")) {
      if (!raw.trim()) continue;
      const result = addTag(next, raw);
      if (!result.ok) {
        setTagError(result.error);
        return;
      }
      next = result.tags;
    }
    setTagError(null);
    setTagInput("");
    if (next !== shownTags) save({ tags: next });
  };

  const removeTag = (tag: string) => save({ tags: shownTags.filter((t) => t !== tag) });

  const startEditingDescription = () => {
    setDraft(shownDescription ?? "");
    setEditingDescription(true);
  };

  const draftCheck = checkDescription(draft);
  const draftLength = codePointLength(draft.trim());

  const submitDescription = () => {
    if (!draftCheck.ok) return;
    setEditingDescription(false);
    if (draftCheck.description !== (shownDescription ?? null)) {
      save({ description: draftCheck.description });
    }
  };

  const saving = pendingTags !== null || pendingDescription !== null;

  return (
    <div className="space-y-3">
      <div>
        <div className="mb-1 flex items-center gap-2">
          <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Tags</h3>
          {saving && (
            <span role="status" className="text-xs text-gray-400 dark:text-gray-500">
              Saving…
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {shownTags.map((tag) => (
            <TagChip
              key={tag}
              tag={tag}
              pending={pendingTags !== null}
              onClick={onSearchTag ? () => onSearchTag(tag) : undefined}
              clickLabel={onSearchTag ? `Search tasks tagged ${tag}` : undefined}
              onRemove={() => removeTag(tag)}
              removeLabel={`Remove tag ${tag}`}
            />
          ))}
          {addingTag ? (
            <input
              ref={tagInputRef}
              aria-label="New tag"
              aria-invalid={tagError ? true : undefined}
              aria-describedby={tagError ? `${descriptionId}-tag-error` : undefined}
              placeholder="tag, another…"
              value={tagInput}
              onChange={(e) => {
                setTagInput(e.target.value);
                setTagError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submitTag();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  setAddingTag(false);
                  setTagInput("");
                  setTagError(null);
                }
              }}
              onBlur={() => {
                if (!tagInput.trim()) {
                  setAddingTag(false);
                  setTagError(null);
                }
              }}
              className="w-40 rounded-full border border-gray-300 px-2 py-0.5 text-xs text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
            />
          ) : shownTags.length < MAX_TAGS ? (
            <button
              type="button"
              onClick={() => setAddingTag(true)}
              className="rounded-full border border-dashed border-gray-300 px-2 py-0.5 text-[11px] text-gray-500 hover:border-blue-400 hover:text-blue-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:text-gray-400 dark:hover:text-blue-400"
            >
              + Add tag
            </button>
          ) : null}
          {shownTags.length === 0 && !addingTag && (
            <span className="text-xs text-gray-400 dark:text-gray-500">No tags</span>
          )}
        </div>
        {tagError && (
          <p
            id={`${descriptionId}-tag-error`}
            role="alert"
            className="mt-1 text-xs text-red-600 dark:text-red-400"
          >
            {tagError}
          </p>
        )}
      </div>

      <div>
        <div className="mb-1 flex items-center gap-2">
          <h3
            id={`${descriptionId}-label`}
            className="text-sm font-medium text-gray-500 dark:text-gray-400"
          >
            Description
          </h3>
          {!editingDescription && shownDescription && (
            <button
              type="button"
              onClick={startEditingDescription}
              className={`${SMALL_BUTTON} px-1 py-0 text-blue-600 hover:underline dark:text-blue-400`}
            >
              Edit
            </button>
          )}
        </div>
        {editingDescription ? (
          <div className="space-y-1.5">
            <textarea
              ref={textareaRef}
              aria-labelledby={`${descriptionId}-label`}
              aria-invalid={!draftCheck.ok ? true : undefined}
              value={draft}
              rows={Math.min(12, Math.max(3, draft.split("\n").length + 1))}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  submitDescription();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  setEditingDescription(false);
                }
              }}
              placeholder="What this task is, what it is for…"
              className="w-full rounded-md border border-gray-300 px-2 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
            />
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={submitDescription}
                disabled={!draftCheck.ok}
                className={PRIMARY}
              >
                Save
              </button>
              <button
                type="button"
                onClick={() => setEditingDescription(false)}
                className={SECONDARY}
              >
                Cancel
              </button>
              <span className="text-xs text-gray-400 dark:text-gray-500">
                ⌘/Ctrl+Enter to save, Esc to cancel
              </span>
              <span
                className={`ml-auto text-xs ${
                  draftCheck.ok
                    ? "text-gray-400 dark:text-gray-500"
                    : "text-red-600 dark:text-red-400"
                }`}
              >
                {draftLength > MAX_DESCRIPTION_LENGTH * 0.8 || !draftCheck.ok
                  ? `${draftLength} / ${MAX_DESCRIPTION_LENGTH}`
                  : ""}
              </span>
            </div>
          </div>
        ) : shownDescription ? (
          <p
            className={`whitespace-pre-wrap break-words text-sm text-gray-900 dark:text-gray-100 ${
              pendingDescription ? "opacity-60" : ""
            }`}
          >
            {shownDescription}
          </p>
        ) : (
          <button
            type="button"
            onClick={startEditingDescription}
            className="rounded text-sm text-gray-400 hover:text-blue-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-500 dark:hover:text-blue-400"
          >
            Add a description…
          </button>
        )}
      </div>

      {error && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}
