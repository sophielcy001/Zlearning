param(
  [Parameter(Mandatory=$true)][int]$RootId,
  [Parameter(Mandatory=$true)][string]$Executable,
  [switch]$InvokeDefault
)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$app=Get-Process -Id $RootId
if ($app.Path -ine (Resolve-Path -LiteralPath $Executable).Path) { throw 'Unexpected executable for the isolated app.' }
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class OwnedDefaultDialog {
  private const uint DM_GETDEFID = 0x0400, WM_GETDLGCODE = 0x0087, BM_CLICK = 0x00F5;
  private const ushort DC_HASDEFID = 0x534B, DLGC_DEFPUSHBUTTON = 0x0010;
  private const int GWL_STYLE = -16, BS_TYPEMASK = 0x000F, BS_DEFPUSHBUTTON = 1;
  private delegate bool Visitor(IntPtr window, IntPtr context);
  [DllImport("user32.dll")] private static extern bool EnumWindows(Visitor visitor, IntPtr context);
  [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr window, Visitor visitor, IntPtr context);
  [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
  [DllImport("user32.dll")] private static extern bool IsWindowEnabled(IntPtr window);
  [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
  [DllImport("user32.dll")] private static extern int GetDlgCtrlID(IntPtr window);
  [DllImport("user32.dll")] private static extern IntPtr GetDlgItem(IntPtr dialog, int id);
  [DllImport("user32.dll")] private static extern int GetWindowLong(IntPtr window, int index);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr window, StringBuilder value, int size);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder name, int size);
  [DllImport("user32.dll", SetLastError=true)] private static extern IntPtr SendMessageTimeout(
    IntPtr window, uint message, IntPtr wParam, IntPtr lParam, uint flags, uint timeout, out UIntPtr value);
  [DllImport("user32.dll", SetLastError=true)] private static extern bool PostMessage(
    IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
  public sealed class ButtonFact {
    public int Id { get; set; }
    public string Caption { get; set; }
    public bool DefaultStyle { get; set; }
    public bool? DefaultDialogCode { get; set; }
    public bool Enabled { get; set; }
  }
  public sealed class Evidence {
    public int? DefaultId { get; set; }
    public string DefaultCaption { get; set; }
    public string DefaultEvidence { get; set; }
    public string DialogClass { get; set; }
    public ButtonFact[] Buttons { get; set; }
    public bool Invoked { get; set; }
  }
  private static uint Owner(IntPtr window) {
    uint pid;
    GetWindowThreadProcessId(window, out pid);
    return pid;
  }
  private static string Text(IntPtr window, bool className) {
    var text = new StringBuilder(160);
    if (className) GetClassName(window, text, text.Capacity);
    else GetWindowText(window, text, text.Capacity);
    return text.ToString();
  }
  public static Evidence Inspect(uint pid, string title, string expected, bool invoke) {
    IntPtr dialog = IntPtr.Zero;
    EnumWindows((handle, unused) => {
      if (Owner(handle) != pid || !IsWindowVisible(handle) || Text(handle, false) != title) return true;
      dialog = handle;
      return false;
    }, IntPtr.Zero);
    if (dialog == IntPtr.Zero) return null;
    var buttons = new List<ButtonFact>();
    var styledDefaults = new List<IntPtr>();
    EnumChildWindows(dialog, (handle, unused) => {
      if (Owner(handle) == pid && IsWindowVisible(handle) && Text(handle, true) == "Button") {
        UIntPtr code;
        bool? defaultCode = SendMessageTimeout(handle, WM_GETDLGCODE, IntPtr.Zero, IntPtr.Zero, 3, 500, out code) == IntPtr.Zero
          ? (bool?)null : (code.ToUInt64() & DLGC_DEFPUSHBUTTON) != 0;
        var fact = new ButtonFact {
          Id = GetDlgCtrlID(handle), Caption = Text(handle, false), DefaultDialogCode = defaultCode,
          DefaultStyle = (GetWindowLong(handle, GWL_STYLE) & BS_TYPEMASK) == BS_DEFPUSHBUTTON, Enabled = IsWindowEnabled(handle)
        };
        buttons.Add(fact);
        if (fact.DefaultStyle && fact.DefaultDialogCode == true && fact.Enabled) styledDefaults.Add(handle);
      }
      return true;
    }, IntPtr.Zero);
    UIntPtr result;
    var proof = new Evidence { DialogClass = Text(dialog, true), Buttons = buttons.ToArray() };
    IntPtr button = IntPtr.Zero;
    if (SendMessageTimeout(dialog, DM_GETDEFID, IntPtr.Zero, IntPtr.Zero, 3, 1000, out result) != IntPtr.Zero) {
      ulong number = result.ToUInt64();
      if (number >> 16 == DC_HASDEFID) {
        proof.DefaultId = (int)(number & 0xFFFF);
        IntPtr candidate = GetDlgItem(dialog, proof.DefaultId.Value);
        if (candidate != IntPtr.Zero && Owner(candidate) == pid && IsWindowVisible(candidate) &&
            IsWindowEnabled(candidate) && Text(candidate, true) == "Button") {
          button = candidate;
          proof.DefaultEvidence = "DM_GETDEFID";
        }
      }
    }
    // Task Dialog can expose an ID without a matching child HWND. Corroborate the default through both button attributes.
    if (button == IntPtr.Zero && styledDefaults.Count == 1) {
      button = styledDefaults[0];
      proof.DefaultEvidence = "BS_DEFPUSHBUTTON+DLGC_DEFPUSHBUTTON";
    }
    if (button == IntPtr.Zero) return proof;
    proof.DefaultCaption = Text(button, false);
    if (invoke && proof.DefaultCaption == expected && Owner(dialog) == pid && Owner(button) == pid) {
      if (!PostMessage(button, BM_CLICK, IntPtr.Zero, IntPtr.Zero)) throw new InvalidOperationException("The default control could not be invoked.");
      proof.Invoked = true;
    }
    return proof;
  }
}
'@
$watch=[Diagnostics.Stopwatch]::StartNew()
while($watch.ElapsedMilliseconds -lt 9000) {
  $result=[OwnedDefaultDialog]::Inspect([uint32]$RootId,'智学 · 完全退出','结束计时并退出',[bool]$InvokeDefault)
  if($null -ne $result) {
    ConvertTo-Json -InputObject $result -Depth 5 -Compress
    return
  }
  Start-Sleep -Milliseconds 100
}
throw 'The owned exit dialog did not appear.'
