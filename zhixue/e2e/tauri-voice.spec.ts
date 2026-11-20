import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { promisify } from 'node:util';
import { expect, test } from '@playwright/test';
import { openInstalled, prepareWorkspace, reopenExisting, serviceStopped, stopOwned } from '../desktop/installed-probe.mjs';

const installed = process.env.ZHIXUE_TEST_TAURI;
const native = resolve('desktop/windows-native.ps1');
const runFile = promisify(execFile);

test('Windows Tauri stops recording when hidden or minimized and keeps the local voice draft workflow', async () => {
  test.skip(process.platform !== 'win32' || !installed, 'requires an installed Windows Tauri build');
  test.setTimeout(100000);
  const binary = resolve(installed!);
  const root = await mkdtemp(join(tmpdir(), 'zhixue-tauri-voice-'));
  const workspace = join(root, 'workspace');
  const profile = join(root, 'webview');
  await mkdir(workspace);
  const audio = resolve('e2e/fixtures/voice-jfk.wav').replaceAll(String.fromCharCode(92), '/');
  let app;
  try {
    app = await openInstalled(binary, 'tauri', workspace, profile, true, [
      '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
      '--use-file-for-fake-audio-capture=' + JSON.stringify(audio),
    ]);
    const { page } = app;
    const applicationId = String(app.child.pid);
    const { lessonId } = await prepareWorkspace(app, workspace);
    const timer = (await app.api('/api/timer', 'POST', { action: 'start', lessonId })).active;
    async function windowState(mode: 'close' | 'minimize' | 'visible') {
      const { stdout } = await runFile('powershell.exe', [
        '-NoProfile', '-NonInteractive', '-File', native, '-Mode', mode, '-RootId', applicationId,
        '-WindowTitle', '智学 · Tauri',
      ], { windowsHide: true, timeout: 15000 });
      return JSON.parse(stdout);
    }
    const camera = await page.evaluate(async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ video: true });
        stream.getTracks().forEach(track => track.stop());
        return 'allowed';
      } catch (error) { return error instanceof DOMException ? error.name : String(error); }
    });
    expect(camera).toBe('NotAllowedError');

    await page.evaluate(() => {
      const realRequest = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      const tracks: MediaStreamTrack[] = [];
      let release = () => {};
      const first = new Promise<void>(ready => { release = ready; });
      Reflect.set(window, 'testVoiceTracks', tracks);
      Reflect.set(window, 'releaseTestVoice', release);
      navigator.mediaDevices.getUserMedia = async constraints => {
        const hold = tracks.length === 0;
        const stream = await realRequest(constraints);
        tracks.push(...stream.getAudioTracks());
        if (hold) await first;
        return stream;
      };
    });
    const trackStates = () => page.evaluate(() =>
      (Reflect.get(window, 'testVoiceTracks') as MediaStreamTrack[]).map(track => track.readyState));
    let transcriptions = 0;
    page.on('request', request => {
      if (new URL(request.url()).pathname === '/api/voice/transcribe') transcriptions++;
    });

    const input = page.getByLabel('学习问题或目标');
    const voice = page.getByRole('button', { name: '学习问题语音输入' });
    const dialog = page.getByRole('dialog', { name: '录音转文字' });
    const start = dialog.getByRole('button', { name: '开始录音' });
    async function sameTimer() {
      expect((await app!.api('/api/timer')).active).toMatchObject({ id: timer.id, status: 'running' });
    }
    const retained = '原来输入的目标';
    await input.fill(retained);
    await voice.click();
    await start.click();
    await expect(dialog.getByText('正在打开麦克风')).toBeVisible();
    await expect.poll(trackStates).toEqual(['live']);
    await windowState('close');
    await expect.poll(async () => (await windowState('visible')).visible).toBe(false);
    await page.evaluate(() => Reflect.get(window, 'releaseTestVoice')());
    await expect.poll(trackStates).toEqual(['ended']);
    await expect(dialog).toHaveCount(0);
    await sameTimer();

    await reopenExisting(binary, app);
    await expect(input).toHaveValue(retained);
    await voice.click();
    await start.click();
    await expect(dialog.getByText('正在录音', { exact: true })).toBeVisible();
    expect((await windowState('minimize')).minimized).toBe(true);
    await expect.poll(trackStates).toEqual(['ended', 'ended']);
    await expect(dialog).toHaveCount(0);
    await sameTimer();

    await reopenExisting(binary, app);
    await voice.click();
    await start.click();
    await expect(dialog.getByText('正在录音', { exact: true })).toBeVisible();
    await page.evaluate(() => { location.hash = '/library'; });
    await expect(page.getByRole('heading', { name: '知识库', exact: true })).toBeVisible();
    await expect(dialog).toHaveCount(0);
    await expect.poll(trackStates).toEqual(['ended', 'ended', 'ended']);
    expect(transcriptions).toBe(0);
    await sameTimer();

    await page.evaluate(() => { location.hash = '/'; });
    await input.fill(retained);
    await voice.click();
    await dialog.getByLabel('录音语言').selectOption('en');
    await start.click();
    await expect(dialog.getByText('正在录音', { exact: true })).toBeVisible();
    await new Promise(ready => setTimeout(ready, 4200));
    await dialog.getByRole('button', { name: '停止并识别' }).click();
    const draft = dialog.getByRole('textbox', { name: '语音草稿' });
    await expect(draft).toHaveValue(/fellow Americans/i, { timeout: 16000 });
    await expect(input).toHaveValue(retained);
    expect(transcriptions).toBe(1);
    await expect.poll(trackStates).toEqual(['ended', 'ended', 'ended', 'ended']);
    await draft.fill('核对后的本机转写');
    await dialog.getByRole('button', { name: '填入文字' }).click();
    await expect(input).toHaveValue(retained + '\n核对后的本机转写');
  } finally {
    if (app && !app.ended()) await stopOwned(app);
    if (app) await serviceStopped(app);
    await rm(root, { recursive: true, force: true, maxRetries: 8, retryDelay: 200 });
  }
});
