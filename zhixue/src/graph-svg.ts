import type { Camera, Point } from './graph-model';
import { CANDIDATE_LINE, GRAPH_SURFACE, graphCaption, graphPalette } from './graph-paint';
import type { Frame, Glyph, Size } from './graph-scene';

const number = (value: number) => Number(value.toFixed(2));
function xml(text: string) {
  return text.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '�').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}

function vectorShape(glyph: Glyph, point: Point, radius: number, paint: string) {
  const x = number(point.x), y = number(point.y), r = number(radius);
  if (glyph.node?.kind === 'note') {
    return '<path d="M ' + x + ' ' + number(y - r) + ' L ' + number(x + r) + ' ' + y
      + ' ' + x + ' ' + number(y + r) + ' ' + number(x - r) + ' ' + y + ' Z" ' + paint + '/>';
  }
  if (glyph.node?.kind === 'import' || glyph.groupId) {
    const edge = glyph.groupId ? r : r * .87;
    return '<rect x="' + number(x - edge) + '" y="' + number(y - edge) + '" width="' + number(2 * edge)
      + '" height="' + number(2 * edge) + '" rx="' + number(edge * .34) + '" ' + paint + '/>';
  }
  if (glyph.node?.kind === 'supplement') {
    const vertices = Array.from({ length: 6 }, (_, index) => {
      const angle = index * Math.PI / 3;
      return number(x + Math.cos(angle) * r) + ',' + number(y + Math.sin(angle) * r);
    });
    return '<polygon points="' + vertices.join(' ') + '" ' + paint + '/>';
  }
  return '<circle cx="' + x + '" cy="' + y + '" r="' + r + '" ' + paint + '/>';
}

export function graphSvg(frame: Frame, size: Size, camera: Camera, focus: string | null) {
  const { width, height } = size;
  const svg = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + number(width) + ' ' + number(height)
    + '" width="' + number(width) + '" height="' + number(height) + '" overflow="hidden">',
    '<title>智学知识图谱</title>',
    '<defs><pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse">'
    + '<rect x="16" y="16" width="1.15" height="1.15" fill="#dce3f1"/></pattern>'
    + '<marker id="arrow" viewBox="0 -4 8 8" markerWidth="7" markerHeight="7" refX="7" orient="auto-start-reverse" markerUnits="userSpaceOnUse">'
    + '<path d="M 1 -3 L 7 0 L 1 3" fill="none" stroke="#7b8ab3" stroke-width="1.1"/></marker></defs>',
    '<rect width="100%" height="100%" fill="' + GRAPH_SURFACE + '"/><rect width="100%" height="100%" fill="url(#grid)"/>',
    '<g stroke-linecap="round" fill="none">'];
  for (const { edge, start, end, startRadius, endRadius } of frame.lines) {
    const delta = { x: end.x - start.x, y: end.y - start.y };
    const distance = Math.hypot(delta.x, delta.y);
    if (distance <= startRadius + endRadius + 3) continue;
    const picked = edge.source === focus || edge.target === focus;
    const arrow = camera.zoom > .4 && !edge.target.startsWith('cluster:');
    const previous = edge.both && camera.zoom > .4 && !edge.source.startsWith('cluster:');
    svg.push('<line x1="' + number(start.x + delta.x * startRadius / distance) + '" y1="' + number(start.y + delta.y * startRadius / distance)
      + '" x2="' + number(end.x - delta.x * endRadius / distance) + '" y2="' + number(end.y - delta.y * endRadius / distance)
      + '" stroke="' + (edge.candidate ? CANDIDATE_LINE : picked ? '#6d7ec5' : edge.manual ? '#98a3c5' : '#bcc6db') + '" stroke-width="' + (picked ? '1.65' : '1.1') + '"'
      + (edge.candidate ? ' stroke-dasharray="5 5"' : '')
      + (arrow ? ' marker-end="url(#arrow)"' : '') + (previous ? ' marker-start="url(#arrow)"' : '') + '/>');
  }
  svg.push('</g><g font-family="system-ui, sans-serif" text-anchor="middle" font-size="12">');
  for (const { glyph, point, radius } of frame.dots) {
    const selected = glyph.id === focus;
    const [surface, ink] = graphPalette(glyph);
    const x = number(point.x), y = number(point.y);
    svg.push('<g><title>' + xml(glyph.title) + '</title>');
    if (selected || glyph.node?.review) {
      svg.push('<circle cx="' + x + '" cy="' + y + '" r="' + number(radius + 4) + '" fill="none" stroke="'
        + (selected ? '#7181d7' : '#b87b24') + '" stroke-width="1.5"/>');
    }
    svg.push(vectorShape(glyph, point, radius, 'fill="' + surface + '" stroke="' + ink + '" stroke-width="' + (selected ? '2' : '1.25') + '"'));
    if (glyph.node?.weak) {
      svg.push('<circle cx="' + number(point.x + radius * .7) + '" cy="' + number(point.y - radius * .7)
        + '" r="3.8" fill="#ba4c58" stroke="white"/>');
    }
    if (glyph.count !== undefined) {
      svg.push('<text x="' + x + '" y="' + y + '" dominant-baseline="central" fill="' + ink + '" font-size="11" font-weight="650">'
        + glyph.count + '</text>');
    }
    if (selected || glyph.groupId || (glyph.node && camera.zoom > .61)) {
      svg.push('<text x="' + x + '" y="' + number(y + radius + 17)
        + '" fill="#3f4b65" stroke="' + GRAPH_SURFACE + '" stroke-width="4" paint-order="stroke" stroke-linejoin="round">'
        + xml(graphCaption(glyph.title)) + '</text>');
    }
    svg.push('</g>');
  }
  svg.push('</g></svg>');
  return svg.join('\n');
}
