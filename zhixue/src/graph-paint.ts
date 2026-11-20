import type { Camera, Point } from './graph-model';
import type { Frame, Glyph, Size } from './graph-scene';

export const GRAPH_SURFACE = '#f8f9fd';
export const CANDIDATE_LINE = '#8758b3';
const COLORS = {
  course: ['#e6eafd', '#4a56b4'],
  note: ['#f2e8fb', '#82539e'],
  import: ['#e3eef8', '#38668d'],
  supplement: ['#e5f1f5', '#307489'],
  learned: ['#e0f1eb', '#287964'],
  waiting: ['#edf0f5', '#68758b'],
  group: ['#e9e9fb', '#5543a3'],
  cluster: ['#e6ebfa', '#435a9c'],
};

function roundedRect(ctx: CanvasRenderingContext2D, point: Point, radius: number) {
  const corner = radius * .34;
  const left = point.x - radius, top = point.y - radius;
  const right = point.x + radius, bottom = point.y + radius;
  ctx.moveTo(left + corner, top);
  ctx.arcTo(right, top, right, bottom, corner);
  ctx.arcTo(right, bottom, left, bottom, corner);
  ctx.arcTo(left, bottom, left, top, corner);
  ctx.arcTo(left, top, right, top, corner);
  ctx.closePath();
}

function shape(ctx: CanvasRenderingContext2D, glyph: Glyph, point: Point, radius: number) {
  const kind = glyph.node?.kind;
  ctx.beginPath();
  if (kind === 'note') {
    ctx.moveTo(point.x, point.y - radius);
    ctx.lineTo(point.x + radius, point.y);
    ctx.lineTo(point.x, point.y + radius);
    ctx.lineTo(point.x - radius, point.y);
    ctx.closePath();
  } else if (kind === 'import' || glyph.groupId) {
    roundedRect(ctx, point, radius * (glyph.groupId ? 1 : .87));
  } else if (kind === 'supplement') {
    for (let i = 0; i < 6; i++) {
      const angle = i * Math.PI / 3;
      const x = point.x + Math.cos(angle) * radius, y = point.y + Math.sin(angle) * radius;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
  } else {
    ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
  }
}

function arrow(ctx: CanvasRenderingContext2D, start: Point, end: Point, radius: number) {
  const angle = Math.atan2(end.y - start.y, end.x - start.x);
  const x = end.x - Math.cos(angle) * radius, y = end.y - Math.sin(angle) * radius;
  ctx.moveTo(x - Math.cos(angle - .54) * 6, y - Math.sin(angle - .54) * 6);
  ctx.lineTo(x, y);
  ctx.lineTo(x - Math.cos(angle + .54) * 6, y - Math.sin(angle + .54) * 6);
}

export function graphCaption(text: string) {
  const letters = Array.from(text);
  return letters.length > 19 ? letters.slice(0, 18).join('') + '…' : text;
}

export function graphPalette(glyph: Glyph) {
  const node = glyph.node;
  const key = node?.learned ? 'learned' : node && !node.ready ? 'waiting' : node?.kind ?? (glyph.groupId ? 'group' : 'cluster');
  return COLORS[key];
}

function label(ctx: CanvasRenderingContext2D, text: string, point: Point, y: number, selected: boolean) {
  const caption = graphCaption(text);
  ctx.font = (selected ? '650' : '540') + ' 12px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  ctx.lineJoin = 'round';
  ctx.lineWidth = 4;
  ctx.strokeStyle = GRAPH_SURFACE;
  ctx.strokeText(caption, point.x, y);
  ctx.fillStyle = selected ? '#26336f' : '#3f4b65';
  ctx.fillText(caption, point.x, y);
}

export function drawGraph(
  ctx: CanvasRenderingContext2D, frame: Frame, size: Size, camera: Camera, focus: string | null, hover: string | null,
) {
  ctx.fillStyle = GRAPH_SURFACE;
  ctx.fillRect(0, 0, size.width, size.height);
  ctx.fillStyle = '#dce3f1';
  for (let y = 16; y < size.height; y += 24) {
    for (let x = 16; x < size.width; x += 24) ctx.fillRect(x, y, 1.15, 1.15);
  }

  ctx.lineCap = 'round';
  for (const { edge, start, end, startRadius, endRadius } of frame.lines) {
    const picked = edge.source === focus || edge.target === focus;
    const distance = Math.hypot(end.x - start.x, end.y - start.y);
    if (distance <= startRadius + endRadius + 3) continue;
    const ux = (end.x - start.x) / distance, uy = (end.y - start.y) / distance;
    ctx.strokeStyle = edge.candidate ? CANDIDATE_LINE : picked ? '#6d7ec5' : edge.manual ? '#98a3c5' : '#bcc6db';
    if (edge.candidate) ctx.setLineDash([5, 5]);
    ctx.lineWidth = picked ? 1.65 : 1.1;
    ctx.beginPath();
    ctx.moveTo(start.x + ux * startRadius, start.y + uy * startRadius);
    ctx.lineTo(end.x - ux * endRadius, end.y - uy * endRadius);
    if (camera.zoom > .4 && !edge.target.startsWith('cluster:')) arrow(ctx, start, end, endRadius);
    if (edge.both && camera.zoom > .4 && !edge.source.startsWith('cluster:')) arrow(ctx, end, start, startRadius);
    ctx.stroke();
    if (edge.candidate) ctx.setLineDash([]);
  }

  for (const { glyph, point, radius } of frame.dots) {
    const selected = focus === glyph.id, highlighted = hover === glyph.id;
    const node = glyph.node;
    const [surface, ink] = graphPalette(glyph);
    if (selected || highlighted || node?.review) {
      ctx.beginPath();
      ctx.arc(point.x, point.y, radius + 4, 0, Math.PI * 2);
      ctx.strokeStyle = selected ? '#7181d7' : node?.review ? '#b87b24' : '#a1aecf';
      ctx.lineWidth = selected ? 1.6 : 1.3;
      ctx.stroke();
    }
    shape(ctx, glyph, point, radius);
    ctx.fillStyle = surface;
    ctx.fill();
    ctx.strokeStyle = ink;
    ctx.lineWidth = selected ? 2 : 1.25;
    ctx.stroke();
    if (node?.weak) {
      ctx.beginPath();
      ctx.arc(point.x + radius * .7, point.y - radius * .7, 3.8, 0, Math.PI * 2);
      ctx.fillStyle = '#ba4c58';
      ctx.fill();
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    if (glyph.count !== undefined) {
      ctx.fillStyle = ink;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = '650 11px system-ui, sans-serif';
      ctx.fillText(String(glyph.count), point.x, point.y);
    }
    if (selected || highlighted || glyph.groupId || (node && camera.zoom > .61)) {
      label(ctx, glyph.title, point, point.y + radius + 7, selected);
    }
  }
}
