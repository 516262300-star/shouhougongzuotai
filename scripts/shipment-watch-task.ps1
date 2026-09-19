[CmdletBinding()]
param([ValidateSet('Install','Disable','Status')][string]$Action = 'Status')
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskName = 'Leedis Shipment No Trace Reminder'
if ($Action -eq 'Disable') {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Write-Output '无物流提醒计划任务已停用，正在执行的周期会正常结束'
    return
}
if ($Action -eq 'Status') {
    Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
    Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime, LastTaskResult, NextRunTime
    return
}
$pointer = Join-Path $root '.runtime/shipment-watch-release.json'
if (-not (Test-Path -LiteralPath $pointer)) { throw '缺少已核验的提醒发布指针' }
$python = Join-Path $root '.venv/Scripts/pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '缺少无窗口Python运行环境' }
$taskAction = New-ScheduledTaskAction -Execute $python -Argument ('"' + (Join-Path $root 'scripts/shipment-watch-run.py') + '"') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
# 使用已有守护任务的实际运行身份；不索取或保存用户密码。
$principal = (Get-ScheduledTask -TaskName 'Leedis Aftersales Module1 Watchdog').Principal
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description '拼多多/天猫发货满20小时无轨迹，向原销售单业务员创建人工待办' -Force | Out-Null
Write-Output '已安装每5分钟巡检；运行中不会重复启动，不产生可见窗口'
