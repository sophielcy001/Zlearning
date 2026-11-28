param([Parameter(Mandatory=$true)][int]$RootId,[Parameter(Mandatory=$true)][string]$Executable,
      [Parameter(Mandatory=$true)][ValidateSet('智学 · Electron','智学 · Tauri')][string]$Title)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class TraySurface {
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string kind, string title);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
 [DllImport("user32.dll")] public static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
 [DllImport("user32.dll")] public static extern bool CloseDesktop(IntPtr desktop);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool GetUserObjectInformation(IntPtr desktop, int index, StringBuilder name, int size, out int needed);
}
"@
function Verify-Desktop {
    $desktop=[TraySurface]::OpenInputDesktop(0,$false,1)
    if($desktop -eq [IntPtr]::Zero){throw 'Interactive desktop is unavailable'}
    try {
        $name=[Text.StringBuilder]::new(128)
        $needed=0
        if(![TraySurface]::GetUserObjectInformation($desktop,2,$name,256,[ref]$needed) -or $name.ToString() -cne 'Default'){
            throw 'Input desktop is not Default'
        }
    } finally { [void][TraySurface]::CloseDesktop($desktop) }
}
function Verify-App {
    $app=Get-Process -Id $RootId
    if(!$app.Path -or $app.Path -ine $Executable -or $app.SessionId -ne (Get-Process -Id $PID).SessionId){
        throw 'Tray target is not the owned installed application'
    }
}
function Exact-Buttons($From,[string[]]$Names) {
    $isButton=[Windows.Automation.PropertyCondition]::new(
        [Windows.Automation.AutomationElement]::ControlTypeProperty,[Windows.Automation.ControlType]::Button)
    @(
      foreach($node in $From.FindAll([Windows.Automation.TreeScope]::Descendants,$isButton)){
                $current=$node.Current
        if($current.ProcessId -ne $explorerId -or $current.IsOffscreen -or !$current.IsEnabled){continue}
        $label=([string]$current.Name).Trim()
        if($label -cnotin $Names){continue}
        if($label -ceq $Title -and ($current.ClassName -cne 'SystemTray.NormalButton' -or
                                  $current.AutomationId -cne 'NotifyItemIcon')){continue}
        $node
      }
    )
}
function Invoke-Exact($node) {
    Verify-Desktop
    Verify-App
    $pattern=$null
    if(!$node.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)){
        throw 'The exact Explorer button did not expose Invoke'
    }
    $pattern.Invoke()
}
Verify-Desktop
Verify-App
$handle=[TraySurface]::FindWindow('Shell_TrayWnd',$null)
if($handle -eq [IntPtr]::Zero){throw 'Windows taskbar was not found'}
$explorerId=[uint32]0
[void][TraySurface]::GetWindowThreadProcessId($handle,[ref]$explorerId)
if((Get-Process -Id $explorerId).ProcessName -ine 'explorer'){throw 'Taskbar is not owned by Explorer'}
$taskbar=[Windows.Automation.AutomationElement]::FromHandle($handle)
$explorer=[Windows.Automation.PropertyCondition]::new(
    [Windows.Automation.AutomationElement]::ProcessIdProperty,[int]$explorerId)
function Overflow-Windows {
    $windows=[Windows.Automation.AutomationElement]::RootElement.FindAll([Windows.Automation.TreeScope]::Children,$explorer)
    @($windows | Where-Object {!$_.Current.IsOffscreen -and $_.Current.ClassName -match 'Overflow'})
}
$clock=[Diagnostics.Stopwatch]::StartNew()
$opened=$false
$matches=@()
$location='visible'
while($clock.ElapsedMilliseconds -lt 8000){
    try {
        $taskbar=[Windows.Automation.AutomationElement]::FromHandle($handle)
        $matches=@(Exact-Buttons $taskbar @($Title))
        if($matches.Count){break}

        $windows=@(Overflow-Windows)
        if($windows.Count){
            $matches=@(foreach($window in $windows){Exact-Buttons $window @($Title)})
            if($matches.Count){
                $location=if($opened){'overflow'}else{'already-open-overflow'}
                break
            }
        } elseif(!$opened) {
            $overflow=@(Exact-Buttons $taskbar @('显示隐藏的图标','Show hidden icons'))
            if($overflow.Count -gt 1){throw 'Explorer exposed multiple visible overflow buttons'}
            if($overflow.Count -eq 1){
                Invoke-Exact $overflow[0]
                $opened=$true
            }
        }
    } catch [Windows.Automation.ElementNotAvailableException] {
        # Explorer can rebuild its accessibility tree when opening or closing the overflow.
    }
    Start-Sleep -Milliseconds 150
}
if($matches.Count -ne 1){throw 'Explorer did not expose a unique visible button for the current app'}
$node=$matches[0]
$nativeClass=$node.Current.ClassName
$rawName=$node.Current.Name
$automationId=$node.Current.AutomationId
Invoke-Exact $node
[ordered]@{method='UI Automation Invoke'; location=$location; process='explorer'; title=$Title; controlClass=$nativeClass; rawAccessibleName=$rawName; automationId=$automationId} |
    ConvertTo-Json -Compress
