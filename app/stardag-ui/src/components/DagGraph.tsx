import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
  useNodesState,
  useEdgesState,
  type NodeTypes,
  type ColorMode,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import Dagre from "@dagrejs/dagre";
import { useCallback, useEffect, useMemo } from "react";
import { useTheme } from "../context/ThemeContext";
import type { TaskWithContext } from "../hooks/useTasks";
import type { TaskGraphResponse } from "../types/task";
import { TaskNode, type TaskNodeData } from "./TaskNode";

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
): { nodes: TaskNodeType[]; edges: Edge[] } {
  if (nodes.length === 0) return { nodes, edges };

  const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));

  g.setGraph({
    rankdir: "TB",
    nodesep: 50,
    ranksep: 80,
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
          label: graphNode.task_family,
          taskId: graphNode.task_id,
          status: graphNode.status,
          isSelected: graphNode.task_id === selectedTaskId,
          isFilterMatch: task?.isFilterMatch ?? true,
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

    return getLayoutedElements(nodes, edges);
  }, [graph, selectedTaskId, taskByTaskId, taskByInternalId, theme]);

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  useEffect(() => {
    setNodes(initialNodes);
    setEdges(initialEdges);
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
      </ReactFlow>
    </div>
  );
}
