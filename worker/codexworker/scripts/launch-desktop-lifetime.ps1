param(
    [Parameter(Mandatory = $true)][string]$NodePath,
    [Parameter(Mandatory = $true)][uint32]$DesktopPid,
    [ValidateSet('bridge','watchdog')][string]$Role = 'bridge'
)
$ErrorActionPreference = 'Stop'
# Create the supervisor as a child of the verified Desktop owner. Breaking out
# of only the innermost inventory job still inherits outer cleanup jobs. No job
# policy modification, elevation, alternate broker, or OS-login task is used.
if (!$env:CODEX_APP_TOOLS_PIPE_PATH -or !$env:RUDOLF_BRIDGE_DESKTOP_STARTED_AT) { throw 'Fresh Desktop environment is required.' }
$bridgeNode = (Resolve-Path -LiteralPath $NodePath).ProviderPath
if ([IO.Path]::GetFileName($bridgeNode) -ne 'node.exe') { throw 'Expected the Desktop-injected Node runtime.' }
$bridgeRoot = Split-Path -Parent $PSScriptRoot
$bridgeSupervisor = Join-Path $PSScriptRoot $(if ($Role -eq 'watchdog') { 'run-script-watchdog.mjs' } else { 'run-desktop-bridge.mjs' })
if (!(Test-Path -LiteralPath $bridgeSupervisor -PathType Leaf)) { throw 'Desktop supervisor is missing.' }
$bridgeOwner = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $DesktopPid)
if (!$bridgeOwner -or $bridgeOwner.Name -ne 'ChatGPT.exe' -or
    $bridgeOwner.ExecutablePath -notmatch '[\\/]OpenAI\.Codex_[^\\/]+[\\/]app[\\/]ChatGPT\.exe$' -or
    $bridgeOwner.CreationDate.ToUniversalTime().ToString('o') -ne $env:RUDOLF_BRIDGE_DESKTOP_STARTED_AT) {
    throw 'Desktop lifetime no longer matches.'
}
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class CodexWorkerLifetimeLaunch {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct StartupInfo {
        public uint Size;
        public string Reserved, Desktop, Title;
        public uint X, Y, XSize, YSize, XCountChars, YCountChars, FillAttribute, Flags;
        public ushort ShowWindow, Reserved2Size;
        public IntPtr Reserved2, StdInput, StdOutput, StdError;
    }
    [StructLayout(LayoutKind.Sequential)] public struct ProcessInfo {
        public IntPtr Process, Thread;
        public uint ProcessId, ThreadId;
    }
    [StructLayout(LayoutKind.Sequential)] public struct StartupInfoEx {
        public StartupInfo Startup;
        public IntPtr Attributes;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool CreateProcess(string application, StringBuilder commandLine, IntPtr processAttributes,
        IntPtr threadAttributes, bool inheritHandles, uint flags, IntPtr environment, string cwd,
        ref StartupInfoEx startup, out ProcessInfo process);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool GetProcessTimes(IntPtr process,
        out long created, out long exited, out long kernel, out long user);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool QueryFullProcessImageName(
        IntPtr process, uint flags, StringBuilder name, ref uint size);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool InitializeProcThreadAttributeList(
        IntPtr list, int count, uint flags, ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool UpdateProcThreadAttribute(
        IntPtr list, uint flags, IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returned);
    [DllImport("kernel32.dll")] public static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll")] public static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
}
'@
$bridgeStartup = New-Object CodexWorkerLifetimeLaunch+StartupInfoEx
$bridgeStartupBase = New-Object CodexWorkerLifetimeLaunch+StartupInfo
$bridgeStartupBase.Size = [Runtime.InteropServices.Marshal]::SizeOf($bridgeStartup)
$bridgeStartupBase.Flags = 1 # STARTF_USESHOWWINDOW
$bridgeStartupBase.ShowWindow = 0 # SW_HIDE
$bridgeStartup.Startup = $bridgeStartupBase
$bridgeProcessInfo = New-Object CodexWorkerLifetimeLaunch+ProcessInfo
$bridgeCommand = New-Object Text.StringBuilder
[void]$bridgeCommand.Append('"' + $bridgeNode + '" "' + $bridgeSupervisor + '" --desktop-pid ' + $DesktopPid)
# The native pipe remains solely in the inherited in-memory environment. It is
# never a command argument, disk handoff, log entry, or reconstructed old handle.
$env:RUDOLF_BRIDGE_LIFETIME_LAUNCH = 'windows-desktop-parent'
$bridgeOwnerHandle = [IntPtr]::Zero
$bridgeAttributes = [IntPtr]::Zero
$bridgeParentValue = [IntPtr]::Zero
$bridgeAttributesReady = $false
try {
    # PROCESS_CREATE_PROCESS | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE.
    # Hold the owner handle while checking creation time/image to reject PID reuse.
    $bridgeOwnerHandle = [CodexWorkerLifetimeLaunch]::OpenProcess(0x101080, $false, $DesktopPid)
    if ($bridgeOwnerHandle -eq [IntPtr]::Zero) { throw ('desktop_lifetime_launch_denied:' + [Runtime.InteropServices.Marshal]::GetLastWin32Error()) }
    [long]$bridgeCreatedAt = 0; [long]$bridgeExitedAt = 0; [long]$bridgeKernel = 0; [long]$bridgeUser = 0
    $bridgeOwnerImage = New-Object Text.StringBuilder 32768
    [uint32]$bridgeOwnerImageSize = $bridgeOwnerImage.Capacity
    $bridgeExpectedCreatedAt = [DateTime]::Parse($env:RUDOLF_BRIDGE_DESKTOP_STARTED_AT, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    if (![CodexWorkerLifetimeLaunch]::GetProcessTimes($bridgeOwnerHandle, [ref]$bridgeCreatedAt, [ref]$bridgeExitedAt, [ref]$bridgeKernel, [ref]$bridgeUser) -or
        ![CodexWorkerLifetimeLaunch]::QueryFullProcessImageName($bridgeOwnerHandle, 0, $bridgeOwnerImage, [ref]$bridgeOwnerImageSize) -or
        $bridgeOwnerImage.ToString() -ne $bridgeOwner.ExecutablePath -or
        [CodexWorkerLifetimeLaunch]::WaitForSingleObject($bridgeOwnerHandle, 0) -ne 258) {
        throw 'desktop_lifetime_owner_changed'
    }
    # CIM retains microseconds; the held handle reports 100ns FILETIME ticks.
    $bridgeCreationDelta = [DateTime]::FromFileTimeUtc($bridgeCreatedAt).Ticks - $bridgeExpectedCreatedAt.Ticks
    if ($bridgeCreationDelta -lt 0 -or $bridgeCreationDelta -gt 9) { throw 'desktop_lifetime_owner_changed' }
    $bridgeAttributeSize = [IntPtr]::Zero
    [void][CodexWorkerLifetimeLaunch]::InitializeProcThreadAttributeList([IntPtr]::Zero, 1, 0, [ref]$bridgeAttributeSize)
    $bridgeAttributes = [Runtime.InteropServices.Marshal]::AllocHGlobal($bridgeAttributeSize)
    if (![CodexWorkerLifetimeLaunch]::InitializeProcThreadAttributeList($bridgeAttributes, 1, 0, [ref]$bridgeAttributeSize)) {
        throw ('desktop_lifetime_launch_denied:' + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    $bridgeAttributesReady = $true
    $bridgeParentValue = [Runtime.InteropServices.Marshal]::AllocHGlobal([IntPtr]::Size)
    [Runtime.InteropServices.Marshal]::WriteIntPtr($bridgeParentValue, $bridgeOwnerHandle)
    # PROC_THREAD_ATTRIBUTE_PARENT_PROCESS, accepted only with OS-granted access
    # to this exact live Desktop. The supervisor inherits Desktop's own job.
    if (![CodexWorkerLifetimeLaunch]::UpdateProcThreadAttribute($bridgeAttributes, 0, [IntPtr]0x20000,
        $bridgeParentValue, [IntPtr]([IntPtr]::Size), [IntPtr]::Zero, [IntPtr]::Zero)) {
        throw ('desktop_lifetime_launch_denied:' + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    $bridgeStartup.Attributes = $bridgeAttributes
    # CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT. No inventory job breakaway
    # or alternate parent fallback: an OS rejection is the final safe error.
    if (![CodexWorkerLifetimeLaunch]::CreateProcess($bridgeNode, $bridgeCommand, [IntPtr]::Zero,
        [IntPtr]::Zero, $false, 0x08080000, [IntPtr]::Zero, $bridgeRoot, [ref]$bridgeStartup, [ref]$bridgeProcessInfo)) {
        throw ('desktop_lifetime_launch_denied:' + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    [Console]::WriteLine('{"supervisorPid":' + $bridgeProcessInfo.ProcessId + ',"launchMode":"windows-desktop-parent"}')
} catch {
    $bridgeSafeError = if ($_.Exception.Message -match '^desktop_lifetime_launch_denied:\d{1,5}$') { $_.Exception.Message } else { 'desktop_lifetime_launch_unavailable' }
    [Console]::Error.WriteLine($bridgeSafeError)
    exit 1
}
finally {
    if ($bridgeProcessInfo.Thread -ne [IntPtr]::Zero) { [void][CodexWorkerLifetimeLaunch]::CloseHandle($bridgeProcessInfo.Thread) }
    if ($bridgeProcessInfo.Process -ne [IntPtr]::Zero) { [void][CodexWorkerLifetimeLaunch]::CloseHandle($bridgeProcessInfo.Process) }
    if ($bridgeAttributesReady) { [CodexWorkerLifetimeLaunch]::DeleteProcThreadAttributeList($bridgeAttributes) }
    if ($bridgeAttributes -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::FreeHGlobal($bridgeAttributes) }
    if ($bridgeParentValue -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::FreeHGlobal($bridgeParentValue) }
    if ($bridgeOwnerHandle -ne [IntPtr]::Zero) { [void][CodexWorkerLifetimeLaunch]::CloseHandle($bridgeOwnerHandle) }
}
