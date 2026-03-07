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
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTheme } from "../context/ThemeContext";
import type { TaskWithContext } from "../hooks/useTasks";
import type {
  TaskGraphResponse,
  TaskGraphExtendedResponse,
  GroupSummary,
} from "../types/task";
import { BatchNode, type BatchNodeData } from "./BatchNode";
import { TaskNode, type TaskNodeData } from "./TaskNode";

export type LayoutDirection = "TB" | "LR";

interface LayoutToggleProps {
  direction: LayoutDirection;
  onDirectionChange: (direction: LayoutDirection) => void;
}

function LayoutToggle({ direction, onDirectionChange }: LayoutToggleProps) {
  const { fitView } = useReactFlow();

  const handleDirectionChange = useCallback(
    (newDirection: LayoutDirection) => {
      onDirectionChange(newDirection);
      // Delay fitView slightly to allow layout to update
      setTimeout(() => fitView({ padding: 0.2 }), 50);
    },
    [onDirectionChange, fitView],
  );

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
      </div>
    </Panel>
  );
}

function isExtendedResponse(
  graph: TaskGraphResponse | TaskGraphExtendedResponse,
): graph is TaskGraphExtendedResponse {
  return "groups" in graph;
}

interface DagGraphProps {
  tasks: TaskWithContext[];
  graph: TaskGraphResponse | TaskGraphExtendedResponse | null;
  selectedTaskId: string | null;
  onTaskClick: (taskId: string) => void;
  buildId?: string;
  onStatusBuildClick?: (buildId: string) => void;
  defaultDirection?: LayoutDirection;
}

const nodeTypes: NodeTypes = {
  taskNode: TaskNode,
  batchNode: BatchNode,
};

type TaskNodeType = Node<TaskNodeData>;
type BatchNodeType = Node<BatchNodeData>;
type AnyNodeType = TaskNodeType | BatchNodeType;

const NODE_WIDTH = 160;
const NODE_HEIGHT = 90;
const BATCH_NODE_WIDTH = 180;
const BATCH_NODE_HEIGHT = 100;

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
    g.setNode(node.id, {
      width: isBatch ? BATCH_NODE_WIDTH : NODE_WIDTH,
      height: isBatch ? BATCH_NODE_HEIGHT : NODE_HEIGHT,
    });
  });

  edges.forEach((edge) => {
    g.setEdge(edge.source, edge.target);
  });

  Dagre.layout(g);

  const layoutedNodes = nodes.map((node) => {
    const nodeWithPosition = g.node(node.id);
    const isBatch = node.type === "batchNode";
    const w = isBatch ? BATCH_NODE_WIDTH : NODE_WIDTH;
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
}: DagGraphProps) {
  const { theme } = useTheme();
  const [direction, setDirection] = useState<LayoutDirection>(defaultDirection);

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

  const { nodes: initialNodes, edges: initialEdges } = useMemo(() => {
    if (!graph || graph.nodes.length === 0) {
      return { nodes: [] as AnyNodeType[], edges: [] as Edge[] };
    }

    const extended = isExtendedResponse(graph);

    // Create nodes from graph data
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
        style: depth > 0 ? { opacity: getDepthOpacity(depth) } : undefined,
        data: {
          label: graphNode.task_name,
          taskId: graphNode.task_id,
          status: graphNode.status,
          isSelected: graphNode.task_id === selectedTaskId,
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
        const maxDepth = Math.max(sourceDepth, targetDepth);
        const isMutedEdge =
          maxDepth > 0 ||
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
              ? getDepthOpacity(maxDepth) * (isMutedEdge ? 0.7 : 1)
              : 1,
          },
        };
      });

    return getLayoutedElements(nodes, edges, direction);
  }, [
    graph,
    selectedTaskId,
    taskByTaskId,
    taskByInternalId,
    nodeDepthMap,
    theme,
    direction,
    buildId,
    onStatusBuildClick,
  ]);

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  useEffect(() => {
    // Force create new node objects to ensure React Flow detects the change
    const newNodes = initialNodes.map((node) => ({
      ...node,
      data: { ...node.data },
    })) as AnyNodeType[];
    setNodes(newNodes);
    setEdges([...initialEdges]);
  }, [initialNodes, initialEdges, setNodes, setEdges]);

  const handleNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      // Find the task_id from the node data
      const nodeData = node.data as TaskNodeData;
      onTaskClick(nodeData.taskId);
    },
    [onTaskClick],
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
        onNodesChange={onNodesChange}
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
        <LayoutToggle direction={direction} onDirectionChange={setDirection} />
      </ReactFlow>
    </div>
  );
}
