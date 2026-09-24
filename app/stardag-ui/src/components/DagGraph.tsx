import {
  ReactFlow,
  Background,
  BaseEdge,
  Controls,
  type Node,
  type Edge,
  type EdgeProps,
  type EdgeTypes,
  getBezierPath,
  useNodesState,
  useEdgesState,
  type NodeTypes,
  type ColorMode,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import Dagre from "@dagrejs/dagre";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTheme } from "../context/ThemeContext";
import { flowModel, type PlanView } from "../utils/planGraph";
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
  // The plan's members and instance edges; one node per instance.
  view: PlanView;
  selectedTaskId: string | null;
  onTaskClick: (taskId: string) => void;
  // Task ids the table's filters exclude; drawn muted, not hidden.
  mutedTaskIds?: Set<string>;
  defaultDirection?: LayoutDirection;
  direction?: LayoutDirection;
  onDirectionChange?: (direction: LayoutDirection) => void;
  positionCache?: React.MutableRefObject<PositionCache>;
}

const nodeTypes: NodeTypes = { taskNode: TaskNode };

type TaskNodeType = Node<TaskNodeData>;

const NODE_MIN_WIDTH = 160;
const NODE_MAX_WIDTH = 300;
const NODE_HEIGHT = 90;
const CHAR_WIDTH_ESTIMATE = 8;
const NODE_PADDING = 40;

function nodeWidth(label: string): number {
  const displayLength = Math.min(label.length, MAX_LABEL_CHARS);
  return Math.max(
    NODE_MIN_WIDTH,
    Math.min(NODE_MAX_WIDTH, displayLength * CHAR_WIDTH_ESTIMATE + NODE_PADDING),
  );
}

const EDGE_COLORS = {
  dark: { normal: "#6b7280", muted: "#4b5563" },
  light: { normal: "#94a3b8", muted: "#d1d5db" },
} as const;

const DYNAMIC_EDGE_TOOLTIP =
  "Dynamic dependency — yielded at runtime from the downstream task's run() generator.";

function edgeStyle(isMuted: boolean, theme: string, isDynamic: boolean) {
  const palette = theme === "dark" ? EDGE_COLORS.dark : EDGE_COLORS.light;
  return {
    stroke: isMuted ? palette.muted : palette.normal,
    strokeWidth: isMuted ? 1.5 : 2,
    opacity: isMuted ? 0.7 : 1,
    ...(isDynamic ? { strokeDasharray: "6 4" } : {}),
  };
}

// A dynamic edge: the bezier path plus an SVG <title> and a wider
// invisible hit-path, so the native tooltip shows on hover.
function DynamicEdge(props: EdgeProps) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition } =
    props;
  const [edgePath] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  return (
    <g>
      <title>{DYNAMIC_EDGE_TOOLTIP}</title>
      <path
        d={edgePath}
        fill="none"
        stroke="transparent"
        strokeWidth={12}
        pointerEvents="stroke"
      />
      <BaseEdge id={id} path={edgePath} style={props.style} markerEnd={props.markerEnd} />
    </g>
  );
}

const edgeTypes: EdgeTypes = { dynamicEdge: DynamicEdge };

function layout(
  nodes: TaskNodeType[],
  edges: Edge[],
  direction: LayoutDirection,
): TaskNodeType[] {
  if (nodes.length === 0) return nodes;
  const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: direction,
    nodesep: direction === "LR" ? 30 : 50,
    ranksep: direction === "LR" ? 100 : 80,
    marginx: 20,
    marginy: 20,
  });
  for (const node of nodes) {
    g.setNode(node.id, { width: nodeWidth(node.data.label), height: NODE_HEIGHT });
  }
  for (const edge of edges) g.setEdge(edge.source, edge.target);
  Dagre.layout(g);
  return nodes.map((node) => {
    const pos = g.node(node.id);
    return {
      ...node,
      position: {
        x: pos.x - nodeWidth(node.data.label) / 2,
        y: pos.y - NODE_HEIGHT / 2,
      },
    };
  });
}

/** The active plan as a graph over instance edges. */
export function DagGraph({
  view,
  selectedTaskId,
  onTaskClick,
  mutedTaskIds,
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

  const localPositionCacheRef = useRef<PositionCache>(createPositionCache());
  const positionCacheRef = externalPositionCache ?? localPositionCacheRef;

  const { layoutedNodes, layoutedEdges } = useMemo(() => {
    const model = flowModel(view);
    const statusById = new Map(model.nodes.map((n) => [n.id, n.status]));
    const mutedIds = new Set(
      model.nodes
        .filter((n) => n.excluded || (mutedTaskIds?.has(n.taskId) ?? false))
        .map((n) => n.id),
    );
    const nodes: TaskNodeType[] = model.nodes.map((n) => ({
      id: n.id,
      type: "taskNode" as const,
      position: { x: 0, y: 0 },
      data: {
        label: n.label,
        taskId: n.taskId,
        status: n.status,
        isSelected: false,
        isMuted: mutedIds.has(n.id),
        excluded: n.excluded,
        direction,
      },
    }));
    const edges: Edge[] = model.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      animated: statusById.get(e.target) === "running",
      style: edgeStyle(mutedIds.has(e.source) || mutedIds.has(e.target), theme, e.isDynamic),
      ...(e.isDynamic ? { type: "dynamicEdge" } : {}),
    }));
    return { layoutedNodes: layout(nodes, edges, direction), layoutedEdges: edges };
  }, [view, mutedTaskIds, theme, direction]);

  const [nodes, setNodes, onNodesChange] = useNodesState(layoutedNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(layoutedEdges);

  // Cached positions win over dagre's; uncached nodes are backfilled.
  useEffect(() => {
    const cache = positionCacheRef.current[direction];
    setNodes(
      layoutedNodes.map((node) => {
        const cached = cache.get(node.id);
        if (!cached) cache.set(node.id, { ...node.position });
        return { ...node, position: cached ?? node.position, data: { ...node.data } };
      }),
    );
    setEdges([...layoutedEdges]);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- positionCacheRef is a stable ref
  }, [layoutedNodes, layoutedEdges, setNodes, setEdges, direction]);

  useEffect(() => {
    setNodes((current) =>
      current.map((node) => {
        const selected = node.data.taskId === selectedTaskId;
        return node.data.isSelected === selected
          ? node
          : { ...node, data: { ...node.data, isSelected: selected } };
      }),
    );
  }, [selectedTaskId, setNodes]);

  const handleDirectionChange = useCallback(
    (next: LayoutDirection) => {
      setNodes((current) => {
        const cache = new Map<string, { x: number; y: number }>();
        for (const node of current) cache.set(node.id, { ...node.position });
        positionCacheRef.current[direction] = cache;
        return current;
      });
      setDirection(next);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- positionCacheRef is a stable ref
    [direction, setNodes],
  );

  const handleResetLayout = useCallback(() => {
    positionCacheRef.current[direction] = new Map();
    setNodes(layoutedNodes.map((node) => ({ ...node, data: { ...node.data } })));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- positionCacheRef is a stable ref
  }, [direction, layoutedNodes, setNodes]);

  const handleNodesChange: typeof onNodesChange = useCallback(
    (changes) => {
      onNodesChange(changes);
      for (const change of changes) {
        if (change.type === "position" && change.position && !change.dragging) {
          positionCacheRef.current[direction].set(change.id, { ...change.position });
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- positionCacheRef is a stable ref
    [onNodesChange, direction],
  );

  if (view.members.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
        No members to display
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
        onNodeClick={(_, node) => onTaskClick((node.data as TaskNodeData).taskId)}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
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
