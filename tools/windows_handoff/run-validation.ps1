# ASCII-only entry point for the Windows PowerShell 5.1 shipped with Windows.
[CmdletBinding()]
param(
    [string] $EvidenceParent = '',
    [ValidateRange(20, 180)] [int] $TimeoutSeconds = 90
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PackageRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
if ([string]::IsNullOrWhiteSpace($EvidenceParent)) {
    $EvidenceParent = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'AIK/ClientHandoffEvidence'
}
$ExecutionId = [Guid]::NewGuid().ToString('N')
$EvidenceRoot = Join-Path ([IO.Path]::GetFullPath($EvidenceParent)) $ExecutionId
if (Test-Path -LiteralPath $EvidenceRoot) { throw 'Refusing to reuse an existing validation directory.' }
New-Item -ItemType Directory -Path $EvidenceRoot | Out-Null
$IsolatedRoot = Join-Path $EvidenceRoot 'installed-check'
New-Item -ItemType Directory -Path $IsolatedRoot | Out-Null
$ReportPath = Join-Path $EvidenceRoot 'handoff-validation.json'
$Child = $null
$ExitCode = 1
$Result = [ordered]@{
    schema_version = 2
    execution_id = $ExecutionId
    scope = 'Independent AIK foundation installer handoff verification'
    started_at = [DateTime]::UtcNow.ToString('o')
    status = 'FAIL'
    evidence_directory = $EvidenceRoot
    package_source_commit = $null
    observed_execution = [ordered]@{
        github_actions = ($env:GITHUB_ACTIONS -eq 'true')
        github_sha = $env:GITHUB_SHA
        github_run_id = $env:GITHUB_RUN_ID
        github_run_attempt = $env:GITHUB_RUN_ATTEMPT
        powershell_version = $PSVersionTable.PSVersion.ToString()
        powershell_edition = $PSVersionTable.PSEdition
        default_encoding_code_page = [Text.Encoding]::Default.CodePage
        default_encoding_web_name = [Text.Encoding]::Default.WebName
        culture = [Globalization.CultureInfo]::CurrentCulture.Name
        ui_culture = [Globalization.CultureInfo]::CurrentUICulture.Name
        os_version = [Environment]::OSVersion.VersionString
        platform = [Environment]::OSVersion.Platform.ToString()
        process_architecture = $env:PROCESSOR_ARCHITECTURE
        is_64_bit_process = [Environment]::Is64BitProcess
    }
    checks = [ordered]@{
        package_member_hashes = 'NOT_RUN'
        verifier_bom_conversion = 'NOT_RUN'
        legacy_powershell_51 = 'NOT_RUN'
        delivered_script_parse = 'NOT_RUN'
        installed_gui_worker_dpapi_uninstall = 'NOT_RUN'
        installed_engine_identity = 'NOT_RUN'
        installed_python_dll_identity = 'NOT_RUN'
        actual_gui_evidence = 'NOT_RUN'
    }
    qualification = [ordered]@{
        windows11_standard_user_clean_machine = 'NOT_RUN'
        developer_tools_absence = 'NOT_RUN'
        release_signing = 'NOT_RUN'
        product_integration = 'NOT_RUN'
    }
    limits = @(
        'PASS applies to this package and the actual bounded foundation checks only.',
        'This is an unsigned development spike; the complete product is not implemented.',
        'An unsigned manifest verifies member consistency, not publisher authenticity.',
        'Observed Windows and user properties do not prove a clean machine or absent developer tools.',
        'A Server administrator CI execution does not qualify Windows 11 standard-user support.',
        'GitHub identity variables are inherited as observed; local execution does not synthesize them.'
    )
}

function Assert-Condition([bool] $Condition, [string] $Message) {
    if (-not $Condition) { throw $Message }
}

function Get-Sha256([string] $FilePath) {
    # Windows PowerShell started by pwsh can inherit a PSModulePath which omits
    # the legacy Get-FileHash script module. Hash with the built-in .NET runtime.
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $Stream = [IO.File]::OpenRead($FilePath)
        try {
            return ([BitConverter]::ToString($Hasher.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant()
        } finally { $Stream.Dispose() }
    } finally { $Hasher.Dispose() }
}

function Read-Json([string] $FilePath) {
    return ([IO.File]::ReadAllText($FilePath, [Text.Encoding]::UTF8) | ConvertFrom-Json)
}

function Parse-ObservedScript([string] $FilePath) {
    $Tokens = $null
    $ParseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($FilePath, [ref] $Tokens, [ref] $ParseErrors)
    return [ordered]@{
        status = $(if (@($ParseErrors).Count -eq 0) { 'PASS' } else { 'FAIL' })
        error_count = @($ParseErrors).Count
        errors = @($ParseErrors | ForEach-Object {
            [ordered]@{ id = $_.ErrorId; message = $_.Message; start_line = $_.Extent.StartLineNumber }
        })
    }
}

function Quote-Argument([string] $Value) {
    Assert-Condition (-not $Value.Contains('"')) 'A generated path contains an unsupported quotation mark.'
    return '"' + $Value + '"'
}

function Stop-OwnedChild {
    if ($null -eq $Child) { return }
    try {
        if (-not $Child.HasExited) {
            & (Join-Path $env:SystemRoot 'System32/taskkill.exe') /PID $Child.Id /T /F | Out-Null
        }
    } catch { }
}

try {
    $ManifestPath = Join-Path $PackageRoot 'handoff-manifest.json'
    Assert-Condition (Test-Path -LiteralPath $ManifestPath -PathType Leaf) 'The package manifest is missing.'
    Assert-Condition ((Get-Item -LiteralPath $ManifestPath).Length -le 1048576) 'The package manifest is too large.'
    $Manifest = Read-Json $ManifestPath
    Assert-Condition ($Manifest.schema_version -eq 1 -and $Manifest.development_artifact -eq $true) 'Unsupported handoff manifest.'
    Assert-Condition ($Manifest.source_commit -cmatch '\A[0-9a-f]{40}\z') 'Invalid package source revision.'
    $Result.package_source_commit = $Manifest.source_commit
    $Result.manifest_sha256 = Get-Sha256 $ManifestPath
    $RequiredMembers = @(
        'README.ko.md', 'run-validation.cmd', 'run-validation.ps1', 'collect-client-environment.ps1',
        'installer/AIK-Foundation-Spike-win-x64-dev-unsigned.exe',
        'verify-installed-spike.ps1', 'provenance/verify-installed-spike.original.ps1',
        'provenance/sidecar-build.json', 'provenance/installed-spike.json',
        'provenance/application-report.json', 'provenance/screenshot.png'
    )
    $Seen = @{}
    Assert-Condition (@($Manifest.files).Count -eq $RequiredMembers.Count) 'Unexpected package member count.'
    foreach ($Member in $Manifest.files) {
        $MemberName = [string] $Member.path
        Assert-Condition ($RequiredMembers -ccontains $MemberName) 'Unexpected package member path.'
        Assert-Condition (-not $Seen.ContainsKey($MemberName)) 'Duplicate package member path.'
        $Seen[$MemberName] = $true
        Assert-Condition ($Member.sha256 -cmatch '\A[0-9a-f]{64}\z') 'Invalid member hash.'
        $MemberPath = Join-Path $PackageRoot $MemberName
        Assert-Condition (Test-Path -LiteralPath $MemberPath -PathType Leaf) ('Missing package member: ' + $MemberName)
        $Item = Get-Item -LiteralPath $MemberPath
        Assert-Condition (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) 'Package members must be regular files.'
        $ParentItem = Get-Item -LiteralPath (Split-Path -Parent $MemberPath)
        Assert-Condition (($ParentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) 'Package directories must not be reparse points.'
        Assert-Condition ($Item.Length -eq $Member.bytes -and (Get-Sha256 $MemberPath) -ceq $Member.sha256) ('Changed package member: ' + $MemberName)
    }
    $Result.checks.package_member_hashes = 'PASS'
    Assert-Condition ($Manifest.installer.path -ceq 'installer/AIK-Foundation-Spike-win-x64-dev-unsigned.exe') 'Unexpected installer path.'
    Assert-Condition ($Manifest.verifier.delivered_path -ceq 'verify-installed-spike.ps1' -and
        $Manifest.verifier.original_package_path -ceq 'provenance/verify-installed-spike.original.ps1') 'Unexpected verifier paths.'
    $InstallerPath = Join-Path $PackageRoot $Manifest.installer.path
    $VerifierPath = Join-Path $PackageRoot $Manifest.verifier.delivered_path
    $OriginalPath = Join-Path $PackageRoot $Manifest.verifier.original_package_path
    Assert-Condition ((Get-Sha256 $InstallerPath) -ceq $Manifest.installer.sha256 -and
        (Get-Item -LiteralPath $InstallerPath).Length -eq $Manifest.installer.bytes) 'Installer identity is inconsistent.'
    Assert-Condition ((Get-Sha256 $VerifierPath) -ceq $Manifest.verifier.delivered_sha256 -and
        (Get-Sha256 $OriginalPath) -ceq $Manifest.verifier.original_sha256) 'Verifier identity is inconsistent.'
    $OriginalBytes = [IO.File]::ReadAllBytes($OriginalPath)
    $DeliveredBytes = [IO.File]::ReadAllBytes($VerifierPath)
    $HasOriginalBom = $OriginalBytes.Length -ge 3 -and $OriginalBytes[0] -eq 239 -and $OriginalBytes[1] -eq 187 -and $OriginalBytes[2] -eq 191
    $Offset = $(if ($HasOriginalBom) { 0 } else { 3 })
    Assert-Condition ($DeliveredBytes.Length -eq $OriginalBytes.Length + $Offset -and
        $DeliveredBytes[0] -eq 239 -and $DeliveredBytes[1] -eq 187 -and $DeliveredBytes[2] -eq 191) 'The delivered verifier requires exactly one UTF-8 BOM.'
    for ($Index = 0; $Index -lt $OriginalBytes.Length; $Index++) {
        Assert-Condition ($OriginalBytes[$Index] -eq $DeliveredBytes[$Index + $Offset]) 'The verifier transformation changed source bytes.'
    }
    $Result.checks.verifier_bom_conversion = 'PASS'
    Copy-Item -LiteralPath $ManifestPath -Destination (Join-Path $EvidenceRoot 'handoff-manifest.json')

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        $Result.status = 'BLOCKED'
        $ExitCode = 2
        throw 'Actual Windows PowerShell 5.1 and a Windows desktop are required for execution.'
    }
    Assert-Condition ($PSVersionTable.PSVersion.Major -eq 5 -and $PSVersionTable.PSVersion.Minor -eq 1 -and
        $PSVersionTable.PSEdition -eq 'Desktop') 'Run the supplied launcher with the built-in Windows PowerShell 5.1.'
    Assert-Condition ([Environment]::Is64BitProcess -and $env:PROCESSOR_ARCHITECTURE -eq 'AMD64') 'Use the built-in x64 Windows PowerShell.'
    $Result.checks.legacy_powershell_51 = 'PASS'
    $Result.legacy_script_parse = [ordered]@{
        original = Parse-ObservedScript $OriginalPath
        delivered_bom_copy = Parse-ObservedScript $VerifierPath
        original_failure_required = $false
    }
    Assert-Condition ($Result.legacy_script_parse.delivered_bom_copy.status -eq 'PASS') 'The delivered verifier failed the actual legacy parser.'
    $Result.checks.delivered_script_parse = 'PASS'

    # Collect the host before either the child PATH or the installed application
    # can change what is observed. Absence of detections is not qualification.
    $CollectorPath = Join-Path $PackageRoot 'collect-client-environment.ps1'
    Assert-Condition ((Parse-ObservedScript $CollectorPath).status -eq 'PASS') 'The environment collector failed the legacy parser.'
    . $CollectorPath
    $EnvironmentReportPath = Join-Path $EvidenceRoot 'client-environment.json'
    $ClientEnvironment = Get-AikClientEnvironment -ExecutionId $ExecutionId -ManifestSha256 $Result.manifest_sha256
    $ClientEnvironment | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $EnvironmentReportPath -Encoding utf8
    $Result.client_environment_report = 'client-environment.json'
    $Result.client_environment_sha256 = Get-Sha256 $EnvironmentReportPath
    $Result.client_environment_status = $ClientEnvironment.status
    $PreparationTemplate = [ordered]@{
        schema_version = 1
        execution_id = $ExecutionId
        manifest_sha256 = $Result.manifest_sha256
        client_environment_sha256 = $Result.client_environment_sha256
        operator = $null
        prepared_at = $null
        method = $null
        description = $null
        development_tools_installed = $null
        records = @()
    }
    $PreparationTemplate | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'client-preparation.template.json') -Encoding utf8

    $PowerShellPath = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $StartInfo = New-Object Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $PowerShellPath
    $StartInfo.WorkingDirectory = $IsolatedRoot
    $StartInfo.UseShellExecute = $false
    $StartInfo.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + (Quote-Argument $VerifierPath) +
        ' -RepositoryRoot ' + (Quote-Argument $IsolatedRoot) + ' -InstallerPath ' + (Quote-Argument $InstallerPath) +
        ' -TimeoutSeconds ' + $TimeoutSeconds
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.EnvironmentVariables['PATH'] = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
    $StartInfo.EnvironmentVariables['PSModulePath'] = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/Modules'
    foreach ($Name in @('PYTHONHOME', 'PYTHONPATH', 'NODE_OPTIONS', 'ELECTRON_RUN_AS_NODE')) {
        $StartInfo.EnvironmentVariables.Remove($Name)
    }
    $Result.child_environment = [ordered]@{
        path = $StartInfo.EnvironmentVariables['PATH']
        powershell = $PowerShellPath
        external_python_and_node_path = 'EXCLUDED'
        installed_tooling_absence = 'NOT_RUN'
        execution_policy_scope = 'child process only; no persistent policy change or elevation'
    }
    $Child = New-Object Diagnostics.Process
    $Child.StartInfo = $StartInfo
    Assert-Condition ($Child.Start()) 'Could not start the packaged verifier.'
    $OutputTask = $Child.StandardOutput.ReadToEndAsync()
    $ErrorTask = $Child.StandardError.ReadToEndAsync()
    $Result.verifier_pid = $Child.Id
    if (-not $Child.WaitForExit(($TimeoutSeconds + 150) * 1000)) {
        Stop-OwnedChild
        throw 'The owned installer validation exceeded its overall timeout.'
    }
    Assert-Condition ($OutputTask.Wait(5000) -and $ErrorTask.Wait(5000)) 'Verifier output streams did not close.'
    $OutputTask.Result | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'verifier.stdout.log') -Encoding utf8
    $ErrorTask.Result | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'verifier.stderr.log') -Encoding utf8
    $Result.verifier_exit_code = $Child.ExitCode
    $ActualDirectory = Join-Path $IsolatedRoot 'packaging/evidence'
    $ActualReportPath = Join-Path $ActualDirectory 'installed-spike.json'
    $Actual = Read-Json $ActualReportPath
    $Result.installed_report = 'installed-check/packaging/evidence/installed-spike.json'
    $Result.installed_report_sha256 = Get-Sha256 $ActualReportPath
    $Result.observed_windows_environment = $Actual.environment
    Assert-Condition ($Child.ExitCode -eq 0 -and $Actual.status -eq 'PASS') 'The actual installed foundation checks did not pass.'
    foreach ($Check in @('install', 'runtime_bundle', 'packaged_gui', 'uninstall')) {
        Assert-Condition ($Actual.checks.$Check -eq 'PASS') ('Installed check failed: ' + $Check)
    }
    Assert-Condition ($Actual.installer.sha256 -ceq $Manifest.installer.sha256 -and
        $Actual.installer.bytes -eq $Manifest.installer.bytes) 'The executed installer identity differs from the package.'
    $Result.checks.installed_gui_worker_dpapi_uninstall = 'PASS'
    Assert-Condition ($Actual.runtime_bundle.engine_sha256 -ceq $Manifest.runtime.engine_sha256) 'The installed engine differs from the expected frozen engine.'
    $Result.checks.installed_engine_identity = 'PASS'
    $ExpectedLibraries = @($Manifest.runtime.python_runtime_libraries)
    $ActualLibraries = @($Actual.runtime_bundle.python_runtime_libraries)
    Assert-Condition ($ExpectedLibraries.Count -gt 0 -and $ExpectedLibraries.Count -eq $ActualLibraries.Count) 'The installed CPython library inventory differs.'
    $LibraryNames = @{}
    foreach ($Expected in $ExpectedLibraries) {
        Assert-Condition (-not $LibraryNames.ContainsKey($Expected.file)) 'Duplicate expected CPython library.'
        $LibraryNames[$Expected.file] = $true
        $Matches = @($ActualLibraries | Where-Object { $_.file -ieq $Expected.file })
        Assert-Condition ($Matches.Count -eq 1 -and $Matches[0].sha256 -ceq $Expected.sha256) 'An installed CPython DLL differs from the expected frozen runtime.'
    }
    $Result.checks.installed_python_dll_identity = 'PASS'
    $ApplicationPath = Join-Path $ActualDirectory 'application-report.json'
    $ScreenshotPath = Join-Path $ActualDirectory 'screenshot.png'
    Assert-Condition ((Get-Sha256 $ApplicationPath) -ceq $Actual.application_report_sha256 -and
        (Get-Sha256 $ScreenshotPath) -ceq $Actual.screenshot_sha256) 'Actual GUI evidence hashes differ from the installed report.'
    $Application = Read-Json $ApplicationPath
    Assert-Condition ($Application.status -eq 'PASS' -and $Application.platform -eq 'win32' -and $Application.packaged -eq $true -and
        $Application.window_visible -eq $true -and $Application.renderer_node_access -eq $false -and
        $Application.checks.health.frozen -eq $true -and
        $Application.checks.health.python_version -ceq $Manifest.runtime.python_version) 'Actual visible GUI or frozen runtime evidence is incomplete.'
    foreach ($Check in @('health', 'worker', 'vault')) {
        Assert-Condition ($Application.checks.$Check.status -eq 'PASS') ('Actual GUI check failed: ' + $Check)
    }
    Assert-Condition ($Actual.product_integration -eq 'NOT_RUN' -and $Actual.support_qualification -eq 'NOT_RUN' -and
        $Application.product_integration -eq 'NOT_RUN') 'Foundation evidence must preserve pending product and support qualification.'
    $Result.checks.actual_gui_evidence = 'PASS'
    $Result.screenshot = 'installed-check/packaging/evidence/screenshot.png'
    $Result.visual_review = 'NOT_RUN'
    $Result.status = 'PASS'
    $ExitCode = 0
} catch {
    $Result.error = $_.Exception.Message
    if ($Result.status -ne 'BLOCKED') { $Result.status = 'FAIL'; $ExitCode = 1 }
} finally {
    Stop-OwnedChild
    $Result.completed_at = [DateTime]::UtcNow.ToString('o')
    $Result | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $ReportPath -Encoding utf8
}

Write-Output ('Handoff verification: ' + $Result.status)
Write-Output ('Evidence: ' + $ReportPath)
if ($Result.Contains('error')) { Write-Output $Result.error }
exit $ExitCode
