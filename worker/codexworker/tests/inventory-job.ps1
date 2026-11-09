param([Parameter(Mandatory = $true)][uint32]$InventoryPid, [switch]$Nested)
$ErrorActionPreference = 'Stop'
# A test-only, unnamed Windows resource job. Only a sibling process of this
# test driver can be assigned; never use this helper on a production process.
$bridgeJobSelf = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID)
$bridgeJobTarget = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $InventoryPid)
if (!$bridgeJobTarget -or $bridgeJobTarget.ParentProcessId -ne $bridgeJobSelf.ParentProcessId -or $bridgeJobTarget.Name -ne 'node.exe') {
    throw 'Inventory job target is not owned by this test driver.'
}
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class RudolfInventoryJob {
    [StructLayout(LayoutKind.Sequential)] public struct BasicLimits {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)] public struct IoCounters {
        public ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes;
    }
    [StructLayout(LayoutKind.Sequential)] public struct ExtendedLimits {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits limits, uint size);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool QueryInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits limits, uint size, IntPtr returned);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
}
'@
$bridgeJobHandle = [RudolfInventoryJob]::CreateJobObject([IntPtr]::Zero, $null)
if ($bridgeJobHandle -eq [IntPtr]::Zero) { throw 'Could not create isolated inventory job.' }
$bridgeInventoryHandle = [IntPtr]::Zero
$bridgeOuterJob = [IntPtr]::Zero
try {
    $bridgeJobLimits = New-Object RudolfInventoryJob+ExtendedLimits
    $bridgeJobBasic = New-Object RudolfInventoryJob+BasicLimits
    $bridgeJobBasic.Flags = 0x2800 # KILL_ON_JOB_CLOSE | BREAKAWAY_OK: resource ownership, not a security sandbox.
    $bridgeJobLimits.Basic = $bridgeJobBasic
    if (![RudolfInventoryJob]::SetInformationJobObject($bridgeJobHandle, 9, [ref]$bridgeJobLimits, [Runtime.InteropServices.Marshal]::SizeOf($bridgeJobLimits))) {
        throw 'Could not configure isolated inventory job.'
    }
    $bridgeActualLimits = New-Object RudolfInventoryJob+ExtendedLimits
    if (![RudolfInventoryJob]::QueryInformationJobObject($bridgeJobHandle, 9, [ref]$bridgeActualLimits,
        [Runtime.InteropServices.Marshal]::SizeOf($bridgeActualLimits), [IntPtr]::Zero) -or $bridgeActualLimits.Basic.Flags -ne 0x2800) {
        throw 'Isolated job limits were not applied.'
    }
    $bridgeInventoryHandle = [RudolfInventoryJob]::OpenProcess(0x501, $false, $InventoryPid)
    if ($Nested) {
        $bridgeOuterJob = [RudolfInventoryJob]::CreateJobObject([IntPtr]::Zero, $null)
        $bridgeOuterLimits = New-Object RudolfInventoryJob+ExtendedLimits
        $bridgeOuterBasic = New-Object RudolfInventoryJob+BasicLimits
        $bridgeOuterBasic.Flags = 0x2000 # Outer cleanup job does not allow breakaway.
        $bridgeOuterLimits.Basic = $bridgeOuterBasic
        if ($bridgeOuterJob -eq [IntPtr]::Zero -or
            ![RudolfInventoryJob]::SetInformationJobObject($bridgeOuterJob, 9, [ref]$bridgeOuterLimits, [Runtime.InteropServices.Marshal]::SizeOf($bridgeOuterLimits)) -or
            ![RudolfInventoryJob]::AssignProcessToJobObject($bridgeOuterJob, $bridgeInventoryHandle)) {
            throw 'Could not configure isolated outer inventory job.'
        }
    }
    if ($bridgeInventoryHandle -eq [IntPtr]::Zero -or ![RudolfInventoryJob]::AssignProcessToJobObject($bridgeJobHandle, $bridgeInventoryHandle)) {
        throw 'Could not assign test-owned inventory process.'
    }
    [Console]::WriteLine('{"assigned":true}')
    [Console]::Out.Flush()
    [void][Console]::ReadLine()
} finally {
    if ($bridgeInventoryHandle -ne [IntPtr]::Zero) { [void][RudolfInventoryJob]::CloseHandle($bridgeInventoryHandle) }
    [void][RudolfInventoryJob]::CloseHandle($bridgeJobHandle)
    if ($bridgeOuterJob -ne [IntPtr]::Zero) { [void][RudolfInventoryJob]::CloseHandle($bridgeOuterJob) }
}
