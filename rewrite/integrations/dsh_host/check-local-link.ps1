param([Parameter(Mandatory=$true)][string]$Directory)
$ErrorActionPreference = 'Stop'
$trusted = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')

function Assert-Private([string]$Path, [bool]$IsDirectory) {
    $entry = Get-Item -LiteralPath $Path -Force
    if ($entry.PSIsContainer -ne $IsDirectory -or ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'private_dsh_link_required'
    }
    $acl = Get-Acl -LiteralPath $Path
    $raw = New-Object Security.AccessControl.RawSecurityDescriptor($acl.Sddl)
    if ($raw.Owner.Value -notin $trusted -or $null -eq $raw.DiscretionaryAcl) {
        throw 'private_dsh_link_required'
    }
    foreach ($ace in $raw.DiscretionaryAcl) {
        if ($ace.AceType -eq [Security.AccessControl.AceType]::AccessDenied) { continue }
        if ($ace.AceType -ne [Security.AccessControl.AceType]::AccessAllowed -or
                $ace.SecurityIdentifier.Value -notin ($trusted + 'S-1-3-4')) {
            throw 'private_dsh_link_required'
        }
    }
}

Assert-Private $Directory $true
foreach ($name in @('dsh-link.json','dsh-link.crt','dsh-link.key')) {
    Assert-Private (Join-Path $Directory $name) $false
}
