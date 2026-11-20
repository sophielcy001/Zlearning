const SAMPLE_RATE = 16_000;

async function wavFile(chunks: Float32Array[], rate: number, maxSeconds: number): Promise<Blob> {
  const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  if (length / rate < 0.25) throw new Error('录音太短，请重新录制。');

  const count = Math.min(maxSeconds * SAMPLE_RATE, Math.floor(length * SAMPLE_RATE / rate));
  const render = new OfflineAudioContext(1, count, SAMPLE_RATE);
  const recording = render.createBuffer(1, length, rate);
  const input = recording.getChannelData(0);
  let offset = 0;
  for (const chunk of chunks) {
    input.set(chunk, offset);
    offset += chunk.length;
  }
  const source = render.createBufferSource();
  source.buffer = recording;
  source.connect(render.destination);
  source.start();
  const pcm = (await render.startRendering()).getChannelData(0);

  const bytes = new ArrayBuffer(44 + count * 2);
  const header = new DataView(bytes);
  function tag(offset: number, label: string) {
    for (const [index, character] of [...label].entries()) header.setUint8(offset + index, character.charCodeAt(0));
  }
  tag(0, 'RIFF'); header.setUint32(4, bytes.byteLength - 8, true); tag(8, 'WAVE');
  tag(12, 'fmt '); header.setUint32(16, 16, true);
  header.setUint16(20, 1, true); header.setUint16(22, 1, true);
  header.setUint32(24, SAMPLE_RATE, true); header.setUint32(28, SAMPLE_RATE * 2, true);
  header.setUint16(32, 2, true); header.setUint16(34, 16, true);
  tag(36, 'data'); header.setUint32(40, count * 2, true);
  for (let index = 0; index < count; index++) {
    header.setInt16(44 + index * 2, Math.max(-32768, Math.min(32767, pcm[index] * 32768)), true);
  }
  return new Blob([bytes], { type: 'audio/wav' });
}

export async function recordVoice(maxSeconds: number, signal: AbortSignal, started: (stop: () => void) => void) {
  const context = new AudioContext();
  let stream: MediaStream | undefined;
  const resume = context.resume().then(() => null, (error: unknown) => error);
  // The permission request runs in the recording button's user gesture.
  function release() {
    stream?.getTracks().forEach(track => track.stop());
    if (context.state !== 'closed') void context.close().catch(() => {});
  }
  signal.addEventListener('abort', release, { once: true });
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 }, video: false });
    signal.throwIfAborted();
    const resumeError = await resume;
    if (resumeError) throw resumeError;
    await context.audioWorklet.addModule('/voice-capture.js');
    signal.throwIfAborted();

    const input = context.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(context, 'zhixue-voice-capture', {
      channelCount: 1, channelCountMode: 'explicit',
      processorOptions: { maxSamples: maxSeconds * context.sampleRate },
    });
    const tracks = stream.getAudioTracks();
    const chunks = await new Promise<Float32Array[]>((resolve, reject) => {
      const received: Float32Array[] = [];
      let finishTimer: number | undefined;
      function complete(error?: unknown) {
        window.clearTimeout(deadline);
        window.clearTimeout(finishTimer);
        signal.removeEventListener('abort', cancelled);
        tracks.forEach(track => track.removeEventListener('ended', interrupted));
        node.onprocessorerror = null;
        node.port.onmessage = null;
        node.port.close();
        if (error) reject(error);
        else resolve(received);
      }
      function cancelled() { complete(signal.reason); }
      function interrupted() { complete(new Error('麦克风已断开，请重新录音。')); }
      function stop() {
        if (finishTimer !== undefined) return;
        finishTimer = window.setTimeout(() => complete(new Error('录音未能保存为草稿，请重新录音。')), 2500);
        node.port.postMessage('stop');
        tracks.forEach(track => track.stop());
      }

      node.port.onmessage = ({ data }: MessageEvent<Float32Array | null>) => {
        if (data === null) complete();
        else received.push(data);
      };
      node.onprocessorerror = interrupted;
      tracks.forEach(track => track.addEventListener('ended', interrupted, { once: true }));
      signal.addEventListener('abort', cancelled, { once: true });
      const deadline = window.setTimeout(stop, maxSeconds * 1000 + 1000);
      // A connected worklet receives audio frames without passing the microphone through to the speakers.
      input.connect(node);
      node.connect(context.destination);
      started(stop);
    });
    release();
    signal.throwIfAborted();
    return await wavFile(chunks, context.sampleRate, maxSeconds);
  } finally {
    signal.removeEventListener('abort', release);
    release();
  }
}
