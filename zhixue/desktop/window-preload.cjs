const { contextBridge, ipcRenderer } = require('electron');

const windowHidden = 'zhixue:window-hidden';
contextBridge.exposeInMainWorld('zhixueDesktop', {
  offline: {
    list: () => ipcRenderer.invoke('zhixue:offline-list'),
    read: (area, key) => ipcRenderer.invoke('zhixue:offline-read', area, key),
    write: (area, key, revision, value) => ipcRenderer.invoke('zhixue:offline-write', area, key, revision, value),
  },
  onWindowHidden(callback) {
    const listener = () => callback();
    ipcRenderer.on(windowHidden, listener);
    return () => ipcRenderer.removeListener(windowHidden, listener);
  },
});
