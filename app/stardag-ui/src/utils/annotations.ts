/**
 * Task annotations — tags and a description — validated the way the
 * registry normalises them (`PATCH /tasks/{id}`), so the editor refuses
 * what the server would refuse, before the round trip.
 *
 * The server's rules: a tag is trimmed, non-empty, at most
 * {@link MAX_TAG_LENGTH} characters and free of control characters; a set
 * is deduplicated, sorted and at most {@link MAX_TAGS} long. Case is kept
 * (`Prod` and `prod` are two tags). A description is trimmed, at most
 * {@link MAX_DESCRIPTION_LENGTH} characters, and empty means cleared.
 * Lengths count code points, as Python's `len` does.
 */

import { compareCodePoints } from "./taskSearch";

export const MAX_TAGS = 32;
export const MAX_TAG_LENGTH = 64;
export const MAX_DESCRIPTION_LENGTH = 4096;

// Unicode category Cc: C0 controls, DEL and C1 controls.
// eslint-disable-next-line no-control-regex
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f-\u009f]/;

export function codePointLength(value: string): number {
  return Array.from(value).length;
}

export type TagCheck = { ok: true; tag: string } | { ok: false; error: string };

/** One tag, trimmed, or why the server would refuse it. */
export function checkTag(raw: string): TagCheck {
  const tag = raw.trim();
  if (!tag) return { ok: false, error: "A tag must not be empty." };
  if (codePointLength(tag) > MAX_TAG_LENGTH) {
    return { ok: false, error: `A tag is at most ${MAX_TAG_LENGTH} characters.` };
  }
  if (CONTROL_CHARACTER.test(tag)) {
    return { ok: false, error: "A tag must not contain control characters." };
  }
  return { ok: true, tag };
}

export type TagsCheck = { ok: true; tags: string[] } | { ok: false; error: string };

/**
 * Adding `raw` to `current`: the new set as the server will store it, or
 * why it would be refused. Adding a tag already there is a no-op (`ok`
 * with the same set).
 */
export function addTag(current: string[], raw: string): TagsCheck {
  const check = checkTag(raw);
  if (!check.ok) return check;
  if (current.includes(check.tag)) return { ok: true, tags: current };
  if (current.length >= MAX_TAGS) {
    return { ok: false, error: `A task carries at most ${MAX_TAGS} tags.` };
  }
  return { ok: true, tags: [...current, check.tag].sort(compareCodePoints) };
}

export type DescriptionCheck =
  | { ok: true; description: string | null }
  | { ok: false; error: string };

/** The description as the server will store it (`null` = cleared). */
export function checkDescription(raw: string): DescriptionCheck {
  const description = raw.trim();
  if (codePointLength(description) > MAX_DESCRIPTION_LENGTH) {
    return {
      ok: false,
      error: `A description is at most ${MAX_DESCRIPTION_LENGTH} characters.`,
    };
  }
  return { ok: true, description: description || null };
}
