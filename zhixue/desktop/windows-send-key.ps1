param([Parameter(Mandatory=$true)][int]$RootId,[Parameter(Mandatory=$true)][string]$Executable,
      [Parameter(Mandatory=$true)][ValidateSet('智学 · Electron','智学 · Tauri','智学 · 完全退出')][string]$Title,
      [Parameter(Mandatory=$true)][ValidateSet('Quit','Enter')][string]$Key)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$app=Get-Process -Id $RootId
if(!$app.Path -or $app.Path -ine $Executable -or $app.SessionId -ne (Get-Process -Id $PID).SessionId){
    throw 'The input target is not the owned installed app'
}
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Diagnostics;
using System.Threading;
public static class OwnedTestInput {
 [StructLayout(LayoutKind.Sequential)] struct Keyboard {
    public ushort vk, scan;
    public uint flags, time;
    public UIntPtr extra;
 }
 [StructLayout(LayoutKind.Sequential)] struct Mouse {
    public int dx, dy;
    public uint data, flags, time;
    public UIntPtr extra;
 }
 [StructLayout(LayoutKind.Explicit)] struct Data {
    [FieldOffset(0)] public Keyboard keyboard;
    [FieldOffset(0)] public Mouse mouse;
 }
 [StructLayout(LayoutKind.Sequential)] struct Input { public uint type; public Data value; }
 [DllImport("user32.dll")] static extern short GetAsyncKeyState(int vk);
 [DllImport("user32.dll")] static extern uint MapVirtualKey(uint code, uint kind);
 [DllImport("user32.dll", SetLastError=true)] static extern uint SendInput(uint count, Input[] data, int size);
 [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] static extern IntPtr GetLastActivePopup(IntPtr window);
 [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
 [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr window);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint id);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr window, StringBuilder text, int length);
 [DllImport("user32.dll")] static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
 [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr desktop);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern bool GetUserObjectInformation(IntPtr desktop, int index, StringBuilder name, int size, out int needed);

 public static uint Insert(uint process, string title, int[] keys, bool waitForDialog) {
    var name = new StringBuilder(128);
    var desktop = OpenInputDesktop(0, false, 1);
    if (desktop == IntPtr.Zero) throw new InvalidOperationException("Input desktop unavailable");
    int needed;
    try {
      if (!GetUserObjectInformation(desktop, 2, name, 256, out needed) || name.ToString() != "Default")
        throw new InvalidOperationException("Input desktop is not Default");
    } finally { CloseDesktop(desktop); }
    foreach (int modifier in new[] { 0x10, 0x11, 0x12, 0x5b, 0x5c }) {
      if ((GetAsyncKeyState(modifier) & 0x8000) != 0)
        throw new InvalidOperationException("A modifier is already held. No input was inserted.");
    }
    var inputs = new Input[keys.Length * 2];
    for (int i = 0; i < keys.Length; i++) {
      var scan = (ushort)MapVirtualKey((uint)keys[i], 0);
      if (scan == 0) throw new InvalidOperationException("The test key did not map to a scan code");
      inputs[i] = new Input { type = 1, value = new Data { keyboard = new Keyboard { scan = scan, flags = 8 } } };
      inputs[inputs.Length-1-i] = new Input { type = 1, value = new Data { keyboard = new Keyboard { scan = scan, flags = 10 } } };
    }
    var wait = Stopwatch.StartNew();
    while (true) {
      uint owner;
      var foreground = GetForegroundWindow();
      GetWindowThreadProcessId(foreground, out owner);
      name.Clear();
      GetWindowText(foreground, name, name.Capacity);
      if (owner == process && name.ToString() == title && foreground == GetForegroundWindow())
        break;
      if (!waitForDialog || wait.ElapsedMilliseconds >= 3000)
        throw new InvalidOperationException("The exact owned test window does not hold focus. No input was inserted.");
      if (owner == process) {
        var popup = GetLastActivePopup(foreground);
        uint popupOwner;
        GetWindowThreadProcessId(popup, out popupOwner);
        name.Clear();
        GetWindowText(popup, name, name.Capacity);
        if (popupOwner == process && name.ToString() == title && IsWindowVisible(popup))
          SetForegroundWindow(popup);
      }
      Thread.Sleep(75);
    }
    foreach (int modifier in new[] { 0x10, 0x11, 0x12, 0x5b, 0x5c }) {
      if ((GetAsyncKeyState(modifier) & 0x8000) != 0)
        throw new InvalidOperationException("A modifier is already held. No input was inserted.");
    }
    uint lastOwner;
    var lastForeground = GetForegroundWindow();
    GetWindowThreadProcessId(lastForeground, out lastOwner);
    name.Clear();
    GetWindowText(lastForeground, name, name.Capacity);
    if (lastOwner != process || name.ToString() != title || lastForeground != GetForegroundWindow())
      throw new InvalidOperationException("Focus changed at dispatch. No input was inserted.");
    var sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(Input)));
    if (sent != inputs.Length) throw new InvalidOperationException("Unexpected number of input events inserted: " + sent);
    return sent;
 }
}
"@
[int[]]$keys=switch($Key){
  'Quit' {0x11;0x10;0x51}
  'Enter' {0x0d}
}
$count=[OwnedTestInput]::Insert($RootId,$Title,$keys,($Key -eq 'Enter'))
[ordered]@{ key=$Key; method='Win32 SendInput scan codes'; inserted=$count; owner=$RootId; title=$Title } | ConvertTo-Json -Compress
