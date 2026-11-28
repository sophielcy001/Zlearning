import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

test('待确认记忆批量编辑保留共享状态，冲突时保留输入', async ({ page }) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(12000);
  const data = await mkdtemp(join(tmpdir(), 'zhixue-memory-batch-'));
  const python = process.env.ZHIXUE_TEST_PYTHON ?? resolve(
    process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
  );
  const service = spawn(python, ['-m', 'backend.launch', '--port', '0', '--stdio', '--data', data], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  const stdout = createInterface({ input: service.stdout });

  try {
    const [address] = await Promise.race([
      once(stdout, 'line'), once(service, 'exit').then(([code]) => { throw new Error('本机服务提前退出 ' + code); }),
    ]);
    const url = new URL(address);
    const session = new URLSearchParams(url.hash.split('?')[1]).get('session');
    async function api(path: string, method = 'GET', body?: object) {
      const response = await page.request.fetch(url.origin + '/api' + path, {
        method, data: body, headers: { Authorization: 'Bearer ' + session },
      });
      expect(response.ok(), await response.text()).toBeTruthy();
      return response.json();
    }

    await page.goto(address);
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
    const original = '\uFEFF先讲一个例子。';
    const saved = await api('/memories', 'POST', { id: crypto.randomUUID(), text: original });
    expect(saved.text).toBe(original);
    await api('/memories', 'POST', { id: crypto.randomUUID(), text: '先检查单位。' });
    await api('/memories', 'POST', { id: crypto.randomUUID(), text: '取整偏好留原样。' });
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '记忆待确认（3）', exact: true }).click();

    const modify = page.getByRole('button', { name: '修改所选', exact: true });
    await expect(modify).toBeDisabled();
    for (const text of ['先讲一个例子。', '先检查单位。']) {
      await page.getByRole('listitem').filter({ has: page.getByText(text, { exact: true }) })
        .getByRole('checkbox', { name: '选择记忆', exact: true }).check();
    }
    const dialog = page.getByRole('dialog', { name: '批量编辑待确认记忆', exact: true });
    const submit = dialog.getByRole('button', { name: '保存所选修改', exact: true });
    const fields = dialog.getByRole('textbox');
    await modify.click();
    await expect(fields).toHaveCount(2);
    await expect(submit).toBeDisabled();
    const olderIndex = (await fields.first().inputValue()) === saved.text ? 0 : 1;
    await fields.nth(olderIndex).fill('   ');
    await expect(submit).toBeDisabled();
    await fields.nth(olderIndex).fill('先阅读一个例子。');
    await fields.nth(1 - olderIndex).fill('统一用厘米比较。');
    await api('/memories/' + saved.id, 'PUT', { revision: saved.revision, text: '另一处改成两个例子。' });
    await submit.click();
    await expect(dialog.getByRole('alert')).toContainText('记忆已变化');
    await expect(fields.nth(olderIndex)).toHaveValue('先阅读一个例子。');
    await expect(fields.nth(1 - olderIndex)).toHaveValue('统一用厘米比较。');
    expect((await api('/memories?view=pending')).items.map((item: { text: string }) => item.text).sort())
      .toEqual(['另一处改成两个例子。', '先检查单位。', '取整偏好留原样。'].sort());

    await dialog.getByRole('button', { name: '关闭', exact: true }).click();
    await expect(page.getByText('另一处改成两个例子。', { exact: true })).toBeVisible();
    await modify.click();
    const updatedIndex = (await fields.first().inputValue()) === '另一处改成两个例子。' ? 0 : 1;
    await fields.nth(updatedIndex).fill('先阅读两个例子。');
    await fields.nth(1 - updatedIndex).fill('统一用厘米比较。');
    await submit.click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText('已保存 2 条待确认记忆修改。', { exact: true })).toBeVisible();

    await page.reload();
    for (const text of ['先阅读两个例子。', '统一用厘米比较。', '取整偏好留原样。']) {
      await expect(page.getByText(text, { exact: true })).toBeVisible();
    }
    await expect(page.getByRole('link', { name: '记忆待确认（3）', exact: true })).toBeVisible();
    expect((await api('/memories?view=shared')).items).toEqual([]);
  } finally {
    stdout.close();
    if (service.exitCode === null && service.signalCode === null) {
      const stopped = once(service, 'exit');
      service.stdin.end();
      await stopped;
    }
    await rm(data, { recursive: true, force: true, maxRetries: 4, retryDelay: 180 });
  }
});
