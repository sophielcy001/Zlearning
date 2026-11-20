import { matchesView, type Camera, type GraphData, type GraphEdge, type GraphLinkCandidate, type GraphNode, type GraphView, type Point } from './graph-model';

export type Size = { width: number; height: number };
export type Glyph = {
  id: string; title: string; point: Point; phase: number; node?: GraphNode; groupId?: string; count?: number;
};
export type Line = Pick<GraphEdge, 'source' | 'target' | 'both' | 'manual'> & { candidate?: boolean };
export type Scene = {
  points: Glyph[]; edges: Line[]; nodes: GraphNode[];
  levels: Map<number, Level>;
};
type Level = {
  size: number; tiles: Map<string, Glyph[]>; points: Map<string, Glyph>; connections: Map<string, Line[]>;
};
export type Frame = {
  dots: { glyph: Glyph; point: Point; radius: number }[];
  lines: { edge: Line; start: Point; end: Point; startRadius: number; endRadius: number }[];
};

const BASE_TILE = 256;
const MIN_ZOOM = .025;
const MAX_ZOOM = 4;
const WORLD_LIMIT = 1_000_000;

export function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

export function clampPoint({ x, y }: Point): Point {
  return { x: clamp(x, -WORLD_LIMIT, WORLD_LIMIT), y: clamp(y, -WORLD_LIMIT, WORLD_LIMIT) };
}

function phaseFor(id: string): number {
  let hash = 0;
  for (const char of id) hash = (31 * hash + char.charCodeAt(0)) >>> 0;
  return hash / 0xffffffff * Math.PI * 2;
}

function midpoint(items: Point[]): Point {
  const sum = items.reduce((total, item) => ({ x: total.x + item.x, y: total.y + item.y }), { x: 0, y: 0 });
  return { x: sum.x / items.length, y: sum.y / items.length };
}

function projectLines(edges: Line[], representatives: Map<string, string>) {
  const result = new Map<string, Line>();
  for (const edge of edges) {
    const source = representatives.get(edge.source);
    const target = representatives.get(edge.target);
    if (!source || !target || source === target) continue;
    const key = source < target ? source + '|' + target : target + '|' + source;
    const existing = result.get(key);
    if (!existing || (existing.candidate && !edge.candidate)) {
      result.set(key, { ...edge, source, target });
    } else if (Boolean(existing.candidate) === Boolean(edge.candidate)) {
      result.set(key, { ...existing, both: existing.both || edge.both || existing.source !== source, manual: existing.manual || edge.manual });
    }
  }
  return [...result.values()];
}

export function graphScene(data: GraphData, view: GraphView, suggestions: readonly GraphLinkCandidate[] = []): Scene {
  const relations: Line[] = suggestions.length ? [...data.edges, ...suggestions.map(({ source, target, both }) => ({
    source, target, both, manual: false, candidate: true,
  }))] : data.edges;
  const near = new Set<string>();
  if (view.local && view.focus) {
    near.add(view.focus);
    for (const edge of relations) {
      if (edge.source === view.focus) near.add(edge.target);
      if (edge.target === view.focus) near.add(edge.source);
    }
  }
  const nodes = data.nodes.filter(node => matchesView(node, view) && (!near.size || near.has(node.id)));
  const byId = new Map(nodes.map(node => [node.id, node]));
  const collapsed = new Set(view.collapsed);
  const representatives = new Map<string, string>();
  const points: Glyph[] = [];
  for (const group of data.groups) {
    if (!collapsed.has(group.id)) continue;
    const members = group.members.flatMap(member => {
      const node = byId.get(member);
      return node ? [node] : [];
    });
    if (!members.length) continue;
    const id = 'group:' + group.id;
    points.push({
      id, title: group.name, groupId: group.id, count: members.length,
      point: midpoint(members.map(member => member.position)), phase: phaseFor(id),
    });
    for (const member of members) representatives.set(member.id, id);
  }
  for (const node of nodes) {
    if (representatives.has(node.id)) continue;
    representatives.set(node.id, node.id);
    points.push({ id: node.id, title: node.title, point: node.position, node, phase: phaseFor(node.id) });
  }
  return { points, nodes, edges: projectLines(relations, representatives), levels: new Map() };
}

export function worldAt(point: Point, size: Size, camera: Camera): Point {
  return {
    x: (point.x - size.width / 2) / camera.zoom + camera.center.x,
    y: (point.y - size.height / 2) / camera.zoom + camera.center.y,
  };
}

export function screenAt(point: Point, size: Size, camera: Camera): Point {
  return {
    x: (point.x - camera.center.x) * camera.zoom + size.width / 2,
    y: (point.y - camera.center.y) * camera.zoom + size.height / 2,
  };
}

export function zoomAt(camera: Camera, size: Size, cursor: Point, scale: number): Camera {
  const anchor = worldAt(cursor, size, camera);
  const zoom = clamp(scale, MIN_ZOOM, MAX_ZOOM);
  return {
    zoom,
    center: clampPoint({
      x: anchor.x - (cursor.x - size.width / 2) / zoom,
      y: anchor.y - (cursor.y - size.height / 2) / zoom,
    }),
  };
}

export function fitScene(scene: Scene, size: Size): Camera {
  if (!scene.points.length) return { center: { x: 0, y: 0 }, zoom: 1 };
  let left = Infinity, right = -Infinity, top = Infinity, bottom = -Infinity;
  for (const { point } of scene.points) {
    left = Math.min(left, point.x); right = Math.max(right, point.x);
    top = Math.min(top, point.y); bottom = Math.max(bottom, point.y);
  }
  const zoom = Math.min((size.width - 54) / (right - left + 100), (size.height - 54) / (bottom - top + 100));
  return { center: { x: (left + right) / 2, y: (top + bottom) / 2 }, zoom: clamp(zoom, MIN_ZOOM, 1.18) };
}

function tileKey(x: number, y: number) {
  return x + ',' + y;
}

function toTiles(points: Glyph[], size: number) {
  const result = new Map<string, Glyph[]>();
  for (const glyph of points) {
    const key = tileKey(Math.floor(glyph.point.x / size), Math.floor(glyph.point.y / size));
    const bucket = result.get(key);
    if (bucket) bucket.push(glyph);
    else result.set(key, [glyph]);
  }
  return result;
}

type Cluster = { points: Glyph[]; representatives: Map<string, string> };
function cluster(points: Glyph[], size: number): Cluster {
  const collapsed: Glyph[] = [];
  const representatives = new Map<string, string>();
  for (const [key, members] of toTiles(points, size)) {
    const id = members.length === 1 ? members[0].id : 'cluster:' + key;
    collapsed.push(members.length === 1 ? members[0] : {
      id, title: '聚合节点', point: midpoint(members.map(member => member.point)),
      count: members.reduce((sum, member) => sum + (member.count ?? 1), 0), phase: phaseFor(id),
    });
    for (const member of members) representatives.set(member.id, id);
  }
  return { points: collapsed, representatives };
}

function sceneLevel(scene: Scene, zoom: number): Level {
  const size = scene.points.length > 100 && zoom < .6 ? 2 ** Math.ceil(Math.log2(72 / zoom)) : 0;
  const cached = scene.levels.get(size);
  if (cached) return cached;
  const grouped = size ? cluster(scene.points, size) : null;
  const glyphs = grouped?.points ?? scene.points;
  const edges = grouped ? projectLines(scene.edges, grouped.representatives) : scene.edges;
  const points = new Map(glyphs.map(glyph => [glyph.id, glyph]));
  const connections = new Map<string, Line[]>();
  for (const edge of edges) {
    const connected = connections.get(edge.source);
    if (connected) connected.push(edge);
    else connections.set(edge.source, [edge]);
  }
  const tileSize = Math.max(BASE_TILE, size);
  const level = { size: tileSize, tiles: toTiles(glyphs, tileSize), points, connections };
  scene.levels.set(size, level);
  return level;
}

function onscreen(point: Point, size: Size, margin: number) {
  return point.x >= -margin && point.y >= -margin && point.x <= size.width + margin && point.y <= size.height + margin;
}

function visibleGlyphs(level: Level, size: Size, camera: Camera) {
  const start = worldAt({ x: -80, y: -80 }, size, camera);
  const end = worldAt({ x: size.width + 80, y: size.height + 80 }, size, camera);
  const first = { x: Math.floor(start.x / level.size), y: Math.floor(start.y / level.size) };
  const last = { x: Math.floor(end.x / level.size), y: Math.floor(end.y / level.size) };
  if ((last.x - first.x + 1) * (last.y - first.y + 1) > 2 * level.tiles.size) {
    return [...level.points.values()];
  }
  const result: Glyph[] = [];
  for (let x = first.x; x <= last.x; x++) {
    for (let y = first.y; y <= last.y; y++) {
      const glyphs = level.tiles.get(tileKey(x, y));
      if (glyphs) result.push(...glyphs);
    }
  }
  return result;
}

function radiusFor(glyph: Glyph, zoom: number) {
  if (glyph.count !== undefined) return clamp(12 + Math.log2(glyph.count + 1) * 2, 13, 26);
  return clamp(13 * Math.sqrt(zoom), 7, 19);
}

export function graphFrame(scene: Scene, size: Size, camera: Camera, time: number, drag?: { id: string; delta: Point }): Frame {
  const level = sceneLevel(scene, camera.zoom);
  const projected = new Map<string, Frame['dots'][number]>();
  function project(glyph: Glyph) {
    const cached = projected.get(glyph.id);
    if (cached) return cached;
    const delta = drag?.id === glyph.id ? drag.delta : undefined;
    const point = screenAt({
      x: glyph.point.x + (delta?.x ?? 0) + Math.sin(time * .00067 + glyph.phase) * 5,
      y: glyph.point.y + (delta?.y ?? 0) + Math.cos(time * .00059 + glyph.phase) * 4,
    }, size, camera);
    const dot = { glyph, point, radius: radiusFor(glyph, camera.zoom) };
    projected.set(glyph.id, dot);
    return dot;
  }
  const dots = visibleGlyphs(level, size, camera).map(project).filter(item => onscreen(item.point, size, 44));
  const dragged = drag && level.points.get(drag.id);
  if (dragged && !dots.some(dot => dot.glyph.id === dragged.id)) dots.push(project(dragged));
  const displayed = new Map(dots.map(dot => [dot.glyph.id, dot]));
  const lines: Frame['lines'] = [];
  for (const dot of dots) {
    for (const edge of level.connections.get(dot.glyph.id) ?? []) {
      const target = displayed.get(edge.target);
      if (target) lines.push({ edge, start: dot.point, end: target.point, startRadius: dot.radius, endRadius: target.radius });
    }
  }
  return { dots, lines };
}

export function hitGlyph(frame: Frame | null, position: Point): Glyph | undefined {
  let closest: Glyph | undefined;
  let distance = Infinity;
  for (const { glyph, point, radius } of frame?.dots ?? []) {
    const delta = Math.hypot(point.x - position.x, point.y - position.y);
    if (delta <= radius + 7 && delta < distance) {
      closest = glyph;
      distance = delta;
    }
  }
  return closest;
}
