param([Parameter(Mandatory=$true)][string]$Directory, [switch]$CheckAdmissions, [switch]$CheckQuestions, [string]$MediaDigests)
$ErrorActionPreference = 'Stop'
$trusted = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')

function Assert-Private([string]$Path, [bool]$IsDirectory, [bool]$RequireFileInheritance = $false, [int]$Refusal = 3) {
    $attributes = [IO.File]::GetAttributes($Path)
    if ([bool]($attributes -band [IO.FileAttributes]::Directory) -ne $IsDirectory -or
            ($attributes -band [IO.FileAttributes]::ReparsePoint)) { exit $Refusal }
    $acl = if ($IsDirectory) { [IO.Directory]::GetAccessControl($Path) } else { [IO.File]::GetAccessControl($Path) }
    $raw = [Security.AccessControl.RawSecurityDescriptor]::new($acl.Sddl)
    if ($raw.Owner.Value -notin $trusted -or $null -eq $raw.DiscretionaryAcl) {
        exit $Refusal
    }
    $fileInheritance = $false
    foreach ($ace in $raw.DiscretionaryAcl) {
        if ($ace.AceType -eq [Security.AccessControl.AceType]::AccessDenied) { continue }
        if ($ace.AceType -ne [Security.AccessControl.AceType]::AccessAllowed -or
                $ace.SecurityIdentifier.Value -notin ($trusted + 'S-1-3-4')) {
            exit $Refusal
        }
        if (([int]$ace.AceFlags -band [int][Security.AccessControl.AceFlags]::ObjectInherit) -and
                $ace.SecurityIdentifier.Value -eq $trusted[0]) {
            $fileInheritance = $true
        }
    }
    if ($RequireFileInheritance -and -not $fileInheritance) { exit 4 }
}

Assert-Private $Directory $true ($CheckAdmissions.IsPresent -or $CheckQuestions.IsPresent)
foreach ($name in @('dsh-link.json','dsh-link.crt','dsh-link.key')) {
    Assert-Private (Join-Path $Directory $name) $false
}
$ledgers = @()
if ($CheckAdmissions) { $ledgers += 'dsh-admissions.sqlite3' }
if ($CheckQuestions) { $ledgers += 'dsh-questions.sqlite3' }
$directoryInfo = [IO.DirectoryInfo]::new($Directory)
foreach ($ledger in $ledgers) {
    foreach ($entry in $directoryInfo.EnumerateFileSystemInfos($ledger + '*')) {
        if ($entry.Name -notin @($ledger,($ledger + '-journal'))) { exit 3 }
        Assert-Private $entry.FullName $false
    }
}
if ($MediaDigests) {
    if ($MediaDigests -cnotmatch '\A[a-f0-9]{64}(,[a-f0-9]{64}){0,9}\z') { exit 5 }
    $cache = Join-Path $Directory 'media'
    Assert-Private $cache $true $false 5
    foreach ($sha256 in $MediaDigests.Split(',')) {
        Assert-Private (Join-Path $cache $sha256) $false $false 5
    }
}
