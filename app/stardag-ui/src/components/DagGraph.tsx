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

interface LayoutNode {
  id: string;
  depth: number;
  horizontalIndex: number;
}

function layoutDag(tasks: Task[]): LayoutNode[] {
  const taskMap = new Map(tasks.map((t) => [t.task_id, t]));
  const depths = new Map<string, number>();
  const visited = new Set<string>();

  // Calculate depth for each node (distance from leaves)
  function calculateDepth(taskId: string): number {
    if (depths.has(taskId)) return depths.get(taskId)!;
    if (visited.has(taskId)) return 0; // Cycle protection
    visited.add(taskId);

    const task = taskMap.get(taskId);
    if (!task || task.dependency_ids.length === 0) {
      depths.set(taskId, 0);
      return 0;
    }

    const maxDepDeph = Math.max(
      ...task.dependency_ids
        .filter((depId) => taskMap.has(depId))
        .map((depId) => calculateDepth(depId)),
    );
    const depth = maxDepDeph + 1;
    depths.set(taskId, depth);
    return depth;
  }

  // Calculate depths for all tasks
  tasks.forEach((t) => calculateDepth(t.task_id));

  // Group by depth and assign horizontal positions
  const byDepth = new Map<number, string[]>();
  tasks.forEach((t) => {
    const depth = depths.get(t.task_id) || 0;
    if (!byDepth.has(depth)) byDepth.set(depth, []);
    byDepth.get(depth)!.push(t.task_id);
  });

  // Create layout nodes
  const layoutNodes: LayoutNode[] = [];
  byDepth.forEach((taskIds, depth) => {
    taskIds.forEach((id, index) => {
      layoutNodes.push({ id, depth, horizontalIndex: index });
    });
  });

  return layoutNodes;
}

type TaskNode = Node<TaskNodeData>;

export function DagGraph({ tasks, selectedTaskId, onTaskClick }: DagGraphProps) {
  const taskMap = useMemo(() => new Map(tasks.map((t) => [t.task_id, t])), [tasks]);

  const { initialNodes, initialEdges } = useMemo(() => {
    const layout = layoutDag(tasks);
    const maxDepth = Math.max(...layout.map((n) => n.depth), 0);

    // Calculate width per depth level
    const countByDepth = new Map<number, number>();
    layout.forEach((n) => {
      countByDepth.set(n.depth, (countByDepth.get(n.depth) || 0) + 1);
    });

    const nodeWidth = 160;
    const nodeHeight = 80;
    const horizontalSpacing = 40;
    const verticalSpacing = 100;

    const nodes: TaskNode[] = layout.map((layoutNode) => {
      const task = taskMap.get(layoutNode.id)!;
      const countAtDepth = countByDepth.get(layoutNode.depth) || 1;
      const totalWidth =
        countAtDepth * nodeWidth + (countAtDepth - 1) * horizontalSpacing;
      const startX = -totalWidth / 2 + nodeWidth / 2;

      return {
        id: layoutNode.id,
        type: "taskNode",
        position: {
          x: startX + layoutNode.horizontalIndex * (nodeWidth + horizontalSpacing),
          y: (maxDepth - layoutNode.depth) * (nodeHeight + verticalSpacing),
        },
        data: {
          label: task.task_family,
          taskId: task.task_id,
          status: task.status,
          isSelected: task.task_id === selectedTaskId,
        },
      };
    });

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

    return { initialNodes: nodes, initialEdges: edges };
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
