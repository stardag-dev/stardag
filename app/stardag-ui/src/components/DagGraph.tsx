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
import type { TaskGraphResponse } from "../types/task";
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

interface DagGraphProps {
  tasks: TaskWithContext[];
  graph: TaskGraphResponse | null;
  selectedTaskId: string | null;
  onTaskClick: (taskId: string) => void;
}

const nodeTypes: NodeTypes = {
  taskNode: TaskNode,
};

type TaskNodeType = Node<TaskNodeData>;

const NODE_WIDTH = 160;
const NODE_HEIGHT = 90;

function getLayoutedElements(
  nodes: TaskNodeType[],
  edges: Edge[],
  direction: LayoutDirection,
): { nodes: TaskNodeType[]; edges: Edge[] } {
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
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  });

  edges.forEach((edge) => {
    g.setEdge(edge.source, edge.target);
  });

  Dagre.layout(g);

  const layoutedNodes = nodes.map((node) => {
    const nodeWithPosition = g.node(node.id);
    return {
      ...node,
      position: {
        x: nodeWithPosition.x - NODE_WIDTH / 2,
        y: nodeWithPosition.y - NODE_HEIGHT / 2,
      },
    };
  });

  return { nodes: layoutedNodes, edges };
}

export function DagGraph({ tasks, graph, selectedTaskId, onTaskClick }: DagGraphProps) {
  const { theme } = useTheme();
  const [direction, setDirection] = useState<LayoutDirection>("LR");

  // Build maps for lookups
  const taskByTaskId = useMemo(
    () => new Map(tasks.map((t) => [t.task_id, t])),
    [tasks],
  );
  const taskByInternalId = useMemo(() => new Map(tasks.map((t) => [t.id, t])), [tasks]);

  const { nodes: initialNodes, edges: initialEdges } = useMemo(() => {
    if (!graph || graph.nodes.length === 0) {
      return { nodes: [], edges: [] };
    }

    // Create nodes from graph data
    const nodes: TaskNodeType[] = graph.nodes.map((graphNode) => {
      const task = taskByTaskId.get(graphNode.task_id);
      return {
        id: String(graphNode.id), // ReactFlow needs string IDs
        type: "taskNode",
        position: { x: 0, y: 0 },
        data: {
          label: graphNode.task_name,
          taskId: graphNode.task_id,
          status: graphNode.status,
          isSelected: graphNode.task_id === selectedTaskId,
          isFilterMatch: task?.isFilterMatch ?? true,
          direction,
          hasAssets: graphNode.asset_count > 0,
        },
      };
    });

    // Create edges from graph data
    const edges: Edge[] = graph.edges.map((graphEdge) => {
      const sourceTask = taskByInternalId.get(graphEdge.source);
      const targetTask = taskByInternalId.get(graphEdge.target);
      const isMutedEdge =
        !(sourceTask?.isFilterMatch ?? true) || !(targetTask?.isFilterMatch ?? true);

      return {
        id: `${graphEdge.source}-${graphEdge.target}`,
        source: String(graphEdge.source),
        target: String(graphEdge.target),
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
          opacity: isMutedEdge ? 0.5 : 1,
        },
      };
    });

    return getLayoutedElements(nodes, edges, direction);
  }, [graph, selectedTaskId, taskByTaskId, taskByInternalId, theme, direction]);

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  useEffect(() => {
    // Force create new node objects to ensure React Flow detects the change
    const newNodes = initialNodes.map((node) => ({
      ...node,
      data: { ...node.data },
    }));
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
