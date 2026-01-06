import Markdown from "react-markdown";
import type { TaskAsset } from "../types/task";

interface AssetViewerProps {
  asset: TaskAsset;
}

export function AssetViewer({ asset }: AssetViewerProps) {
  if (asset.asset_type === "markdown") {
    // Markdown body is stored as { content: "<markdown string>" }
    const content = typeof asset.body?.content === "string" ? asset.body.content : "";
    return (
      <div className="prose prose-sm dark:prose-invert max-w-none">
        <Markdown>{content}</Markdown>
      </div>
    );
  }

  // JSON asset - body is the actual data dict
  return (
    <pre className="overflow-auto rounded-md bg-gray-50 dark:bg-gray-900 p-3 text-sm text-gray-800 dark:text-gray-200">
      {JSON.stringify(asset.body, null, 2)}
    </pre>
  );
}

interface AssetListProps {
  assets: TaskAsset[];
}

export function AssetList({ assets }: AssetListProps) {
  if (assets.length === 0) {
    return null;
  }

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Assets</h3>
      <div className="space-y-4">
        {assets.map((asset) => (
          <div
            key={asset.id}
            className="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden"
          >
            <div className="bg-gray-50 dark:bg-gray-800 px-3 py-2 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {asset.name}
              </span>
              <span className="text-xs text-gray-500 dark:text-gray-400 px-2 py-0.5 bg-gray-200 dark:bg-gray-700 rounded">
                {asset.asset_type}
              </span>
            </div>
            <div className="p-3">
              <AssetViewer asset={asset} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
