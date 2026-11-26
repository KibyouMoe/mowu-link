param([Parameter(Mandatory=$true)][string]$Directory)
$ErrorActionPreference = 'Stop'
$trusted = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')

function Assert-Private([string]$Path, [bool]$IsDirectory) {
    $attributes = [IO.File]::GetAttributes($Path)
    if ([bool]($attributes -band [IO.FileAttributes]::Directory) -ne $IsDirectory -or
            ($attributes -band [IO.FileAttributes]::ReparsePoint)) { exit 3 }
    $acl = if ($IsDirectory) { [IO.Directory]::GetAccessControl($Path) } else { [IO.File]::GetAccessControl($Path) }
    $raw = [Security.AccessControl.RawSecurityDescriptor]::new($acl.Sddl)
    if ($raw.Owner.Value -notin $trusted -or $null -eq $raw.DiscretionaryAcl) {
        exit 3
    }
    foreach ($ace in $raw.DiscretionaryAcl) {
        if ($ace.AceType -eq [Security.AccessControl.AceType]::AccessDenied) { continue }
        if ($ace.AceType -ne [Security.AccessControl.AceType]::AccessAllowed -or
                $ace.SecurityIdentifier.Value -notin ($trusted + 'S-1-3-4')) {
            exit 3
        }
    }
}

Assert-Private $Directory $true
foreach ($name in @('dsh-link.json','dsh-link.crt','dsh-link.key')) {
    Assert-Private (Join-Path $Directory $name) $false
}
