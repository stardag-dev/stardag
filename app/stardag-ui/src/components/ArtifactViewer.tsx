import { useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TaskArtifact } from "../types/task";
import { FullscreenModal } from "./FullscreenModal";

// Expand icon component
function ExpandIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg
      className={className}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={1.5}
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15"
      />
    </svg>
  );
}

interface ExpandButtonProps {
  onClick: () => void;
  title?: string;
}

export function ExpandButton({ onClick, title = "Expand" }: ExpandButtonProps) {
  return (
    <button
      onClick={onClick}
      title={title}
      className="rounded p-1 text-gray-400 hover:bg-gray-200 hover:text-gray-600 dark:hover:bg-gray-600 dark:hover:text-gray-300"
    >
      <ExpandIcon />
    </button>
  );
}

interface ArtifactContentProps {
  artifact: TaskArtifact;
  fullscreen?: boolean;
}

function ArtifactContent({ artifact, fullscreen = false }: ArtifactContentProps) {
  const sizeClass = fullscreen ? "prose-base" : "prose-sm";

  if (artifact.artifact_type === "markdown") {
    const content =
      typeof artifact.body?.content === "string" ? artifact.body.content : "";
    return (
      <div className="overflow-auto rounded-md bg-gray-50 p-3 dark:bg-gray-900">
        <div
          className={`prose ${sizeClass} max-w-none prose-headings:mb-2 prose-headings:mt-4 prose-p:my-2 prose-table:my-2 dark:prose-invert`}
        >
          <Markdown remarkPlugins={[remarkGfm]}>{content}</Markdown>
        </div>
      </div>
    );
  }

  // JSON artifact
  return (
    <pre className="overflow-auto rounded-md bg-gray-50 p-3 text-sm text-gray-800 dark:bg-gray-900 dark:text-gray-200">
      {JSON.stringify(artifact.body, null, 2)}
    </pre>
  );
}

interface ArtifactViewerProps {
  artifact: TaskArtifact;
}

export function ArtifactViewer({ artifact }: ArtifactViewerProps) {
  return <ArtifactContent artifact={artifact} />;
}

interface ArtifactListProps {
  artifacts: TaskArtifact[];
}

export function ArtifactList({ artifacts }: ArtifactListProps) {
  const [expandedArtifact, setExpandedArtifact] = useState<TaskArtifact | null>(null);

  if (artifacts.length === 0) {
    return null;
  }

  return (
    <>
      <div className="space-y-4">
        <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
          Artifacts
        </h3>
        <div className="space-y-4">
          {artifacts.map((artifact) => (
            <div
              key={artifact.id}
              className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700"
            >
              <div className="flex items-center justify-between border-b border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-800">
                <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  {artifact.name}
                </span>
                <div className="flex items-center gap-2">
                  <span className="rounded bg-gray-200 px-2 py-0.5 text-xs text-gray-500 dark:bg-gray-700 dark:text-gray-400">
                    {artifact.artifact_type}
                  </span>
                  <ExpandButton
                    onClick={() => setExpandedArtifact(artifact)}
                    title="View fullscreen"
                  />
                </div>
              </div>
              <div className="p-3">
                <ArtifactViewer artifact={artifact} />
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Fullscreen modal */}
      <FullscreenModal
        isOpen={expandedArtifact !== null}
        onClose={() => setExpandedArtifact(null)}
        title={expandedArtifact?.name ?? ""}
      >
        {expandedArtifact && <ArtifactContent artifact={expandedArtifact} fullscreen />}
      </FullscreenModal>
    </>
  );
}
