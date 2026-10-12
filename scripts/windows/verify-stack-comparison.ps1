[CmdletBinding()]
param(
    [string] $RepositoryRoot = (Join-Path $PSScriptRoot '../..'),
    [string] $InstallerPath = '',
    [ValidateRange(20, 180)] [int] $TimeoutSeconds = 90
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$EvidenceRoot = Join-Path $RepositoryRoot 'packaging/evidence'
$EvidenceDirectory = Join-Path $EvidenceRoot 'tauri'
New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
foreach ($PreviousOutput in @('application-report.json', 'screenshot.png', 'screenshot-desktop-crop.png', 'application.stdout.log', 'application.stderr.log')) {
    $PreviousPath = Join-Path $EvidenceDirectory $PreviousOutput
    if (Test-Path -LiteralPath $PreviousPath -PathType Leaf) { Remove-Item -LiteralPath $PreviousPath -Force }
}
$ResultPath = Join-Path $EvidenceDirectory 'installed-spike.json'
$StartedAt = [DateTime]::UtcNow
$ValidationRoot = Join-Path ([IO.Path]::GetTempPath()) ('AIK Tauri 검증 ' + [Guid]::NewGuid().ToString('N'))
$InstallRoot = Join-Path $ValidationRoot '설치 경로'
$SmokeRoot = Join-Path $ValidationRoot '실행 증거'
$WorkingRoot = Join-Path $ValidationRoot '빈 작업 경로'
$AppPath = Join-Path $InstallRoot 'aik-tauri-comparison.exe'
$EnginePath = Join-Path $InstallRoot 'engine/aik-engine.exe'
$ApplicationProcess = $null
$ApplicationReport = $null
$OutputTask = $null
$ErrorTask = $null
$Installed = $false
$ExitCode = 1
$Result = [ordered]@{
    schema_version = 1
    scope = 'AIK-002 installed Tauri candidate, compared with the same-run Electron candidate'
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
        screenshot_capture = 'NOT_RUN'
        visual_review = 'NOT_RUN'
        uninstall = 'NOT_RUN'
    }
    webview2_supply = [ordered]@{
        configured_mode = 'offlineInstaller'
        silent = $true
        install_scope = 'currentUser'
        runtime_absent_install = 'NOT_RUN'
        note = 'The NSIS bundle embeds the Evergreen offline installer. An already installed compatible runtime can be reused; this run does not simulate runtime absence.'
    }
    limits = @(
        'The three workloads are synthetic foundation probes, not VM, AGS, vendor login, or complete product execution.',
        'Windows Server CI does not qualify a clean Windows 11 standard-user installation.',
        'Developer tools are removed from the app PATH, but remain installed on the CI host.',
        'Memory is a single live snapshot after the three probes; it is not a peak or long-run performance benchmark.',
        'Electron and Tauri native/main and renderer metrics use different process APIs and must not be combined into a universal whole-app ranking.',
        'Captured screenshots require separate visual inspection; capture success alone is not visual correctness.',
        'Signing, trusted updating, actual vendors, and product data removal remain separate release gates.'
    )
}

function Get-Sha256([string] $FilePath) {
    return (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Stop-OwnedApplication {
    if ($null -eq $ApplicationProcess) { return }
    try {
        if (-not $ApplicationProcess.HasExited) {
            & (Join-Path $env:SystemRoot 'System32/taskkill.exe') /PID $ApplicationProcess.Id /T /F | Out-Null
        }
    } catch { }
}

function Get-WebViewInstallations {
    $Found = @()
    foreach ($BasePath in @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft/EdgeWebView/Application'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft/EdgeWebView/Application')
    )) {
        if (-not (Test-Path -LiteralPath $BasePath -PathType Container)) { continue }
        foreach ($VersionDirectory in @(Get-ChildItem -LiteralPath $BasePath -Directory)) {
            $ExecutablePath = Join-Path $VersionDirectory.FullName 'msedgewebview2.exe'
            if (Test-Path -LiteralPath $ExecutablePath -PathType Leaf) {
                $Found += [ordered]@{
                    version_directory = $VersionDirectory.Name
                    file_version = [Diagnostics.FileVersionInfo]::GetVersionInfo($ExecutablePath).FileVersion
                    sha256 = Get-Sha256 $ExecutablePath
                }
            }
        }
    }
    return $Found
}

function Get-ObservedWebViewProcesses([int] $RootProcessId) {
    $Processes = @(Get-CimInstance Win32_Process)
    $OwnedIds = [Collections.Generic.HashSet[uint32]]::new()
    [void] $OwnedIds.Add([uint32] $RootProcessId)
    for ($Round = 0; $Round -lt 64; $Round++) {
        $Before = $OwnedIds.Count
        foreach ($ProcessRow in $Processes) {
            if ($OwnedIds.Contains([uint32] $ProcessRow.ParentProcessId)) {
                [void] $OwnedIds.Add([uint32] $ProcessRow.ProcessId)
            }
        }
        if ($Before -eq $OwnedIds.Count) { break }
    }
    $Observed = @()
    foreach ($ProcessRow in $Processes) {
        if (-not $OwnedIds.Contains([uint32] $ProcessRow.ProcessId) -or $ProcessRow.Name -ine 'msedgewebview2.exe') { continue }
        $Role = 'browser'
        if ($ProcessRow.CommandLine -match '(?:^|\s)--type=([^\s"]+)') { $Role = $Matches[1] }
        $Live = Get-Process -Id $ProcessRow.ProcessId -ErrorAction SilentlyContinue
        $Version = $null
        $BinaryHash = $null
        if ($ProcessRow.ExecutablePath -and (Test-Path -LiteralPath $ProcessRow.ExecutablePath -PathType Leaf)) {
            $Version = [Diagnostics.FileVersionInfo]::GetVersionInfo($ProcessRow.ExecutablePath).FileVersion
            $BinaryHash = Get-Sha256 $ProcessRow.ExecutablePath
        }
        $Bytes = $null
        if ($null -ne $Live) { $Bytes = $Live.WorkingSet64 }
        $Observed += [ordered]@{
            pid = $ProcessRow.ProcessId
            parent_pid = $ProcessRow.ParentProcessId
            role = $Role
            working_set_bytes = $Bytes
            file_version = $Version
            binary_sha256 = $BinaryHash
        }
    }
    return $Observed
}

function Save-ActualWindow([IntPtr] $WindowHandle) {
    Add-Type -AssemblyName System.Drawing
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AikComparisonWindowCapture {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr window, out RECT rect);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr window, IntPtr device, uint flags);
}
'@
    $Rectangle = New-Object AikComparisonWindowCapture+RECT
    if (-not [AikComparisonWindowCapture]::GetWindowRect($WindowHandle, [ref] $Rectangle) -or -not [AikComparisonWindowCapture]::IsWindowVisible($WindowHandle)) {
        throw 'The actual installed Tauri HWND is not a visible window.'
    }
    $Width = $Rectangle.Right - $Rectangle.Left
    $Height = $Rectangle.Bottom - $Rectangle.Top
    if ($Width -lt 100 -or $Height -lt 100 -or $Width -gt 4096 -or $Height -gt 4096) { throw 'Unexpected native window dimensions.' }
    $Bitmap = [Drawing.Bitmap]::new($Width, $Height)
    $Graphics = [Drawing.Graphics]::FromImage($Bitmap)
    try {
        $Device = $Graphics.GetHdc()
        try { $Printed = [AikComparisonWindowCapture]::PrintWindow($WindowHandle, $Device, 2) }
        finally { $Graphics.ReleaseHdc($Device) }
        if (-not $Printed) { throw 'Windows PrintWindow did not capture the visible Tauri window.' }
        $Bitmap.Save((Join-Path $EvidenceDirectory 'screenshot.png'), [Drawing.Imaging.ImageFormat]::Png)
    } finally { $Graphics.Dispose(); $Bitmap.Dispose() }
    $DesktopCapture = 'NOT_RUN'
    try {
        $Bitmap = [Drawing.Bitmap]::new($Width, $Height)
        $Graphics = [Drawing.Graphics]::FromImage($Bitmap)
        try {
            $Graphics.CopyFromScreen($Rectangle.Left, $Rectangle.Top, 0, 0, [Drawing.Size]::new($Width, $Height))
            $Bitmap.Save((Join-Path $EvidenceDirectory 'screenshot-desktop-crop.png'), [Drawing.Imaging.ImageFormat]::Png)
            $DesktopCapture = 'CAPTURED'
        } finally { $Graphics.Dispose(); $Bitmap.Dispose() }
    } catch { $DesktopCapture = 'UNAVAILABLE' }
    return [ordered]@{
        native_window_visible = $true
        width = $Width
        height = $Height
        method = 'Win32 PrintWindow PW_RENDERFULLCONTENT'
        desktop_crop = $DesktopCapture
        sha256 = Get-Sha256 (Join-Path $EvidenceDirectory 'screenshot.png')
        visual_review = 'NOT_RUN'
    }
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        $Result.status = 'BLOCKED'; $ExitCode = 2
        throw 'A real Windows x64 environment is required to compare installed desktop candidates.'
    }
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
        $Result.status = 'BLOCKED'; $ExitCode = 2
        throw 'The comparison installers only target Windows x64.'
    }
    $OperatingSystem = Get-CimInstance Win32_OperatingSystem
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
        administrator = $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        github_actions = ($env:GITHUB_ACTIONS -eq 'true')
        runner_image = $env:ImageOS
        runner_image_version = $env:ImageVersion
    }
    $Result.webview2_supply.installed_before = @(Get-WebViewInstallations)
    if ([string]::IsNullOrWhiteSpace($InstallerPath)) {
        $BundleDirectory = Join-Path $RepositoryRoot 'spikes/tauri-comparison/src-tauri/target/x86_64-pc-windows-msvc/release/bundle/nsis'
        $Installers = @(Get-ChildItem -LiteralPath $BundleDirectory -File -Filter '*.exe')
        if ($Installers.Count -ne 1) { throw "Expected exactly one Tauri NSIS installer, found $($Installers.Count)." }
        $InstallerPath = $Installers[0].FullName
    }
    $InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
    $Result.installer = [ordered]@{
        file = [IO.Path]::GetFileName($InstallerPath)
        bytes = (Get-Item -LiteralPath $InstallerPath).Length
        sha256 = Get-Sha256 $InstallerPath
        signature_status = (Get-AuthenticodeSignature -LiteralPath $InstallerPath).Status.ToString()
        release_publisher_qualified = $false
    }
    New-Item -ItemType Directory -Path $InstallRoot, $SmokeRoot, $WorkingRoot -Force | Out-Null
    # NSIS /D= is last and consumes the rest; the generated path deliberately uses spaces and Korean.
    $InstallerProcess = Start-Process -FilePath $InstallerPath -ArgumentList "/S /D=$InstallRoot" -PassThru -Wait
    if ($InstallerProcess.ExitCode -ne 0) { throw "The Tauri installer exited with code $($InstallerProcess.ExitCode)." }
    $Installed = $true
    if (-not (Test-Path -LiteralPath $AppPath -PathType Leaf)) { throw 'The installed Tauri GUI executable is missing.' }
    $Result.checks.install = 'PASS'
    if (-not (Test-Path -LiteralPath $EnginePath -PathType Leaf)) { throw 'The installed shared frozen sidecar is missing.' }
    $PythonLibraries = @(Get-ChildItem -LiteralPath (Split-Path $EnginePath) -Recurse -File -Filter 'python3*.dll')
    if ($PythonLibraries.Count -eq 0) { throw 'The installed CPython runtime DLL is missing.' }
    $Result.runtime_bundle = [ordered]@{
        gui_sha256 = Get-Sha256 $AppPath
        engine_sha256 = Get-Sha256 $EnginePath
        python_runtime_libraries = @($PythonLibraries | ForEach-Object { [ordered]@{ file = $_.Name; sha256 = Get-Sha256 $_.FullName } })
        installed_outside_source_tree = $true
        unicode_and_space_paths = $true
    }
    $Result.checks.runtime_bundle = 'PASS'
    $StartInfo = New-Object Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $AppPath
    $StartInfo.WorkingDirectory = $WorkingRoot
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.EnvironmentVariables['PATH'] = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
    $StartInfo.EnvironmentVariables['AIK_COMPARISON_REPORT_DIR'] = $SmokeRoot
    $StartInfo.EnvironmentVariables['WEBVIEW2_USER_DATA_FOLDER'] = Join-Path $SmokeRoot 'webview-profile'
    foreach ($VariableName in @('PYTHONHOME', 'PYTHONPATH', 'NODE_OPTIONS', 'ELECTRON_RUN_AS_NODE', 'AIK_SPIKE_PYTHON')) {
        $StartInfo.EnvironmentVariables.Remove($VariableName)
    }
    $Result.child_environment = [ordered]@{
        developer_tool_path_removed = $true
        pythonhome_removed = $true
        pythonpath_removed = $true
        explicit_python_override_removed = $true
        working_directory_outside_source_tree = $true
        run_specific_webview_profile = $true
        installed_tooling_absence = 'NOT_RUN'
    }
    $ApplicationProcess = New-Object Diagnostics.Process
    $ApplicationProcess.StartInfo = $StartInfo
    if (-not $ApplicationProcess.Start()) { throw 'The installed Tauri process could not be started.' }
    $OutputTask = $ApplicationProcess.StandardOutput.ReadToEndAsync()
    $ErrorTask = $ApplicationProcess.StandardError.ReadToEndAsync()
    $Result.application_pid = $ApplicationProcess.Id
    $ApplicationReportPath = Join-Path $SmokeRoot 'report.json'
    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while (-not (Test-Path -LiteralPath $ApplicationReportPath -PathType Leaf)) {
        if ($ApplicationProcess.HasExited) { throw "The Tauri window exited before producing a report (exit $($ApplicationProcess.ExitCode))." }
        if ([DateTime]::UtcNow -ge $Deadline) { throw "The Tauri GUI exceeded the $TimeoutSeconds second smoke timeout." }
        Start-Sleep -Milliseconds 250
    }
    Copy-Item -LiteralPath $ApplicationReportPath -Destination (Join-Path $EvidenceDirectory 'application-report.json') -Force
    $ApplicationReport = Get-Content -LiteralPath $ApplicationReportPath -Raw | ConvertFrom-Json
    $Result.application_report_sha256 = Get-Sha256 $ApplicationReportPath
    # Capture the live post-check process state before closing the real window.
    $Result.webview2_observation = [ordered]@{
        observed_at = [DateTime]::UtcNow.ToString('o')
        phase = 'Immediately after native post-probe report; before screenshot and application close'
        reported_runtime_version = $ApplicationReport.webview2_version
        processes = @(Get-ObservedWebViewProcesses $ApplicationProcess.Id)
        command_lines_disclosed = $false
    }
    $ApplicationProcess.Refresh()
    if ($ApplicationProcess.MainWindowHandle -eq [IntPtr]::Zero) { throw 'The running Tauri process has no native main window.' }
    $Result.screenshot = Save-ActualWindow $ApplicationProcess.MainWindowHandle
    $Result.checks.screenshot_capture = 'PASS'
    if ($ApplicationReport.schema_version -ne 1 -or $ApplicationReport.status -ne 'PASS' -or $ApplicationReport.platform -ne 'win32' -or $ApplicationReport.window_visible -ne $true -or $ApplicationReport.packaged -ne $true) {
        throw 'The actual Tauri window did not pass the packaged visible GUI contract.'
    }
    foreach ($RequiredCheck in @('health', 'worker', 'vault')) {
        if ($ApplicationReport.checks.$RequiredCheck.status -ne 'PASS') { throw "The actual Tauri renderer check did not pass: $RequiredCheck" }
    }
    if ($ApplicationReport.checks.health.frozen -ne $true -or $ApplicationReport.checks.health.python_version -ne '3.14.8') { throw 'The Tauri renderer did not reach the frozen CPython 3.14.8 sidecar.' }
    if ($ApplicationReport.native_pid -ne $ApplicationProcess.Id -or $ApplicationReport.sidecar_pid -ne $ApplicationReport.checks.health.pid) { throw 'The native report process identities do not match the actual app and sidecar.' }
    if ($ApplicationReport.metrics.main_working_set_bytes -le 0 -or @($Result.webview2_observation.processes | Where-Object { $_.role -eq 'renderer' -and $_.working_set_bytes -gt 0 }).Count -eq 0) { throw 'The live native and WebView2 renderer memory observation is incomplete.' }
    if ($ApplicationReport.product_integration -ne 'NOT_RUN' -or $ApplicationReport.renderer_node_access_status -ne 'NOT_MEASURED_BY_NATIVE_BACKEND') { throw 'The comparison must preserve unmeasured product and renderer isolation limits.' }
    if (-not $ApplicationProcess.CloseMainWindow() -or -not $ApplicationProcess.WaitForExit(15000)) { throw 'The actual Tauri window did not close gracefully.' }
    $Result.application_exit_code = $ApplicationProcess.ExitCode
    if ($ApplicationProcess.ExitCode -ne 0) { throw 'The actual Tauri GUI exited unsuccessfully.' }
    $Result.checks.packaged_gui = 'PASS'
    $Result.status = 'PASS'; $ExitCode = 0
} catch {
    $Result.error = $_.Exception.Message
    if ($Result.status -ne 'BLOCKED') { $Result.status = 'FAIL'; $ExitCode = 1 }
    # A failed renderer may never reach finish_smoke. Preserve its real screen
    # before cleanup so missing reports are diagnosable without inventing PASS.
    if ($null -ne $ApplicationProcess -and [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT -and -not $Result.Contains('screenshot')) {
        try {
            $ApplicationProcess.Refresh()
            if (-not $ApplicationProcess.HasExited -and $ApplicationProcess.MainWindowHandle -ne [IntPtr]::Zero) {
                $Result.screenshot = Save-ActualWindow $ApplicationProcess.MainWindowHandle
                $Result.checks.screenshot_capture = 'PASS'
            }
        } catch { $Result.screenshot_error = $_.Exception.Message }
    }
} finally {
    Stop-OwnedApplication
    foreach ($LogItem in @(@($OutputTask, 'application.stdout.log'), @($ErrorTask, 'application.stderr.log'))) {
        if ($null -ne $LogItem[0]) {
            try {
                if ($LogItem[0].Wait(5000)) { $LogItem[0].Result | Set-Content -LiteralPath (Join-Path $EvidenceDirectory $LogItem[1]) -Encoding utf8 }
            } catch { }
        }
    }
    if ($Installed) {
        try {
            $Uninstallers = @(Get-ChildItem -LiteralPath $InstallRoot -File -Filter '*uninstall*.exe')
            if ($Uninstallers.Count -ne 1) { throw 'The Tauri uninstaller is missing or ambiguous.' }
            $UninstallerProcess = Start-Process -FilePath $Uninstallers[0].FullName -ArgumentList '/S' -PassThru -Wait
            if ($UninstallerProcess.ExitCode -ne 0) { throw "The uninstaller exited with code $($UninstallerProcess.ExitCode)." }
            $CleanupDeadline = [DateTime]::UtcNow.AddSeconds(20)
            while ((Test-Path -LiteralPath $AppPath) -and [DateTime]::UtcNow -lt $CleanupDeadline) { Start-Sleep -Milliseconds 250 }
            if ((Test-Path -LiteralPath $AppPath) -or (Test-Path -LiteralPath $EnginePath)) { throw 'The Tauri uninstaller left application executables behind.' }
            $Result.checks.uninstall = 'PASS'
        } catch {
            $Result.checks.uninstall = 'FAIL'; $Result.uninstall_error = $_.Exception.Message
            $Result.status = 'FAIL'; $ExitCode = 1
        }
    }
    if (Test-Path -LiteralPath $ValidationRoot) {
        try { Remove-Item -LiteralPath $ValidationRoot -Recurse -Force } catch { $Result.temporary_cleanup = 'INCOMPLETE' }
    }
    $Result.completed_at = [DateTime]::UtcNow.ToString('o')
    $Result.elapsed_ms = [Math]::Round(([DateTime]::UtcNow - $StartedAt).TotalMilliseconds)
    $Result | ConvertTo-Json -Depth 18 | Set-Content -LiteralPath $ResultPath -Encoding utf8
}

$Comparison = [ordered]@{
    schema_version = 1
    scope = 'AIK-002 candidate installability, typed IPC, shared frozen worker and vault, post-check memory observations'
    source_commit = $env:GITHUB_SHA
    workflow_run_id = $env:GITHUB_RUN_ID
    status = 'BLOCKED'
    workloads = @('health', 'worker_probe', 'vault_probe')
    same_job = 'NOT_PROVEN'
    same_frozen_engine = 'NOT_PROVEN'
    tauri = $Result
    support_qualification = 'NOT_RUN'
    product_integration = 'NOT_RUN'
    performance_ranking = 'NOT_ASSERTED'
    measurement_limits = @(
        'Each candidate runs sequentially on the same CI runner with the same frozen engine; no randomized repeats or clean Windows 11 runs.',
        'Electron main_rss_bytes is Node process.memoryUsage().rss; renderer_working_set_kb is Electron getProcessMemoryInfo().workingSetSize.',
        'Tauri native main_working_set_bytes and process_tree values are Win32 GetProcessMemoryInfo WorkingSetSize, captured immediately after three checks.',
        'The separate Tauri WebView2 role snapshot uses Win32_Process identity plus .NET Process.WorkingSet64, shortly after the native report.',
        'WebView2 browser, GPU and utility processes are retained separately. Shared memory may be double-counted by working-set sums; exited worker memory is absent.',
        'No candidate total, peak, average, percentile, runtime-absent installation, or categorical performance superiority is established.',
        'Tauri offlineInstaller supplies WebView2 in its installer; Electron embeds its Chromium runtime. Installer bytes compare these specific supply choices.'
    )
}
try {
    $ElectronResult = Get-Content -LiteralPath (Join-Path $EvidenceRoot 'installed-spike.json') -Raw | ConvertFrom-Json
    $ElectronApp = Get-Content -LiteralPath (Join-Path $EvidenceRoot 'application-report.json') -Raw | ConvertFrom-Json
    $Comparison.electron = $ElectronResult
    $Comparison.electron_application = $ElectronApp
    $Comparison.tauri_application = $ApplicationReport
    if ($ElectronResult.source_commit -ne $Result.source_commit -or $ElectronResult.workflow_run_id -ne $Result.workflow_run_id -or [string]::IsNullOrWhiteSpace($Result.workflow_run_id)) { throw 'Both candidates must come from the same source and actual workflow run.' }
    $Comparison.same_job = 'PASS'
    if ($Result.status -ne 'PASS' -or $ElectronResult.status -ne 'PASS' -or $ElectronApp.status -ne 'PASS') { throw 'Both installed candidates must actually pass before comparison can pass.' }
    if ($ElectronResult.runtime_bundle.engine_sha256 -ne $Result.runtime_bundle.engine_sha256) { throw 'The installed candidate engine hashes differ.' }
    $Comparison.same_frozen_engine = 'PASS'
    $Comparison.status = 'PASS'
} catch {
    $Comparison.error = $_.Exception.Message
    if ($Result.status -eq 'FAIL') { $Comparison.status = 'FAIL' }
    if ($ExitCode -eq 0) { $ExitCode = 1 }
}
$Comparison | ConvertTo-Json -Depth 22 | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'stack-comparison.json') -Encoding utf8
Write-Output "Installed Tauri validation: $($Result.status); same-run comparison: $($Comparison.status). Evidence: $ResultPath"
if ($Result.Contains('error')) { Write-Output $Result.error }
exit $ExitCode
