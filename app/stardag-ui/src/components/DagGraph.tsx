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
import { TaskNode, type TaskNodeData } from "./TaskNode";

interface DagGraphProps {
  tasks: TaskWithContext[];
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

export function DagGraph({ tasks, selectedTaskId, onTaskClick }: DagGraphProps) {
  const { theme } = useTheme();
  const taskMap = useMemo(() => new Map(tasks.map((t) => [t.task_id, t])), [tasks]);

  const { nodes: initialNodes, edges: initialEdges } = useMemo(() => {
    const nodes: TaskNodeType[] = tasks.map((task) => ({
      id: task.task_id,
      type: "taskNode",
      position: { x: 0, y: 0 },
      data: {
        label: task.task_family,
        taskId: task.task_id,
        status: task.status,
        isSelected: task.task_id === selectedTaskId,
        isFilterMatch: task.isFilterMatch,
      },
    }));

    const edges: Edge[] = [];
    tasks.forEach((task) => {
      task.dependency_ids.forEach((depId) => {
        if (taskMap.has(depId)) {
          const sourceTask = taskMap.get(depId)!;
          const isMutedEdge = !task.isFilterMatch || !sourceTask.isFilterMatch;
          edges.push({
            id: `${depId}-${task.task_id}`,
            source: depId,
            target: task.task_id,
            animated: task.status === "running",
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
          });
        }
      });
    });

    return getLayoutedElements(nodes, edges);
  }, [tasks, selectedTaskId, taskMap, theme]);

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  useEffect(() => {
    setNodes(initialNodes);
    setEdges(initialEdges);
  }, [initialNodes, initialEdges, setNodes, setEdges]);

  const handleNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      onTaskClick(node.id);
    },
    [onTaskClick],
  );

  if (tasks.length === 0) {
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
