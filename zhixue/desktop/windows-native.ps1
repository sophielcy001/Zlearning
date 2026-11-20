param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('machine', 'snapshot', 'visible', 'close', 'minimize', 'quit', 'dialog')]
  [string]$Mode,
  [int]$RootId,
  [string]$WindowTitle,
  [string]$WebProfile,
  [string]$DialogTitle,
  [string]$Choice,
  [switch]$DefaultChoice,
  [switch]$FocusAssist
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Write-Result($Value) {
  ConvertTo-Json -InputObject $Value -Depth 7 -Compress
}

if ($Mode -eq 'machine') {
  $os = Get-CimInstance Win32_OperatingSystem
  $system = Get-CimInstance Win32_ComputerSystem
  $cpu = @(Get-CimInstance Win32_Processor | Select-Object Name, Architecture, NumberOfLogicalProcessors)
  $video = @(Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion)
  Write-Result @{
    os = $os.Caption; build = $os.BuildNumber; version = $os.Version
    logical = [int]$system.NumberOfLogicalProcessors; ramBytes = [double]$system.TotalPhysicalMemory
    cpu = $cpu; video = $video
  }
  return
}

if ($Mode -eq 'snapshot') {
  $nativeProcesses = @{}
  foreach ($running in Get-Process) { $nativeProcesses[$running.Id] = $running }
  $processes = @(
    foreach ($process in Get-CimInstance Win32_Process) {
      $owned = $nativeProcesses[[int]$process.ProcessId]
      if ($null -eq $owned -or $null -eq $process.CreationDate) { continue }
      $webview = ($WebProfile -and $process.Name -ieq 'msedgewebview2.exe' -and $process.CommandLine -and
        $process.CommandLine.IndexOf($WebProfile, [StringComparison]::OrdinalIgnoreCase) -ge 0)
      try {
        $born = ([DateTimeOffset]$owned.StartTime).ToUnixTimeMilliseconds()
        $cimBorn = ([DateTimeOffset]$process.CreationDate).ToUnixTimeMilliseconds()
        if ([Math]::Abs($born - $cimBorn) -gt 2) { continue }
        @{
          pid = [int]$process.ProcessId; parent = [int]$process.ParentProcessId; name = $process.Name
          born = $born; cpuMs = [double]$owned.TotalProcessorTime.TotalMilliseconds
          working = [double]$owned.WorkingSet64; privateBytes = [double]$owned.PrivateMemorySize64
          webview = [bool]$webview
        }
      } catch {
        # Protected and exited processes do not have usable statistics.
        continue
      }
    }
  )
  $at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  $gpu = $null
  try {
    $class = 'Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine'
    $gpu = @(
      foreach ($engine in Get-CimInstance -ClassName $class -ErrorAction Stop) {
        if ($engine.Name -match '^pid_(\d+)_(.+)$') {
          @{ pid = [int]$Matches[1]; engine = $Matches[2]; percent = [double]$engine.UtilizationPercentage }
        }
      }
    )
  } catch {
    $gpu = $null
  }
  Write-Result @{ at = $at; rows = $processes; gpu = $gpu }
  return
}

if ($RootId -le 0) { throw 'A running desktop process ID is required.' }
$null = Get-Process -Id $RootId

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class EvaluationWindow {
  private delegate bool WindowVisitor(IntPtr window, IntPtr context);
  [DllImport("user32.dll")]
  private static extern bool EnumChildWindows(IntPtr parent, WindowVisitor visitor, IntPtr context);
  [DllImport("user32.dll")]
  private static extern bool IsWindowVisible(IntPtr window);
  [DllImport("user32.dll")]
  private static extern bool IsWindowEnabled(IntPtr window);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)]
  private static extern int GetClassName(IntPtr window, StringBuilder className, int capacity);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)]
  private static extern int GetWindowText(IntPtr window, StringBuilder text, int capacity);
  [DllImport("user32.dll", SetLastError=true)]
  public static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")]
  public static extern bool ShowWindowAsync(IntPtr window, int command);
  [DllImport("user32.dll")]
  public static extern bool IsIconic(IntPtr window);
  [DllImport("user32.dll")]
  public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr window);

  [StructLayout(LayoutKind.Sequential)]
  private struct GuiThreadInfo {
    public uint size, flags;
    public IntPtr active, focus, capture, menuOwner, moveSize, caret;
    public int left, top, right, bottom;
  }

  [DllImport("user32.dll")]
  private static extern bool GetGUIThreadInfo(uint thread, ref GuiThreadInfo info);

  public static bool IsButtonFocused(IntPtr dialog, IntPtr button) {
    uint owner;
    uint thread = GetWindowThreadProcessId(dialog, out owner);
    var info = new GuiThreadInfo { size = (uint)Marshal.SizeOf(typeof(GuiThreadInfo)) };
    return thread != 0 && GetGUIThreadInfo(thread, ref info) && info.active == dialog && info.focus == button;
  }
  [DllImport("user32.dll")]
  public static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

  public static IntPtr FindVisibleWindow(uint processId, string title) {
    return FindVisible(processId, title, IntPtr.Zero);
  }

  public static IntPtr FindVisibleButton(uint processId, string title, IntPtr dialog) {
    return FindVisible(processId, title, dialog);
  }

  private static IntPtr FindVisible(uint processId, string title, IntPtr dialog) {
    var result = IntPtr.Zero;
    var caption = new StringBuilder(title.Length + 2);
    var className = new StringBuilder(64);
    EnumChildWindows(dialog, (window, context) => {
      uint owner;
      GetWindowThreadProcessId(window, out owner);
      if (owner != processId || !IsWindowVisible(window)) return true;
      if (dialog != IntPtr.Zero &&
          (!IsWindowEnabled(window) || GetClassName(window, className, className.Capacity) == 0 ||
           !String.Equals(className.ToString(), "Button", StringComparison.Ordinal))) return true;
      if (GetWindowText(window, caption, caption.Capacity) == 0 ||
          !String.Equals(caption.ToString(), title, StringComparison.Ordinal)) return true;
      result = window;
      return false;
    }, IntPtr.Zero);
    return result;
  }
}
'@

if ($Mode -in @('visible', 'close', 'minimize', 'quit')) {
  if ([string]::IsNullOrWhiteSpace($WindowTitle)) { throw 'The learning window title is required.' }
  $learningWindow = [EvaluationWindow]::FindVisibleWindow($RootId, $WindowTitle)
}

if ($Mode -eq 'visible') {
  Write-Result @{ visible = $learningWindow -ne [IntPtr]::Zero }
  return
}

if ($Mode -eq 'close') {
  if ($learningWindow -eq [IntPtr]::Zero -or
      -not [EvaluationWindow]::PostMessage($learningWindow, 16, [IntPtr]::Zero, [IntPtr]::Zero)) {
    throw 'The owned window could not be closed.'
  }
  Write-Result @{ acknowledged = $true }
  return
}

if ($Mode -eq 'minimize') {
  if ($learningWindow -eq [IntPtr]::Zero -or -not [EvaluationWindow]::ShowWindowAsync($learningWindow, 6)) {
    throw 'The owned window could not be minimized.'
  }
  $clock = [Diagnostics.Stopwatch]::StartNew()
  while ($clock.ElapsedMilliseconds -lt 5000) {
    if ([EvaluationWindow]::IsIconic($learningWindow)) {
      Write-Result @{ minimized = $true }
      return
    }
    Start-Sleep -Milliseconds 75
  }
  throw 'The owned window did not minimize.'
}

function Test-WindowForeground([IntPtr]$Window) {
  if ([EvaluationWindow]::GetForegroundWindow() -ne $Window) { return $false }
  [uint32]$owner = 0
  $null = [EvaluationWindow]::GetWindowThreadProcessId($Window, [ref]$owner)
  return $owner -eq $RootId
}

function Send-WindowKey([string]$Key, [IntPtr]$Window, [IntPtr]$Button = [IntPtr]::Zero, $AccessibleButton = $null) {
  [uint32]$owner = 0
  $null = [EvaluationWindow]::GetWindowThreadProcessId($Window, [ref]$owner)
  if ($owner -ne $RootId) { throw 'The owned keyboard target was unavailable.' }
  $keyboard = New-Object -ComObject WScript.Shell
  if (-not $FocusAssist -and -not (Test-WindowForeground $Window)) {
    $null = [EvaluationWindow]::SetForegroundWindow($Window)
  }
  $limit = if ($FocusAssist) { 30000 } else { 2000 }
  $waiting = [Diagnostics.Stopwatch]::StartNew()
  while ($waiting.ElapsedMilliseconds -lt $limit) {
    if (Test-WindowForeground $Window) {
      $ready = if ($Button -ne [IntPtr]::Zero) {
        [EvaluationWindow]::IsButtonFocused($Window, $Button)
      } elseif ($null -ne $AccessibleButton) {
        $AccessibleButton.Current.HasKeyboardFocus
      } else {
        $true
      }
      if ($ready -and (Test-WindowForeground $Window)) {
        $keyboard.SendKeys($Key)
        return
      }
    }
    Start-Sleep -Milliseconds 75
  }
  if (-not (Test-WindowForeground $Window)) {
    throw 'The test window did not acquire keyboard focus. No key was sent.'
  }
  throw 'The expected default button did not acquire keyboard focus. No key was sent.'
}

if ($Mode -eq 'quit') {
  Send-WindowKey '^+q' $learningWindow
  Write-Result @{ acknowledged = $true }
  return
}

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$condition = [System.Windows.Automation.AndCondition]::new(
  [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty, $Choice),
  [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
)
$clock = [Diagnostics.Stopwatch]::StartNew()
while ($clock.ElapsedMilliseconds -lt 10000) {
  # Owned Win32 dialogs need not appear as direct children of the accessibility desktop.
  $dialog = [EvaluationWindow]::FindVisibleWindow($RootId, $DialogTitle)
  if ($dialog -ne [IntPtr]::Zero) {
    # A standard TaskDialog can expose its buttons as panes without an Invoke pattern.
    $nativeButton = [EvaluationWindow]::FindVisibleButton($RootId, $Choice, $dialog)
    if ($nativeButton -ne [IntPtr]::Zero) {
      if ($DefaultChoice) {
        Send-WindowKey '{ENTER}' $dialog $nativeButton
      } elseif (-not [EvaluationWindow]::PostMessage($nativeButton, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)) {
        throw 'The owned timer button could not be clicked.'
      }
    } else {
      $window = [System.Windows.Automation.AutomationElement]::FromHandle($dialog)
      if ($window.Current.ProcessId -ne $RootId) { continue }
      $button = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
      if ($null -eq $button) { continue }
      if ($DefaultChoice) {
        Send-WindowKey '{ENTER}' $dialog -AccessibleButton $button
      } else {
        $pattern = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $pattern.Invoke()
      }
    }
    $closed = [Diagnostics.Stopwatch]::StartNew()
    while ($closed.ElapsedMilliseconds -lt 4500) {
      if ([EvaluationWindow]::FindVisibleWindow($RootId, $DialogTitle) -eq [IntPtr]::Zero) {
        Write-Result @{ acknowledged = $true }
        return
      }
      Start-Sleep -Milliseconds 100
    }
    throw 'The owned timer dialog remained visible after the choice.'
  }
  Start-Sleep -Milliseconds 100
}
throw 'The expected native timer dialog or choice did not appear.'
