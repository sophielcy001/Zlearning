import { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type Ref } from 'react';
import type { Camera, Point } from './graph-model';
import { clampPoint, fitScene, graphFrame, hitGlyph, zoomAt, type Frame, type Glyph, type Scene, type Size } from './graph-scene';
import { drawGraph } from './graph-paint';
import { graphSvg } from './graph-svg';

type Pose = { id: string; delta: Point };
type Gesture = { pointer: number; start: Point; camera: Camera; glyph?: Glyph; moved: boolean };
type Runtime = {
  size: Size; ratio: number; camera: Camera | null; target: Camera | null; frame: Frame | null;
  phase: number; drag?: Pose; visible: boolean; request: () => void;
};
export type GraphCanvasHandle = {
  fit: () => void; zoom: (factor: number) => void; capture: (format: 'png' | 'svg') => Promise<Blob>;
};
type Props = {
  ref?: Ref<GraphCanvasHandle>; scene: Scene; center: Point | null; zoom: number;
  focus: string | null; motion: boolean; reduced: boolean;
  onCamera: (camera: Camera) => void; onSelect: (glyph: Glyph) => void; onOpen: (glyph: Glyph) => void;
  onMove: (glyph: Glyph, delta: Point) => Promise<void>;
};

const MARGIN = 0.0008;

function pointerAt(element: HTMLElement, clientX: number, clientY: number) {
  const rect = element.getBoundingClientRect();
  return { x: clientX - rect.left, y: clientY - rect.top };
}

function targetFor(runtime: Runtime, props: Props): Camera {
  return props.center ? { center: props.center, zoom: props.zoom } : fitScene(props.scene, runtime.size);
}

export function GraphCanvas(props: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const current = useRef(props);
  const runtime = useRef<Runtime>({
    size: { width: 0, height: 0 }, ratio: 1, camera: null, target: null, frame: null,
    phase: 0, visible: true, request: () => {},
  });
  const gesture = useRef<Gesture | null>(null);
  const lastTap = useRef<{ glyph: Glyph; at: number; point: Point } | undefined>(undefined);
  const [hover, setHover] = useState<Glyph>();
  const hovered = useRef<Glyph | undefined>(undefined);

  function showHover(glyph?: Glyph) {
    if (hovered.current?.id === glyph?.id) return;
    hovered.current = glyph;
    setHover(glyph);
    runtime.current.request();
  }

  useLayoutEffect(() => {
    current.current = props;
    const state = runtime.current;
    state.target = targetFor(state, props);
    if (hovered.current && !props.scene.points.some(glyph => glyph.id === hovered.current?.id) && !hovered.current.id.startsWith('cluster:')) {
      hovered.current = undefined;
      setHover(undefined);
    }
    state.request();
  }, [props]);

  useEffect(() => {
    const element = canvas.current;
    const context = element?.getContext('2d', { alpha: false });
    if (!element || !context) return;
    const state = runtime.current;
    let animation = 0;
    let last = 0;

    function redraw(now: number) {
      animation = 0;
      const options = current.current;
      const target = state.target;
      if (!context || !target || !state.visible || document.hidden || !state.size.width || !state.size.height) return;
      const camera = state.camera ?? target;
      const elapsed = last ? Math.max(1, Math.min(now - last, 40)) : 16;
      last = now;
      const stillMoving = Math.abs(camera.zoom - target.zoom) > MARGIN
        || Math.hypot(camera.center.x - target.center.x, camera.center.y - target.center.y) * target.zoom > .25;
      const blend = state.drag || gesture.current || options.reduced || !stillMoving ? 1 : 1 - Math.exp(-elapsed / 55);
      state.camera = {
        center: { x: camera.center.x + (target.center.x - camera.center.x) * blend,
          y: camera.center.y + (target.center.y - camera.center.y) * blend },
        zoom: camera.zoom + (target.zoom - camera.zoom) * blend,
      };
      if (options.motion) state.phase += elapsed;
      const frame = graphFrame(options.scene, state.size, state.camera, state.phase, state.drag);
      state.frame = frame;
      context.setTransform(state.ratio, 0, 0, state.ratio, 0, 0);
      drawGraph(context, frame, state.size, state.camera, options.focus, hovered.current?.id ?? null);
      if ((options.motion && options.scene.points.length) || (stillMoving && blend < 1)) request();
    }

    function request() {
      if (!animation && state.visible && !document.hidden) animation = window.requestAnimationFrame(redraw);
    }
    state.request = request;
    const resize = new ResizeObserver(([entry]) => {
      state.size = { width: entry.contentRect.width, height: entry.contentRect.height };
      state.ratio = Math.min(2, window.devicePixelRatio || 1);
      element.width = Math.max(1, Math.round(state.size.width * state.ratio));
      element.height = Math.max(1, Math.round(state.size.height * state.ratio));
      state.target = targetFor(state, current.current);
      if (!state.camera) state.camera = state.target;
      request();
    });
    resize.observe(element);
    const visibility = new IntersectionObserver(([entry]) => {
      state.visible = entry.isIntersecting;
      request();
    });
    visibility.observe(element);

    function wheel(event: WheelEvent) {
      if (!state.camera) return;
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? state.size.height : 1;
      const scale = Math.exp(-Math.max(-120, Math.min(120, event.deltaY * unit)) * .003);
      const point = pointerAt(element!, event.clientX, event.clientY);
      const camera = zoomAt(state.camera, state.size, point, state.camera.zoom * scale);
      state.target = state.camera = camera;
      current.current.onCamera(camera);
      request();
    }
    element.addEventListener('wheel', wheel, { passive: false });
    document.addEventListener('visibilitychange', request);
    return () => {
      resize.disconnect();
      visibility.disconnect();
      element.removeEventListener('wheel', wheel);
      document.removeEventListener('visibilitychange', request);
      window.cancelAnimationFrame(animation);
      state.request = () => {};
    };
  }, []);

  function applyCamera(camera: Camera, snap = false) {
    const state = runtime.current;
    state.target = camera;
    if (snap) state.camera = camera;
    props.onCamera(camera);
    state.request();
  }

  function changeScale(factor: number) {
    const { size, camera, target } = runtime.current;
    const from = target ?? camera;
    if (from) applyCamera(zoomAt(from, size, { x: size.width / 2, y: size.height / 2 }, from.zoom * factor));
  }

  function fit() {
    applyCamera(fitScene(props.scene, runtime.current.size));
  }

  async function capture(format: 'png' | 'svg') {
    const state = runtime.current;
    const element = canvas.current;
    if (!element || !state.frame || !state.camera) throw new Error('图谱画布仍在准备，请稍后重试。');
    if (format === 'svg') {
      return new Blob([graphSvg(state.frame, state.size, state.camera, props.focus)], { type: 'image/svg+xml' });
    }
    return new Promise<Blob>((resolve, reject) => {
      element.toBlob(blob => blob ? resolve(blob) : reject(new Error('暂时无法导出图谱图片。')), 'image/png');
    });
  }

  useImperativeHandle(props.ref, () => ({ fit, zoom: changeScale, capture }));

  function begin(event: PointerEvent<HTMLCanvasElement>) {
    const state = runtime.current;
    if (event.button !== 0 || !event.isPrimary || !state.camera || state.drag) return;
    const point = pointerAt(event.currentTarget, event.clientX, event.clientY);
    gesture.current = { pointer: event.pointerId, start: point, camera: state.camera, glyph: hitGlyph(state.frame, point), moved: false };
    state.target = state.camera;
    event.currentTarget.setPointerCapture(event.pointerId);
    event.currentTarget.focus({ preventScroll: true });
  }

  function moving(event: PointerEvent<HTMLCanvasElement>) {
    const state = runtime.current;
    const point = pointerAt(event.currentTarget, event.clientX, event.clientY);
    const held = gesture.current;
    if (!held || held.pointer !== event.pointerId) {
      if (event.pointerType !== 'touch') showHover(hitGlyph(state.frame, point));
      return;
    }
    const pixels = { x: point.x - held.start.x, y: point.y - held.start.y };
    if (!held.moved && Math.hypot(pixels.x, pixels.y) < 5) return;
    held.moved = true;
    showHover(undefined);
    const delta = { x: pixels.x / held.camera.zoom, y: pixels.y / held.camera.zoom };
    if (held.glyph?.node || held.glyph?.groupId) state.drag = { id: held.glyph.id, delta };
    else {
      state.camera = state.target = {
        center: clampPoint({ x: held.camera.center.x - delta.x, y: held.camera.center.y - delta.y }), zoom: held.camera.zoom,
      };
    }
    state.request();
  }

  function end(event: PointerEvent<HTMLCanvasElement>, cancelled = false) {
    const state = runtime.current;
    const held = gesture.current;
    if (!held || held.pointer !== event.pointerId) return;
    gesture.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    if (cancelled) {
      state.drag = undefined;
      state.target = held.camera;
    } else if (held.moved) {
      lastTap.current = undefined;
      const pose = state.drag;
      if (pose && held.glyph) {
        void props.onMove(held.glyph, pose.delta).finally(() => {
          if (state.drag === pose) state.drag = undefined;
          state.request();
        });
      } else if (state.camera) props.onCamera(state.camera);
    } else if (held.glyph) {
      const point = pointerAt(event.currentTarget, event.clientX, event.clientY);
      const previous = lastTap.current;
      const firstTap = !previous || performance.now() - previous.at > 520
        || Math.hypot(previous.point.x - point.x, previous.point.y - point.y) > 12;
      if (firstTap) {
        lastTap.current = { glyph: held.glyph, at: performance.now(), point };
        if (held.glyph.node || held.glyph.groupId) props.onSelect(held.glyph);
        else if (state.camera) {
          const center = { x: state.size.width / 2, y: state.size.height / 2 };
          applyCamera(zoomAt({ center: held.glyph.point, zoom: state.camera.zoom }, state.size, center, state.camera.zoom * 2.2));
        }
      }
    }
    state.request();
  }

  function keyboard(event: KeyboardEvent<HTMLCanvasElement>) {
    const state = runtime.current;
    if (event.key === 'Home') fit();
    else if (event.key === '+' || event.key === '=') changeScale(1.25);
    else if (event.key === '-') changeScale(.8);
    else {
      const camera = state.target ?? state.camera;
      if (!camera) return;
      const offsets: Record<string, [number, number] | undefined> = {
        ArrowLeft: [-46, 0], ArrowRight: [46, 0], ArrowUp: [0, -46], ArrowDown: [0, 46],
      };
      const offset = offsets[event.key];
      if (!offset) return;
      applyCamera({ ...camera, center: clampPoint({
        x: camera.center.x + offset[0] / camera.zoom, y: camera.center.y + offset[1] / camera.zoom,
      }) });
    }
    event.preventDefault();
  }

  return <div className="graph-camera">
    <canvas ref={canvas} className="graph-canvas" tabIndex={0} aria-label="知识图谱画布" aria-describedby="graph-keys"
      onPointerDown={begin} onPointerMove={moving} onPointerUp={event => end(event)}
      onPointerCancel={event => end(event, true)} onPointerLeave={() => showHover(undefined)}
      onDoubleClick={() => {
        const tap = lastTap.current;
        if (tap && performance.now() - tap.at < 800) props.onOpen(tap.glyph);
        lastTap.current = undefined;
      }} onKeyDown={keyboard} />
    <span id="graph-keys" className="sr-only">方向键平移，按加号或减号缩放，Home 键显示全图。可通过搜索列表选择节点。</span>
    {hover && <div className="graph-hover" aria-hidden="true"><strong>{hover.title}</strong>
      {hover.node?.excerpt && <p>{hover.node.excerpt}</p>}
      {hover.count !== undefined && <p>{hover.count} 个节点 · {hover.groupId ? '双击展开' : '单击放大'}</p>}
    </div>}
  </div>;
}
