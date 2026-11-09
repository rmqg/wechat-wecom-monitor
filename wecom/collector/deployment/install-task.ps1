[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CollectorRoot,

    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [string]$TaskName = 'WeCom-Incremental'
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
    $python = [IO.Path]::GetFullPath($PythonExe)
    $collectorScript = Join-Path $root 'collect-wecom.py'
    $config = Join-Path $root 'config.json'
    if (-not (Test-Path -LiteralPath $collectorScript -PathType Leaf)) { throw "Collector script not found: $collectorScript" }
    if (-not (Test-Path -LiteralPath $config -PathType Leaf)) { throw "Configuration not found: $config" }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python executable not found: $python" }

    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $expectedArguments = '"' + $collectorScript + '" --config "' + $config + '" collect'
    $expectedAction = New-ScheduledTaskAction -Execute $python -Argument $expectedArguments -WorkingDirectory $root
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        if ($existing.Description -cne $marker) { throw "Refusing to replace task '$TaskName': ownership marker does not match." }
        if ((Resolve-TaskSid ([string]$existing.Principal.UserId)) -cne $sid) { throw "Refusing to replace task '$TaskName': principal is not the current user SID." }
        if (@($existing.Actions).Count -ne 1) { throw "Refusing to replace task '$TaskName': it does not have exactly one action." }
        if ([string]$existing.Actions[0].Arguments -cne $expectedArguments -or [string]$existing.Actions[0].WorkingDirectory -ine $root) { throw "Refusing to replace task '$TaskName': action does not match this collector root." }
        $stateDir = Join-Path $root 'state'
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $current = [Security.Principal.SecurityIdentifier]::new($sid)
        $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
        foreach ($identity in @($current, $system)) {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
            $acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $stateDir -AclObject $acl
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
        $backupXml = Join-Path $stateDir ("task-{0}-{1}.xml" -f $TaskName, $stamp)
        $backupMeta = "$backupXml.json"
        Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $backupXml -Encoding UTF8
        $meta = [ordered]@{ marker = $marker; task_name = $TaskName; collector_root = $root; user_sid = $sid; xml_path = $backupXml; created_at = (Get-Date).ToString('o') }
        $meta | ConvertTo-Json | Set-Content -LiteralPath $backupMeta -Encoding UTF8
    }

    $action = $expectedAction
    $once = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval ([TimeSpan]::FromMinutes(1))
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $sid
    $principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10))
    $settings.Hidden = $false
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($once, $logon) -Principal $principal -Settings $settings -Description $marker -Force | Out-Null
    $registered = Get-ScheduledTask -TaskName $TaskName
    [pscustomobject]@{ TaskName = $TaskName; Triggers = 'Every 1 minute (first run in 1 minute); at user logon'; State = [string]$registered.State; Configuration = $config } | Format-List
}
catch {
    [Console]::Error.WriteLine("Task installation failed: {0}" -f $_.Exception.Message)
    exit 1
}
