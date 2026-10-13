[CmdletBinding()]
param(
    [string] $RepositoryRoot = (Join-Path $PSScriptRoot '../..'),
    [string] $InstallerPath = '',
    [ValidateRange(20, 180)] [int] $TimeoutSeconds = 90
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$EvidenceDirectory = Join-Path $RepositoryRoot 'packaging/evidence'
New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
foreach ($PreviousOutput in @('application-report.json', 'screenshot.png', 'application.stdout.log', 'application.stderr.log')) {
    $PreviousPath = Join-Path $EvidenceDirectory $PreviousOutput
    if (Test-Path -LiteralPath $PreviousPath -PathType Leaf) { Remove-Item -LiteralPath $PreviousPath -Force }
}
$ResultPath = Join-Path $EvidenceDirectory 'installed-spike.json'
$StartedAt = [DateTime]::UtcNow
$ValidationRoot = Join-Path ([IO.Path]::GetTempPath()) ('AIK 검증 ' + [Guid]::NewGuid().ToString('N'))
$InstallRoot = Join-Path $ValidationRoot '설치 경로'
$SmokeRoot = Join-Path $ValidationRoot '실행 증거'
$WorkingRoot = Join-Path $ValidationRoot '빈 작업 경로'
$AppPath = Join-Path $InstallRoot 'AIK Foundation Spike.exe'
$EnginePath = Join-Path $InstallRoot 'resources/engine/aik-engine.exe'
$ApplicationProcess = $null
$Installed = $false
$ExitCode = 1
$Result = [ordered]@{
    schema_version = 1
    scope = 'AIK-002 packaged Windows foundation spike'
    source_commit = $env:GITHUB_SHA
    workflow_run_id = $env:GITHUB_RUN_ID
    workflow_attempt = $env:GITHUB_RUN_ATTEMPT
    started_at = $StartedAt.ToString('o')
    status = 'FAIL'
    development_artifact = $true
    support_qualification = 'NOT_RUN'
    product_integration = 'NOT_RUN'
    checks = [ordered]@{
        install = 'NOT_RUN'
        runtime_bundle = 'NOT_RUN'
        packaged_gui = 'NOT_RUN'
        uninstall = 'NOT_RUN'
    }
    limits = @(
        'This installer contains an AIK-002 foundation spike, not the complete product.',
        'A GitHub Server runner does not qualify Windows 11 client support.',
        'Removing developer tools from the child PATH does not make the runner a clean computer.',
        'No real vendor, AGS, or VM product integration is exercised.',
        'Windows 11 standard-user installation, publisher signing, update trust, and product data removal remain separate release gates.'
    )
}

function Get-Sha256([string] $FilePath) {
    return (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Stop-OwnedApplication {
    if ($null -eq $ApplicationProcess) { return }
    try {
        if (-not $ApplicationProcess.HasExited) {
            # Only the process started by this test and its descendants are targeted.
            & (Join-Path $env:SystemRoot 'System32/taskkill.exe') /PID $ApplicationProcess.Id /T /F | Out-Null
        }
    } catch {
        # The original validation error is more useful than an already-exited PID.
    }
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        $Result.status = 'BLOCKED'
        $ExitCode = 2
        throw 'A real Windows x64 environment is required to execute the installed spike.'
    }
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
        $Result.status = 'BLOCKED'
        $ExitCode = 2
        throw 'The spike installer only targets Windows x64.'
    }
    $OperatingSystem = Get-CimInstance Win32_OperatingSystem
    $Processors = @(Get-CimInstance Win32_Processor)
    $Computer = Get-CimInstance Win32_ComputerSystem
    $Principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    $Result.environment = [ordered]@{
        platform = 'win32'
        architecture = $env:PROCESSOR_ARCHITECTURE
        os_caption = $OperatingSystem.Caption
        os_version = $OperatingSystem.Version
        os_build = $OperatingSystem.BuildNumber
        os_product_type = $OperatingSystem.ProductType
        logical_processors = $Computer.NumberOfLogicalProcessors
        memory_bytes = $Computer.TotalPhysicalMemory
        cpu_names = @($Processors | ForEach-Object { $_.Name })
        administrator = $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        github_actions = ($env:GITHUB_ACTIONS -eq 'true')
        runner_image = $env:ImageOS
        runner_image_version = $env:ImageVersion
    }
    if ([string]::IsNullOrWhiteSpace($InstallerPath)) {
        $Installers = @(Get-ChildItem -LiteralPath (Join-Path $RepositoryRoot 'packaging/dist') -File -Filter 'AIK-Foundation-Spike-*-win-x64-dev-unsigned.exe')
        if ($Installers.Count -ne 1) {
            throw "Expected exactly one built development installer, found $($Installers.Count)."
        }
        $InstallerPath = $Installers[0].FullName
    }
    $InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
    $Signature = Get-AuthenticodeSignature -LiteralPath $InstallerPath
    $Result.installer = [ordered]@{
        file = [IO.Path]::GetFileName($InstallerPath)
        bytes = (Get-Item -LiteralPath $InstallerPath).Length
        sha256 = Get-Sha256 $InstallerPath
        signature_status = $Signature.Status.ToString()
        release_publisher_qualified = $false
    }
    New-Item -ItemType Directory -Path $InstallRoot, $SmokeRoot, $WorkingRoot -Force | Out-Null
    # NSIS /D= must be the last argument and consumes the remaining command line.
    # Generated paths cannot contain quotation marks; spaces and Korean are deliberate.
    $InstallerProcess = Start-Process -FilePath $InstallerPath -ArgumentList "/S /D=$InstallRoot" -PassThru -Wait
    if ($InstallerProcess.ExitCode -ne 0) {
        throw "The development installer exited with code $($InstallerProcess.ExitCode)."
    }
    $Installed = $true
    if (-not (Test-Path -LiteralPath $AppPath -PathType Leaf)) {
        throw 'The installer did not produce the expected GUI executable.'
    }
    $Result.checks.install = 'PASS'
    if (-not (Test-Path -LiteralPath $EnginePath -PathType Leaf)) {
        throw 'The installed sidecar executable is missing.'
    }
    $PythonLibraries = @(Get-ChildItem -LiteralPath (Split-Path $EnginePath) -Recurse -File -Filter 'python3*.dll')
    if ($PythonLibraries.Count -eq 0) { throw 'The installed CPython runtime DLL is missing.' }
    $Result.runtime_bundle = [ordered]@{
        gui_sha256 = Get-Sha256 $AppPath
        engine_sha256 = Get-Sha256 $EnginePath
        python_runtime_libraries = @($PythonLibraries | ForEach-Object {
            [ordered]@{ file = $_.Name; sha256 = Get-Sha256 $_.FullName }
        })
        installed_outside_source_tree = $true
        unicode_and_space_paths = $true
    }
    $Result.checks.runtime_bundle = 'PASS'

    $StartInfo = New-Object Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $AppPath
    $StartInfo.WorkingDirectory = $WorkingRoot
    $StartInfo.UseShellExecute = $false
    $StartInfo.Arguments = '--spike-smoke "--report-dir=' + $SmokeRoot + '"'
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.EnvironmentVariables['PATH'] = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
    foreach ($VariableName in @('PYTHONHOME', 'PYTHONPATH', 'NODE_OPTIONS', 'ELECTRON_RUN_AS_NODE')) {
        $StartInfo.EnvironmentVariables.Remove($VariableName)
    }
    $Result.child_environment = [ordered]@{
        path = $StartInfo.EnvironmentVariables['PATH']
        pythonhome_removed = $true
        pythonpath_removed = $true
        node_options_removed = $true
        electron_run_as_node_removed = $true
        working_directory_outside_source_tree = $true
        installed_tooling_absence = 'NOT_RUN'
    }
    $ApplicationProcess = New-Object Diagnostics.Process
    $ApplicationProcess.StartInfo = $StartInfo
    if (-not $ApplicationProcess.Start()) { throw 'The installed GUI process could not be started.' }
    $OutputTask = $ApplicationProcess.StandardOutput.ReadToEndAsync()
    $ErrorTask = $ApplicationProcess.StandardError.ReadToEndAsync()
    $Result.application_pid = $ApplicationProcess.Id
    if (-not $ApplicationProcess.WaitForExit($TimeoutSeconds * 1000)) {
        Stop-OwnedApplication
        throw "The installed GUI exceeded the $TimeoutSeconds second smoke timeout."
    }
    $Result.application_exit_code = $ApplicationProcess.ExitCode
    if (-not $OutputTask.Wait(5000) -or -not $ErrorTask.Wait(5000)) {
        throw 'The application exited without closing its subprocess output streams.'
    }
    $OutputTask.Result | Set-Content -LiteralPath (Join-Path $EvidenceDirectory 'application.stdout.log') -Encoding utf8
    $ErrorTask.Result | Set-Content -LiteralPath (Join-Path $EvidenceDirectory 'application.stderr.log') -Encoding utf8

    $ApplicationReportPath = Join-Path $SmokeRoot 'report.json'
    if (-not (Test-Path -LiteralPath $ApplicationReportPath -PathType Leaf)) {
        throw 'The installed GUI did not create its own validation report.'
    }
    Copy-Item -LiteralPath $ApplicationReportPath -Destination (Join-Path $EvidenceDirectory 'application-report.json') -Force
    $ApplicationReport = Get-Content -LiteralPath $ApplicationReportPath -Raw | ConvertFrom-Json
    if ($ApplicationReport.status -eq 'BLOCKED' -or $ApplicationProcess.ExitCode -eq 2) {
        $Result.status = 'BLOCKED'
        $ExitCode = 2
        throw 'The actual installed GUI reported a blocked foundation-spike check.'
    }
    if ($ApplicationProcess.ExitCode -ne 0 -or $ApplicationReport.schema_version -ne 1 -or $ApplicationReport.status -ne 'PASS') {
        throw 'The actual installed GUI did not pass its smoke contract.'
    }
    if ($ApplicationReport.platform -ne 'win32' -or $ApplicationReport.window_visible -ne $true -or $ApplicationReport.renderer_node_access -ne $false) {
        throw 'The report did not prove a visible Windows GUI with renderer Node access disabled.'
    }
    foreach ($RequiredCheck in @('health', 'worker', 'vault', 'auth_vault')) {
        if ($ApplicationReport.checks.$RequiredCheck.status -ne 'PASS') {
            throw "The installed GUI check did not pass: $RequiredCheck"
        }
    }
    if ($ApplicationReport.checks.health.frozen -ne $true -or $ApplicationReport.checks.health.python_version -ne '3.14.8') {
        throw 'The renderer did not reach the frozen CPython 3.14.8 sidecar.'
    }
    if ($ApplicationReport.product_integration -ne 'NOT_RUN') {
        throw 'The foundation spike must not claim real product integration.'
    }
    $ScreenshotPath = Join-Path $SmokeRoot 'screenshot.png'
    if (-not (Test-Path -LiteralPath $ScreenshotPath -PathType Leaf) -or (Get-Item -LiteralPath $ScreenshotPath).Length -lt 8) {
        throw 'The actual GUI screenshot is missing.'
    }
    Copy-Item -LiteralPath $ScreenshotPath -Destination (Join-Path $EvidenceDirectory 'screenshot.png') -Force
    $Result.application_report_sha256 = Get-Sha256 $ApplicationReportPath
    $Result.screenshot_sha256 = Get-Sha256 $ScreenshotPath
    $Result.checks.packaged_gui = 'PASS'
    $Result.status = 'PASS'
    $ExitCode = 0
} catch {
    $Result.error = $_.Exception.Message
    if ($Result.status -ne 'BLOCKED') { $Result.status = 'FAIL'; $ExitCode = 1 }
} finally {
    Stop-OwnedApplication
    if ($Installed) {
        try {
            $Uninstallers = @(Get-ChildItem -LiteralPath $InstallRoot -File -Filter 'Uninstall*.exe')
            if ($Uninstallers.Count -ne 1) { throw 'The installed uninstaller is missing or ambiguous.' }
            $UninstallerProcess = Start-Process -FilePath $Uninstallers[0].FullName -ArgumentList '/S' -PassThru -Wait
            if ($UninstallerProcess.ExitCode -ne 0) { throw "The uninstaller exited with code $($UninstallerProcess.ExitCode)." }
            $CleanupDeadline = [DateTime]::UtcNow.AddSeconds(20)
            while ((Test-Path -LiteralPath $AppPath) -and [DateTime]::UtcNow -lt $CleanupDeadline) {
                Start-Sleep -Milliseconds 250
            }
            if ((Test-Path -LiteralPath $AppPath) -or (Test-Path -LiteralPath $EnginePath)) {
                throw 'The uninstaller left installed application executables behind.'
            }
            $Result.checks.uninstall = 'PASS'
        } catch {
            $Result.checks.uninstall = 'FAIL'
            $Result.uninstall_error = $_.Exception.Message
            $Result.status = 'FAIL'
            $ExitCode = 1
        }
    }
    # Only this run's generated temporary directory is removed. Electron's
    # smoke profile lives under SmokeRoot. The DPAPI probe separately removes
    # its own run directory under the Windows known LocalAppData folder.
    if (Test-Path -LiteralPath $ValidationRoot) {
        try { Remove-Item -LiteralPath $ValidationRoot -Recurse -Force } catch {
            $Result.temporary_cleanup = 'INCOMPLETE'
        }
    }
    $Result.completed_at = [DateTime]::UtcNow.ToString('o')
    $Result.elapsed_ms = [Math]::Round(([DateTime]::UtcNow - $StartedAt).TotalMilliseconds)
    $Result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ResultPath -Encoding utf8
}

Write-Output "Installed foundation-spike validation: $($Result.status). Evidence: $ResultPath"
if ($Result.Contains('error')) { Write-Output $Result.error }
exit $ExitCode
