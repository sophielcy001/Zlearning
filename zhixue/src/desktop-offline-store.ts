import { api } from './api';

export type DesktopOfflineArea = 'items' | 'timers';

export type DesktopOfflineStore = {
  list: () => Promise<unknown[]>;
  read: (area: DesktopOfflineArea, key: string) => Promise<{ revision: number; value?: unknown }>;
  write: (area: DesktopOfflineArea, key: string, revision: number, value: unknown) => Promise<boolean>;
};

declare global {
  interface Window {
    zhixueTauriOffline?: boolean;
    zhixueDesktop?: {
      onWindowHidden: (callback: () => void) => () => void;
      offline?: DesktopOfflineStore;
    };
  }
}

function offlineRecordPath(area: DesktopOfflineArea, key: string) {
  return '/desktop-offline/' + area + '/record?' + new URLSearchParams({ key });
}

export const tauriOfflineStore: DesktopOfflineStore = {
  list: () => api('/desktop-offline/items'),
  read: (area, key) => api(offlineRecordPath(area, key)),
  write: async (area, key, revision, value) =>
    (await api<{ saved: boolean }>(offlineRecordPath(area, key), 'PUT', { revision, value })).saved,
};

export async function changeDesktopRecord<T, R>(
  store: DesktopOfflineStore, area: DesktopOfflineArea, key: string,
  edit: (value: T | undefined, revision: number) => { value: T | undefined; result: R; unchanged?: boolean },
): Promise<R> {
  for (let attempt = 0; attempt < 8; attempt++) {
    const previous = await store.read(area, key);
    const { value, result, unchanged } = edit(previous.value as T | undefined, previous.revision);
    if (unchanged || await store.write(area, key, previous.revision, value)) return result;
  }
  throw new Error('本机离线记录同时发生了修改，请重试。');
}
