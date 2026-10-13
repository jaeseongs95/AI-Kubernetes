[CmdletBinding()]
param(
    [string] $RepositoryRoot = (Join-Path $PSScriptRoot '../..'),
    [string] $PythonExecutable = 'python'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'The sidecar must be built on Windows x64. Cross-built output is not accepted.'
}
if (-not [Environment]::Is64BitProcess) {
    throw 'Use an x64 PowerShell and x64 CPython build environment.'
}

$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$SourceRoot = Join-Path $RepositoryRoot 'spikes/windows-foundation/python'
# AIK-022 probe runs the product packages (aik_vault, aik_auth_lifecycle, ...) inside the engine.
$ProductSourceRoot = Join-Path $RepositoryRoot 'src'
$EntryPath = Join-Path $SourceRoot 'sidecar_entry.py'
$BuildRoot = Join-Path $RepositoryRoot 'packaging/build'
$EngineOutput = Join-Path $BuildRoot 'engine'
$BuildEnvironment = Join-Path $BuildRoot 'venv'
$BuildPython = Join-Path $BuildEnvironment 'Scripts/python.exe'
$LockPath = Join-Path $RepositoryRoot 'packaging/requirements-build.windows.lock'

if (-not (Test-Path -LiteralPath $EntryPath -PathType Leaf)) {
    throw "Missing sidecar entry point: $EntryPath"
}
$PythonVersion = & $PythonExecutable -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
if ($LASTEXITCODE -ne 0 -or $PythonVersion.Trim() -ne '3.14.8') {
    throw "The Windows bundle requires CPython 3.14.8; observed: $PythonVersion"
}
$PythonBits = & $PythonExecutable -c 'import struct; print(struct.calcsize("P") * 8)'
if ($LASTEXITCODE -ne 0 -or $PythonBits.Trim() -ne '64') {
    throw 'The Windows bundle requires an x64 CPython interpreter.'
}

New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
& $PythonExecutable -m venv $BuildEnvironment
if ($LASTEXITCODE -ne 0) { throw 'Creating the isolated packaging environment failed.' }
& $BuildPython -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --no-deps -r $LockPath
if ($LASTEXITCODE -ne 0) { throw 'Installing the hash-locked Windows build dependencies failed.' }
& $BuildPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'The Windows build dependency lock is incomplete or incompatible.' }

& $BuildPython -m PyInstaller `
    --noconfirm --clean --onedir --console --noupx `
    --name aik-engine `
    --paths $SourceRoot `
    --paths $ProductSourceRoot `
    --hidden-import aik_spike.auth_vault `
    --hidden-import aik_vault `
    --hidden-import aik_connection `
    --hidden-import aik_auth_lifecycle `
    --hidden-import aik_provider.contracts `
    --hidden-import aik_runtime.paths `
    --hidden-import aik_security `
    --distpath $EngineOutput `
    --workpath (Join-Path $BuildRoot 'pyinstaller-work') `
    --specpath (Join-Path $BuildRoot 'pyinstaller-spec') `
    $EntryPath
if ($LASTEXITCODE -ne 0) { throw 'Freezing the Windows sidecar failed.' }

$FrozenEngine = Join-Path $EngineOutput 'aik-engine/aik-engine.exe'
$RuntimeLibraries = @(Get-ChildItem -LiteralPath (Split-Path $FrozenEngine) -Recurse -File -Filter 'python3*.dll')
if (-not (Test-Path -LiteralPath $FrozenEngine -PathType Leaf) -or $RuntimeLibraries.Count -eq 0) {
    throw 'The frozen sidecar or bundled CPython runtime DLL is missing.'
}
$BuildPackages = & $BuildPython -m pip list --format=json --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw 'Could not record the build package inventory.' }
$BuildManifest = [ordered]@{
    schema_version = 1
    scope = 'AIK-002 Windows foundation spike sidecar build'
    created_at = [DateTime]::UtcNow.ToString('o')
    source_commit = $env:GITHUB_SHA
    target = 'win32-x64'
    python = $PythonVersion.Trim()
    bundle_strategy = 'PyInstaller onedir; no external Python required at runtime'
    engine_sha256 = (Get-FileHash -LiteralPath $FrozenEngine -Algorithm SHA256).Hash.ToLowerInvariant()
    runtime_libraries = @($RuntimeLibraries | ForEach-Object {
        [ordered]@{
            file = $_.Name
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
    build_packages = @($BuildPackages | ConvertFrom-Json)
    product_integration = 'NOT_RUN'
}
$ManifestPath = Join-Path $BuildRoot 'sidecar-build.json'
$BuildManifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ManifestPath -Encoding utf8
Write-Output "Frozen Windows sidecar created: $FrozenEngine"
