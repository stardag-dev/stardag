import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
  Position,
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
import { LayoutToggle } from "./LayoutToggle";
import { TaskNode, type TaskNodeData } from "./TaskNode";
import {
  createPositionCache,
  MAX_LABEL_CHARS,
  type LayoutDirection,
  type PositionCache,
} from "./dagLayout";

export type { LayoutDirection } from "./dagLayout";

interface DagGraphProps {
  tasks: TaskWithContext[];
  graph: TaskGraphResponse | TaskGraphExtendedResponse | null;
  selectedTaskId: string | null;
  onTaskClick: (taskId: string) => void;
  buildId?: string;
  onStatusBuildClick?: (buildId: string) => void;
  defaultDirection?: LayoutDirection;
  direction?: LayoutDirection;
  onDirectionChange?: (direction: LayoutDirection) => void;
  positionCache?: React.MutableRefObject<PositionCache>;
}

const nodeTypes: NodeTypes = {
  taskNode: TaskNode,
  batchNode: BatchNode,
};

type TaskNodeType = Node<TaskNodeData>;
type BatchNodeType = Node<BatchNodeData>;
type AnyNodeType = TaskNodeType | BatchNodeType;

// --- Node dimension constants ---

const NODE_MIN_WIDTH = 160;
const NODE_MAX_WIDTH = 300;
const NODE_HEIGHT = 90;
const BATCH_NODE_MIN_WIDTH = 180;
const BATCH_NODE_MAX_WIDTH = 320;
const BATCH_NODE_HEIGHT = 100;
const CHAR_WIDTH_ESTIMATE = 8;
const NODE_PADDING = 40;

function estimateNodeWidth(label: string, minWidth: number, maxWidth: number): number {
  const displayLength = Math.min(label.length, MAX_LABEL_CHARS);
  return Math.max(
    minWidth,
    Math.min(maxWidth, displayLength * CHAR_WIDTH_ESTIMATE + NODE_PADDING),
  );
}

function getNodeDimensions(node: AnyNodeType): { w: number; h: number } {
  const isBatch = node.type === "batchNode";
  const label = (node.data as { label: string }).label;
  return {
    w: isBatch
      ? estimateNodeWidth(label, BATCH_NODE_MIN_WIDTH, BATCH_NODE_MAX_WIDTH)
      : estimateNodeWidth(label, NODE_MIN_WIDTH, NODE_MAX_WIDTH),
    h: isBatch ? BATCH_NODE_HEIGHT : NODE_HEIGHT,
  };
}

// --- Edge color constants ---

const EDGE_COLORS = {
  dark: { normal: "#6b7280", muted: "#4b5563" },
  light: { normal: "#94a3b8", muted: "#d1d5db" },
} as const;

function getEdgeStyle(isMuted: boolean, theme: string, depthOpacity: number) {
  const palette = theme === "dark" ? EDGE_COLORS.dark : EDGE_COLORS.light;
  return {
    stroke: isMuted ? palette.muted : palette.normal,
    strokeWidth: isMuted ? 1.5 : 2,
    opacity: isMuted ? depthOpacity * 0.7 : 1,
  };
}

// --- Layout helpers ---

function getDepthOpacity(depth: number): number {
  if (depth === 0) return 1;
  if (depth === 1) return 0.7;
  return 0.5;
}

function getHandlePositions(direction: LayoutDirection): {
  source: Position;
  target: Position;
} {
  return direction === "LR"
    ? { source: Position.Right, target: Position.Left }
    : { source: Position.Bottom, target: Position.Top };
}

function applyDagreLayout(
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

  const nodeSizes = new Map<string, { w: number; h: number }>();
  nodes.forEach((node) => {
    const size = getNodeDimensions(node);
    nodeSizes.set(node.id, size);
    g.setNode(node.id, { width: size.w, height: size.h });
  });

  edges.forEach((edge) => {
    g.setEdge(edge.source, edge.target);
  });

  Dagre.layout(g);

  const layoutedNodes = nodes.map((node) => {
    const pos = g.node(node.id);
    const size = nodeSizes.get(node.id)!;
    return {
      ...node,
      position: {
        x: pos.x - size.w / 2,
        y: pos.y - size.h / 2,
      },
    };
  });

  return { nodes: layoutedNodes, edges };
}

// --- Graph data builders ---

interface BuildNodesParams {
  graph: TaskGraphResponse | TaskGraphExtendedResponse;
  taskByTaskId: Map<string, TaskWithContext>;
  direction: LayoutDirection;
  buildId?: string;
  onStatusBuildClick?: (buildId: string) => void;
}

function buildGraphNodes({
  graph,
  taskByTaskId,
  direction,
  buildId,
  onStatusBuildClick,
}: BuildNodesParams): AnyNodeType[] {
  const extended = isExtendedResponse(graph);
  const handlePositions = getHandlePositions(direction);

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
      sourcePosition: handlePositions.source,
      targetPosition: handlePositions.target,
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
      sourcePosition: handlePositions.source,
      targetPosition: handlePositions.target,
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

  return nodes;
}

interface BuildEdgesParams {
  graph: TaskGraphResponse | TaskGraphExtendedResponse;
  nodeIds: Set<string>;
  taskByInternalId: Map<string | number, TaskWithContext>;
  nodeDepthMap: Map<string, number>;
  theme: string;
}

function buildGraphEdges({
  graph,
  nodeIds,
  taskByInternalId,
  nodeDepthMap,
  theme,
}: BuildEdgesParams): Edge[] {
  return graph.edges
    .filter(
      (graphEdge) =>
        nodeIds.has(String(graphEdge.source)) && nodeIds.has(String(graphEdge.target)),
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
        style: getEdgeStyle(isMutedEdge, theme, getDepthOpacity(maxAbsDepth)),
      };
    });
}

// --- Component ---

export function DagGraph({
  tasks,
  graph,
  selectedTaskId,
  onTaskClick,
  buildId,
  onStatusBuildClick,
  defaultDirection = "LR",
  direction: controlledDirection,
  onDirectionChange: controlledOnDirectionChange,
  positionCache: externalPositionCache,
}: DagGraphProps) {
  const { theme } = useTheme();
  const isControlled =
    controlledDirection !== undefined && controlledOnDirectionChange !== undefined;
  const [localDirection, setLocalDirection] = useState<LayoutDirection>(
    controlledDirection ?? defaultDirection,
  );
  const direction = isControlled ? controlledDirection : localDirection;
  const setDirection = isControlled ? controlledOnDirectionChange : setLocalDirection;

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

  // Compute layout: build nodes and edges, then apply dagre positioning
  const { nodes: layoutedNodes, edges: layoutedEdges } = useMemo(() => {
    if (!graph || graph.nodes.length === 0) {
      return { nodes: [] as AnyNodeType[], edges: [] as Edge[] };
    }

    const nodes = buildGraphNodes({
      graph,
      taskByTaskId,
      direction,
      buildId,
      onStatusBuildClick,
    });

    const nodeIds = new Set(nodes.map((n) => n.id));
    const edges = buildGraphEdges({
      graph,
      nodeIds,
      taskByInternalId,
      nodeDepthMap,
      theme,
    });

    return applyDagreLayout(nodes, edges, direction);
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
