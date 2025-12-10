import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
  useNodesState,
  useEdgesState,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import Dagre from "@dagrejs/dagre";
import { useCallback, useEffect, useMemo } from "react";
import type { Task } from "../types/task";
import { TaskNode, type TaskNodeData } from "./TaskNode";

interface DagGraphProps {
  tasks: Task[];
  selectedTaskId: string;
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
  const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));

  g.setGraph({
    rankdir: "TB", // Top to bottom
    nodesep: 50, // Horizontal spacing between nodes
    ranksep: 80, // Vertical spacing between ranks
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
  const taskMap = useMemo(() => new Map(tasks.map((t) => [t.task_id, t])), [tasks]);

  const { nodes: initialNodes, edges: initialEdges } = useMemo(() => {
    // Create nodes
    const nodes: TaskNodeType[] = tasks.map((task) => ({
      id: task.task_id,
      type: "taskNode",
      position: { x: 0, y: 0 }, // Will be set by dagre
      data: {
        label: task.task_family,
        taskId: task.task_id,
        status: task.status,
        isSelected: task.task_id === selectedTaskId,
      },
    }));

    // Create edges (from dependency to dependent)
    const edges: Edge[] = [];
    tasks.forEach((task) => {
      task.dependency_ids.forEach((depId) => {
        if (taskMap.has(depId)) {
          edges.push({
            id: `${depId}-${task.task_id}`,
            source: depId,
            target: task.task_id,
            animated: task.status === "running",
            style: { stroke: "#94a3b8", strokeWidth: 2 },
          });
        }
      });
    });

    // Apply dagre layout
    return getLayoutedElements(nodes, edges);
  }, [tasks, selectedTaskId, taskMap]);

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  // Update nodes when tasks change
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
      <div className="flex h-64 items-center justify-center text-gray-500">
        No tasks to display
      </div>
    );
  }

  return (
    <div className="h-96 w-full rounded-lg border border-gray-200 bg-gray-50">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.5}
        maxZoom={1.5}
      >
        <Background color="#e5e7eb" gap={16} />
        <Controls />
      </ReactFlow>
    </div>
  );
}
