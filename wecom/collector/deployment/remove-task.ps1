[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CollectorRoot,

    [string]$TaskName = 'WeCom-Incremental',

    [string]$RestoreBackup
)

$ErrorActionPreference = 'Stop'
$marker = 'WeComCollector managed task; marker=7f94c8c9-25a0-4e7e-9a0e-collect-wecom'

function Resolve-TaskSid([string]$Identity) {
    $value = $Identity.TrimStart([char]'*')
    if ($value.StartsWith('S-1-')) { return ([Security.Principal.SecurityIdentifier]::new($value)).Value }
    return ([Security.Principal.NTAccount]::new($value)).Translate([Security.Principal.SecurityIdentifier]).Value
}

try {
    $root = [IO.Path]::GetFullPath($CollectorRoot).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $scriptPath = Join-Path $root 'collect-wecom.py'
    $configPath = Join-Path $root 'config.json'
    $expectedArguments = '"' + $scriptPath + '" --config "' + $configPath + '" collect'
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    if ($null -ne $task) {
        if ($task.Description -cne $marker) { throw "Refusing to remove task '$TaskName': ownership marker does not match." }
        if ((Resolve-TaskSid ([string]$task.Principal.UserId)) -cne $sid) { throw "Refusing to remove task '$TaskName': principal is not the current user SID." }
        if (@($task.Actions).Count -ne 1) { throw "Refusing to remove task '$TaskName': it does not have exactly one action." }
        if ([IO.Path]::GetFullPath([string]$task.Actions[0].WorkingDirectory) -ine $root -or [string]$task.Actions[0].Arguments -cne $expectedArguments) {
            throw "Refusing to remove task '$TaskName': action does not target the supplied collector root."
        }
    }

    if ($RestoreBackup) {
        $backupPath = [IO.Path]::GetFullPath($RestoreBackup)
        $statePrefix = [IO.Path]::GetFullPath((Join-Path $root 'state')).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        if (-not $backupPath.StartsWith($statePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Backup metadata must be inside this collector state directory.' }
        if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) { throw "Backup metadata not found: $backupPath" }
        $meta = Get-Content -LiteralPath $backupPath -Raw | ConvertFrom-Json
        if ($meta.marker -cne $marker -or $meta.task_name -cne $TaskName -or $meta.user_sid -cne $sid -or [IO.Path]::GetFullPath([string]$meta.collector_root) -ine $root) {
            throw 'Backup metadata does not belong to this task, collector root, and current user.'
        }
        $xmlPath = [IO.Path]::GetFullPath([string]$meta.xml_path)
        if (-not $xmlPath.StartsWith($statePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Backup XML must be inside this collector state directory.' }
        if (-not (Test-Path -LiteralPath $xmlPath -PathType Leaf)) { throw "Backup task XML not found: $xmlPath" }
        [xml]$xml = Get-Content -LiteralPath $xmlPath -Raw
        if ([string]$xml.Task.RegistrationInfo.Description -cne $marker) { throw 'Backup XML does not contain this project ownership marker.' }
        if ((Resolve-TaskSid ([string]$xml.Task.Principals.Principal.UserId)) -cne $sid -or [string]$xml.Task.Principals.Principal.LogonType -cne 'InteractiveToken' -or [string]$xml.Task.Principals.Principal.RunLevel -cne 'LeastPrivilege') { throw 'Backup XML principal or privilege level does not match this collector.' }
        if (@($xml.Task.Actions.Exec).Count -ne 1 -or [IO.Path]::GetFullPath([string]$xml.Task.Actions.Exec.WorkingDirectory) -ine $root -or [string]$xml.Task.Actions.Exec.Arguments -cne $expectedArguments) { throw 'Backup XML action does not match this collector root.' }
        if ($null -ne $task) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
        Register-ScheduledTask -TaskName $TaskName -Xml (Get-Content -LiteralPath $xmlPath -Raw) -Force | Out-Null
        $restored = Get-ScheduledTask -TaskName $TaskName
        [pscustomobject]@{ TaskName = $TaskName; State = [string]$restored.State; Configuration = $root; Result = 'restored backup' } | Format-List
    }
    elseif ($null -ne $task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        [pscustomobject]@{ TaskName = $TaskName; State = 'removed'; Configuration = $root; Result = 'removed' } | Format-List
    }
    else {
        [pscustomobject]@{ TaskName = $TaskName; State = 'absent'; Configuration = $root; Result = 'no action taken' } | Format-List
    }
}
catch {
    [Console]::Error.WriteLine("Task removal or restore failed: {0}" -f $_.Exception.Message)
    exit 1
}
