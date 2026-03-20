import {
  ReactFlow,
  Background,
  Controls,
  Panel,
  useReactFlow,
  type Node,
  type Edge,
  useNodesState,
  useEdgesState,
  type NodeTypes,
  type ColorMode,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import Dagre from "@dagrejs/dagre";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTheme } from "../context/ThemeContext";
import type {
  TaskWithContext,
  TaskGraphResponse,
  TaskGraphExtendedResponse,
  GroupSummary,
} from "../types/task";
import { isExtendedResponse } from "../types/task";
import { BatchNode, type BatchNodeData } from "./BatchNode";
import { TaskNode, type TaskNodeData } from "./TaskNode";
import {
  createPositionCache,
  type LayoutDirection,
  type PositionCache,
} from "./dagLayout";

export type { LayoutDirection } from "./dagLayout";

interface LayoutToggleProps {
  direction: LayoutDirection;
  onDirectionChange: (direction: LayoutDirection) => void;
  onResetLayout: () => void;
}

function LayoutToggle({
  direction,
  onDirectionChange,
  onResetLayout,
}: LayoutToggleProps) {
  const { fitView } = useReactFlow();

  const handleDirectionChange = useCallback(
    (newDirection: LayoutDirection) => {
      onDirectionChange(newDirection);
      setTimeout(() => fitView({ padding: 0.2 }), 50);
    },
    [onDirectionChange, fitView],
  );

  const handleResetLayout = useCallback(() => {
    onResetLayout();
    setTimeout(() => fitView({ padding: 0.2 }), 50);
  }, [onResetLayout, fitView]);

  return (
    <Panel position="top-right">
      <div className="flex items-center gap-1 rounded-md border border-gray-200 bg-white p-1 shadow-sm dark:border-gray-700 dark:bg-gray-800">
        <button
          onClick={() => handleDirectionChange("LR")}
          className={`rounded px-2 py-1 text-xs font-medium transition-colors ${
            direction === "LR"
              ? "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
              : "text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
          }`}
          title="Left to Right"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M14 5l7 7m0 0l-7 7m7-7H3"
            />
          </svg>
        </button>
        <button
          onClick={() => handleDirectionChange("TB")}
          className={`rounded px-2 py-1 text-xs font-medium transition-colors ${
            direction === "TB"
              ? "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
              : "text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
          }`}
          title="Top to Bottom"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M19 14l-7 7m0 0l-7-7m7 7V3"
            />
          </svg>
        </button>
        <div className="mx-0.5 h-4 w-px bg-gray-200 dark:bg-gray-700" />
        <button
          onClick={handleResetLayout}
          className="rounded px-2 py-1 text-xs font-medium text-gray-600 transition-colors hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
          title="Reset layout"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
            />
          </svg>
        </button>
      </div>
    </Panel>
  );
}

interface DagGraphProps {
  tasks: TaskWithContext[];
  graph: TaskGraphResponse | TaskGraphExtendedResponse | null;
  selectedTaskId: string | null;
  onTaskClick: (taskId: string) => void;
  buildId?: string;
  onStatusBuildClick?: (buildId: string) => void;
  defaultDirection?: LayoutDirection;
  positionCache?: React.MutableRefObject<PositionCache>;
}

const nodeTypes: NodeTypes = {
  taskNode: TaskNode,
  batchNode: BatchNode,
};

type TaskNodeType = Node<TaskNodeData>;
type BatchNodeType = Node<BatchNodeData>;
type AnyNodeType = TaskNodeType | BatchNodeType;

const NODE_MIN_WIDTH = 160;
const NODE_MAX_WIDTH = 300;
const NODE_HEIGHT = 90;
const BATCH_NODE_MIN_WIDTH = 180;
const BATCH_NODE_MAX_WIDTH = 320;
const BATCH_NODE_HEIGHT = 100;
const CHAR_WIDTH_ESTIMATE = 8;
const NODE_PADDING = 40;
function estimateNodeWidth(label: string, minWidth: number, maxWidth: number): number {
  return Math.max(
    minWidth,
    Math.min(maxWidth, label.length * CHAR_WIDTH_ESTIMATE + NODE_PADDING),
  );
}

function getLayoutedElements(
  nodes: AnyNodeType[],
  edges: Edge[],
  direction: LayoutDirection,
): { nodes: AnyNodeType[]; edges: Edge[] } {
  if (nodes.length === 0) return { nodes, edges };

  const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));

  g.setGraph({
    rankdir: direction,
    nodesep: direction === "LR" ? 30 : 50,
    ranksep: direction === "LR" ? 100 : 80,
    marginx: 20,
    marginy: 20,
  });

  nodes.forEach((node) => {
    const isBatch = node.type === "batchNode";
    const label = (node.data as { label: string }).label;
    const w = isBatch
      ? estimateNodeWidth(label, BATCH_NODE_MIN_WIDTH, BATCH_NODE_MAX_WIDTH)
      : estimateNodeWidth(label, NODE_MIN_WIDTH, NODE_MAX_WIDTH);
    const h = isBatch ? BATCH_NODE_HEIGHT : NODE_HEIGHT;
    g.setNode(node.id, { width: w, height: h });
  });

  edges.forEach((edge) => {
    g.setEdge(edge.source, edge.target);
  });

  Dagre.layout(g);

  const layoutedNodes = nodes.map((node) => {
    const nodeWithPosition = g.node(node.id);
    const isBatch = node.type === "batchNode";
    const label = (node.data as { label: string }).label;
    const w = isBatch
      ? estimateNodeWidth(label, BATCH_NODE_MIN_WIDTH, BATCH_NODE_MAX_WIDTH)
      : estimateNodeWidth(label, NODE_MIN_WIDTH, NODE_MAX_WIDTH);
    const h = isBatch ? BATCH_NODE_HEIGHT : NODE_HEIGHT;
    return {
      ...node,
      position: {
        x: nodeWithPosition.x - w / 2,
        y: nodeWithPosition.y - h / 2,
      },
    };
  });

  return { nodes: layoutedNodes, edges };
}

function getDepthOpacity(depth: number): number {
  if (depth === 0) return 1;
  if (depth === 1) return 0.7;
  return 0.5;
}

export function DagGraph({
  tasks,
  graph,
  selectedTaskId,
  onTaskClick,
  buildId,
  onStatusBuildClick,
  defaultDirection = "LR",
  positionCache: externalPositionCache,
}: DagGraphProps) {
  const { theme } = useTheme();
  const [direction, setDirection] = useState<LayoutDirection>(defaultDirection);

  // Cache of user-adjusted node positions per layout direction
  // Use external cache if provided (shared across instances), otherwise local
  const localPositionCacheRef = useRef<PositionCache>(createPositionCache());
  const positionCacheRef = externalPositionCache ?? localPositionCacheRef;

  // Build maps for lookups
  const taskByTaskId = useMemo(
    () => new Map(tasks.map((t) => [t.task_id, t])),
    [tasks],
  );
  const taskByInternalId = useMemo(() => new Map(tasks.map((t) => [t.id, t])), [tasks]);

  // Build depth map for extended responses
  const nodeDepthMap = useMemo(() => {
    const map = new Map<string, number>();
    if (graph && isExtendedResponse(graph)) {
      for (const node of graph.nodes) {
        map.set(String(node.id), node.traversal_depth);
      }
      for (const group of graph.groups) {
        map.set(group.group_id, group.depth);
      }
    }
    return map;
  }, [graph]);

  // Separate layout computation (expensive, resets positions) from data updates (cheap, preserves positions)
  const { nodes: layoutedNodes, edges: layoutedEdges } = useMemo(() => {
    if (!graph || graph.nodes.length === 0) {
      return { nodes: [] as AnyNodeType[], edges: [] as Edge[] };
    }

    const extended = isExtendedResponse(graph);

    // Create nodes from graph data (without selection state - that's applied separately)
    const nodes: AnyNodeType[] = graph.nodes.map((graphNode) => {
      const task = taskByTaskId.get(graphNode.task_id);
      const depth = extended
        ? (graphNode as { traversal_depth?: number }).traversal_depth ?? 0
        : 0;
      const isPrimary = extended
        ? (graphNode as { is_primary?: boolean }).is_primary ?? true
        : true;
      const isFilterMatch = task?.isFilterMatch ?? isPrimary;

      return {
        id: String(graphNode.id),
        type: "taskNode" as const,
        position: { x: 0, y: 0 },
        style: depth !== 0 ? { opacity: getDepthOpacity(Math.abs(depth)) } : undefined,
        data: {
          label: graphNode.task_name,
          taskId: graphNode.task_id,
          status: graphNode.status,
          isSelected: false,
          isFilterMatch,
          direction,
          hasArtifacts: graphNode.artifact_count > 0,
          waitingForLock: task?.waiting_for_lock,
          statusBuildId: task?.status_build_id,
          currentBuildId: buildId,
          onStatusBuildClick,
        },
      } satisfies TaskNodeType;
    });

    // Add batch nodes for groups
    const groups: GroupSummary[] = extended
      ? (graph as TaskGraphExtendedResponse).groups
      : [];
    for (const group of groups) {
      nodes.push({
        id: group.group_id,
        type: "batchNode" as const,
        position: { x: 0, y: 0 },
        style: { opacity: getDepthOpacity(group.depth) },
        data: {
          label: group.task_name,
          count: group.count,
          taskNamespace: group.task_namespace,
          depth: group.depth,
          status: group.status,
          direction,
        },
      } satisfies BatchNodeType);
    }

    // Create edges from graph data
    const nodeIdSet = new Set(nodes.map((n) => n.id));
    const edges: Edge[] = graph.edges
      .filter(
        (graphEdge) =>
          nodeIdSet.has(String(graphEdge.source)) &&
          nodeIdSet.has(String(graphEdge.target)),
      )
      .map((graphEdge) => {
        const sourceId = String(graphEdge.source);
        const targetId = String(graphEdge.target);
        const sourceTask = taskByInternalId.get(graphEdge.source);
        const targetTask = taskByInternalId.get(graphEdge.target);
        const sourceDepth = nodeDepthMap.get(sourceId) ?? 0;
        const targetDepth = nodeDepthMap.get(targetId) ?? 0;
        const maxAbsDepth = Math.max(Math.abs(sourceDepth), Math.abs(targetDepth));
        const isMutedEdge =
          maxAbsDepth > 0 ||
          !(sourceTask?.isFilterMatch ?? true) ||
          !(targetTask?.isFilterMatch ?? true);

        return {
          id: `${graphEdge.source}-${graphEdge.target}`,
          source: sourceId,
          target: targetId,
          animated: targetTask?.status === "running",
          style: {
            stroke: isMutedEdge
              ? theme === "dark"
                ? "#4b5563"
                : "#d1d5db"
              : theme === "dark"
                ? "#6b7280"
                : "#94a3b8",
            strokeWidth: isMutedEdge ? 1.5 : 2,
            opacity: isMutedEdge
              ? getDepthOpacity(maxAbsDepth) * (isMutedEdge ? 0.7 : 1)
              : 1,
          },
        };
      });

    return getLayoutedElements(nodes, edges, direction);
  }, [
    graph,
    taskByTaskId,
    taskByInternalId,
    nodeDepthMap,
    theme,
    direction,
    buildId,
    onStatusBuildClick,
  ]);

  const [nodes, setNodes, onNodesChange] = useNodesState(layoutedNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(layoutedEdges);

  // Apply layout: use cached positions if available, otherwise dagre defaults.
  // Also backfill uncached nodes so the cache is complete for other instances.
  useEffect(() => {
    const cache = positionCacheRef.current[direction];
    const newNodes = layoutedNodes.map((node) => {
      const cachedPos = cache.get(node.id);
      const position = cachedPos ?? node.position;
      if (!cachedPos) {
        cache.set(node.id, { ...position });
      }
      return {
        ...node,
        position,
        data: { ...node.data },
      };
    }) as AnyNodeType[];
    setNodes(newNodes);
    setEdges([...layoutedEdges]);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- positionCacheRef is a stable ref
  }, [layoutedNodes, layoutedEdges, setNodes, setEdges, direction]);

  // Update selection state without resetting positions
  useEffect(() => {
    setNodes((currentNodes) =>
      currentNodes.map((node) => {
        if (node.type !== "taskNode") return node;
        const taskData = node.data as TaskNodeData;
        const shouldBeSelected = taskData.taskId === selectedTaskId;
        if (taskData.isSelected === shouldBeSelected) return node;
        return {
          ...node,
          data: { ...node.data, isSelected: shouldBeSelected },
        } as AnyNodeType;
      }),
    );
  }, [selectedTaskId, setNodes]);

  // Save current node positions to cache before switching direction
  const handleDirectionChange = useCallback(
    (newDirection: LayoutDirection) => {
      setNodes((currentNodes) => {
        const cache = new Map<string, { x: number; y: number }>();
        for (const node of currentNodes) {
          cache.set(node.id, { ...node.position });
        }
        positionCacheRef.current[direction] = cache;
        return currentNodes;
      });
      setDirection(newDirection);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- positionCacheRef is a stable ref
    [direction, setNodes],
  );

  // Reset layout: clear cache for current direction, re-apply dagre positions
  const handleResetLayout = useCallback(() => {
    positionCacheRef.current[direction] = new Map();
    const newNodes = layoutedNodes.map((node) => ({
      ...node,
      data: { ...node.data },
    })) as AnyNodeType[];
    setNodes(newNodes);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- positionCacheRef is a stable ref
  }, [direction, layoutedNodes, setNodes]);

  const handleNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      const nodeData = node.data as TaskNodeData;
      onTaskClick(nodeData.taskId);
    },
    [onTaskClick],
  );

  // Persist positions to cache whenever nodes are dragged
  const handleNodesChange: typeof onNodesChange = useCallback(
    (changes) => {
      onNodesChange(changes);
      // After drag ends, update the position cache
      for (const change of changes) {
        if (change.type === "position" && change.position && !change.dragging) {
          positionCacheRef.current[direction].set(change.id, {
            ...change.position,
          });
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- positionCacheRef is a stable ref
    [onNodesChange, direction],
  );

  if (!graph || graph.nodes.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
        No tasks to display
      </div>
    );
  }

  const colorMode: ColorMode = theme === "dark" ? "dark" : "light";

  return (
    <div className="h-full w-full bg-gray-50 dark:bg-gray-900">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={handleNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        nodeTypes={nodeTypes}
        colorMode={colorMode}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.3}
        maxZoom={2}
      >
        <Background color={theme === "dark" ? "#374151" : "#e5e7eb"} gap={16} />
        <Controls className="!bg-white dark:!bg-gray-800 !border-gray-200 dark:!border-gray-700" />
        <LayoutToggle
          direction={direction}
          onDirectionChange={handleDirectionChange}
          onResetLayout={handleResetLayout}
        />
      </ReactFlow>
    </div>
  );
}
