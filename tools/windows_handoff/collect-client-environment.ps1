# ASCII-only Windows PowerShell 5.1 library. Dot-sourcing performs no probes.
# The caller must invoke this before changing PATH or other host environment.
function Get-AikClientEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('\A[0-9a-f]{32}\z')]
        [string] $ExecutionId,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('\A[0-9a-f]{64}\z')]
        [string] $ManifestSha256
    )

    Set-StrictMode -Version Latest
    $ErrorActionPreference = 'Stop'
    # ValidatePattern is case-insensitive in PowerShell; enforce lowercase here.
    if ($ExecutionId -cnotmatch '\A[0-9a-f]{32}\z' -or
        $ManifestSha256 -cnotmatch '\A[0-9a-f]{64}\z') {
        throw 'ExecutionId and ManifestSha256 must be lowercase hexadecimal.'
    }
    $ProbeNames = @('operating_system', 'user_token', 'hardware',
        'path_commands', 'uninstall_registry', 'known_locations')
    $ProbeStates = [ordered]@{}
    foreach ($ProbeName in $ProbeNames) { $ProbeStates[$ProbeName] = 'PASS' }
    $ObservedErrors = New-Object 'System.Collections.Generic.List[object]'
    $Detections = New-Object 'System.Collections.Generic.List[object]'
    $Aliases = New-Object 'System.Collections.Generic.List[object]'
    $SeenDetections = @{}
    $SeenAliases = @{}
    $InspectedDotnet = @{}
    $Result = [ordered]@{
        schema_version = 1
        execution_id = $ExecutionId
        manifest_sha256 = $ManifestSha256
        observed_at = [DateTime]::UtcNow.ToString('o')
        status = 'INCOMPLETE'
        os = [ordered]@{
            platform = [Environment]::OSVersion.Platform.ToString()
            version = [Environment]::OSVersion.Version.ToString()
            build = 0
            product_type = 0
            architecture = ''
            edition_id = ''
            display_version = ''
        }
        user = [ordered]@{ elevated = $null; administrator_group_member = $null }
        hardware = [ordered]@{ logical_processors = 0; memory_bytes = [long] 0 }
        tool_inventory = [ordered]@{
            status = 'INCOMPLETE'
            scope = ('Read-only original PATH executable names; HKLM/HKCU 32/64-bit uninstall registrations; ' +
                'bounded conventional install directories for Python, Node/npm, Git, Rust, Visual Studio/MSBuild, Java and .NET SDK. ' +
                'No detected program is launched. No whole-disk, other-user, package-manager or remote-directory inventory. ' +
                'An empty result does not establish tool absence or a clean machine. WindowsApps Python aliases are ' +
                'reported separately and do not establish whether their target is installed. A dotnet host or shipped .NET runtime alone is excluded.')
            probes = @()
            detections = @()
            app_execution_aliases = @()
        }
        errors = @()
    }

    function Add-ProbeError([string] $Probe, [string] $Message) {
        $ProbeStates[$Probe] = 'FAIL'
        # Keep diagnostics bounded even if a local registry contains many errors.
        if ($ObservedErrors.Count -lt 128) {
            if ($Message.Length -gt 1024) { $Message = $Message.Substring(0, 1024) }
            $ObservedErrors.Add([ordered]@{ probe = $Probe; message = $Message })
        }
    }

    function Add-Detection([string] $Tool, [string] $Source, [string] $Location) {
        $Identity = ($Tool + '|' + $Source + '|' + $Location).ToLowerInvariant()
        if (-not $SeenDetections.ContainsKey($Identity)) {
            if ($Detections.Count -ge 1024) {
                Add-ProbeError $Source 'The 1024-detection limit was reached; inventory is incomplete.'
                return
            }
            $SeenDetections[$Identity] = $true
            $Detections.Add([ordered]@{ tool = $Tool; source = $Source; location = $Location })
        }
    }

    function Get-LocalItem([string] $Candidate, [string] $Probe) {
        try {
            $FullPath = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Candidate))
            if ($FullPath.StartsWith('\\') -or $FullPath -notmatch '\A[A-Za-z]:\\') {
                Add-ProbeError $Probe ('A non-local or unsupported path was not inspected: ' + $Candidate)
                return $null
            }
            $DriveRoot = [IO.Path]::GetPathRoot($FullPath)
            $Drive = New-Object IO.DriveInfo $DriveRoot
            if ($Drive.DriveType -eq [IO.DriveType]::Network) {
                Add-ProbeError $Probe ('A network drive was not inspected: ' + $Candidate)
                return $null
            }
            # Check ancestors before looking at the leaf. A local drive can
            # contain a junction into a remote or otherwise excluded directory.
            # The final leaf may be a WindowsApps alias and is inspected below.
            $Parts = $FullPath.Substring($DriveRoot.Length).Split('\')
            $AncestorPath = $DriveRoot
            for ($PartIndex = 0; $PartIndex -lt $Parts.Length; $PartIndex++) {
                $Ancestor = Get-Item -LiteralPath $AncestorPath -Force -ErrorAction Stop
                if (-not $Ancestor.PSIsContainer -or
                    ($Ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    Add-ProbeError $Probe ('A non-directory or reparse-point ancestor was not traversed: ' + $AncestorPath)
                    return $null
                }
                if ($PartIndex -lt $Parts.Length - 1) {
                    $AncestorPath = Join-Path $AncestorPath $Parts[$PartIndex]
                }
            }
            return Get-Item -LiteralPath $FullPath -Force -ErrorAction Stop
        } catch [System.Management.Automation.ItemNotFoundException] {
            return $null
        } catch {
            Add-ProbeError $Probe ('Cannot inspect ' + $Candidate + ': ' + $_.Exception.Message)
            return $null
        }
    }

    function Get-ChildDirectories([string] $Parent, [string] $Probe, [string] $Pattern = '*') {
        $ParentItem = Get-LocalItem $Parent $Probe
        if ($null -eq $ParentItem) { return }
        if (-not $ParentItem.PSIsContainer) {
            Add-ProbeError $Probe ('Expected an install directory: ' + $Parent)
            return
        }
        if (($ParentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Add-ProbeError $Probe ('A reparse-point directory was not enumerated: ' + $Parent)
            return
        }
        try {
            $DirectoryCount = 0
            foreach ($ChildDirectory in [IO.Directory]::EnumerateDirectories($ParentItem.FullName, $Pattern)) {
                if ($DirectoryCount -ge 128) {
                    Add-ProbeError $Probe ('The 128-subdirectory limit was reached: ' + $Parent)
                    break
                }
                $DirectoryCount++
                Write-Output $ChildDirectory
            }
        } catch {
            Add-ProbeError $Probe ('Cannot enumerate ' + $Parent + ': ' + $_.Exception.Message)
        }
    }

    function Inspect-Executable([string] $Candidate, [string] $Tool, [string] $Probe) {
        $Executable = Get-LocalItem $Candidate $Probe
        if ($null -eq $Executable) { return }
        if ($Executable.PSIsContainer) {
            Add-ProbeError $Probe ('An executable path is a directory: ' + $Candidate)
            return
        }
        $IsPythonAlias = $Tool -eq 'python' -and
            -not [string]::IsNullOrWhiteSpace($LocalApplicationData) -and
            ([IO.Path]::GetDirectoryName($Executable.FullName).TrimEnd('\') -ieq
                (Join-Path $LocalApplicationData 'Microsoft\WindowsApps').TrimEnd('\')) -and
            ($Executable.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        if ($IsPythonAlias) {
            $AliasIdentity = $Executable.FullName.ToLowerInvariant()
            if (-not $SeenAliases.ContainsKey($AliasIdentity)) {
                $SeenAliases[$AliasIdentity] = $true
                $Aliases.Add([ordered]@{ tool = $Tool; location = $Executable.FullName })
            }
            return
        }
        Add-Detection $Tool $Probe $Executable.FullName
    }

    function Inspect-DotnetSdk([string] $DotnetRoot, [string] $Probe) {
        $SdkIdentity = ($Probe + '|' + $DotnetRoot).ToLowerInvariant()
        if ($InspectedDotnet.ContainsKey($SdkIdentity)) { return }
        $InspectedDotnet[$SdkIdentity] = $true
        foreach ($VersionRoot in @(Get-ChildDirectories (Join-Path $DotnetRoot 'sdk') $Probe)) {
            # A host, shared runtime, or empty leftover sdk directory is not an SDK.
            $SdkDriver = Get-LocalItem (Join-Path $VersionRoot 'dotnet.dll') $Probe
            $SdkBuild = Get-LocalItem (Join-Path $VersionRoot 'MSBuild.dll') $Probe
            if (($null -ne $SdkDriver -and -not $SdkDriver.PSIsContainer) -or
                ($null -ne $SdkBuild -and -not $SdkBuild.PSIsContainer)) {
                Add-Detection 'dotnet_sdk' $Probe $VersionRoot
            }
        }
    }

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        foreach ($ProbeName in $ProbeNames) {
            Add-ProbeError $ProbeName 'Not run: this collector requires actual Windows.'
        }
        $Result.status = 'UNSUPPORTED'
    } else {
        $Result.os.platform = 'win32'
        $LocalApplicationData = [Environment]::GetFolderPath('LocalApplicationData')
        $RoamingApplicationData = [Environment]::GetFolderPath('ApplicationData')
        $UserProfileRoot = [Environment]::GetFolderPath('UserProfile')
        $ProgramRoots = @([Environment]::GetFolderPath('ProgramFiles'),
            [Environment]::GetFolderPath('ProgramFilesX86'), $env:ProgramW6432) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique

        try {
            $OperatingSystems = @(Get-CimInstance -ClassName Win32_OperatingSystem -OperationTimeoutSec 10 -ErrorAction Stop)
            if ($OperatingSystems.Count -ne 1) { throw 'Expected one local operating system observation.' }
            $OperatingSystem = $OperatingSystems[0]
            $BuildNumber = [int] $OperatingSystem.BuildNumber
            $ProductType = [int] $OperatingSystem.ProductType
            if ($BuildNumber -le 0 -or $ProductType -notin @(1, 2, 3) -or
                [string]::IsNullOrWhiteSpace($OperatingSystem.Version)) {
                throw 'The operating system observation is incomplete.'
            }
            # Environment architecture can describe an emulated process. Use
            # the locally observed processor platform instead of trusting it.
            $Processors = @(Get-CimInstance -ClassName Win32_Processor -OperationTimeoutSec 10 -ErrorAction Stop)
            if ($Processors.Count -eq 0) { throw 'No local processor architecture was observed.' }
            $Architectures = @($Processors | ForEach-Object {
                if ($null -eq $_.Architecture) { throw 'A processor architecture is missing.' }
                [int] $_.Architecture
            } | Select-Object -Unique)
            $ArchitectureNames = @{ 0 = 'x86'; 1 = 'MIPS'; 2 = 'ALPHA'; 3 = 'POWERPC'; 5 = 'ARM'; 6 = 'IA64'; 9 = 'AMD64'; 12 = 'ARM64' }
            if ($Architectures.Count -ne 1 -or -not $ArchitectureNames.ContainsKey($Architectures[0])) {
                throw 'The local processor architecture is mixed or unsupported by this collector.'
            }
            $Result.os.version = [string] $OperatingSystem.Version
            $Result.os.build = $BuildNumber
            $Result.os.product_type = $ProductType
            $Result.os.architecture = $ArchitectureNames[$Architectures[0]]
            $WindowsBaseKey = $null
            $WindowsVersionKey = $null
            try {
                $WindowsBaseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
                    [Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
                $WindowsVersionKey = $WindowsBaseKey.OpenSubKey('SOFTWARE\Microsoft\Windows NT\CurrentVersion', $false)
                if ($null -eq $WindowsVersionKey) { throw 'The Windows version registry key is missing.' }
                $EditionId = $WindowsVersionKey.GetValue('EditionID', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                $DisplayVersion = $WindowsVersionKey.GetValue('DisplayVersion', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                if ($EditionId -isnot [string] -or [string]::IsNullOrWhiteSpace($EditionId) -or $EditionId.Length -gt 64 -or
                    $DisplayVersion -isnot [string] -or [string]::IsNullOrWhiteSpace($DisplayVersion) -or $DisplayVersion.Length -gt 64) {
                    throw 'Windows EditionID or DisplayVersion is missing or invalid.'
                }
                $Result.os.edition_id = $EditionId
                $Result.os.display_version = $DisplayVersion
            } finally {
                if ($null -ne $WindowsVersionKey) { $WindowsVersionKey.Dispose() }
                if ($null -ne $WindowsBaseKey) { $WindowsBaseKey.Dispose() }
            }
        } catch { Add-ProbeError 'operating_system' $_.Exception.Message }

        try {
            $Computers = @(Get-CimInstance -ClassName Win32_ComputerSystem -OperationTimeoutSec 10 -ErrorAction Stop)
            if ($Computers.Count -ne 1) { throw 'Expected one local hardware observation.' }
            $LogicalProcessors = [int] $Computers[0].NumberOfLogicalProcessors
            $MemoryBytes = [long] $Computers[0].TotalPhysicalMemory
            if ($LogicalProcessors -le 0 -or $MemoryBytes -le 0) { throw 'The hardware observation is incomplete.' }
            $Result.hardware.logical_processors = $LogicalProcessors
            $Result.hardware.memory_bytes = $MemoryBytes
        } catch { Add-ProbeError 'hardware' $_.Exception.Message }

        try {
            # TokenGroups exposes deny-only SIDs; IsInRole alone would miss a
            # filtered administrator. Query only the current process token.
            if ($null -eq ('AikClientEvidence.TokenReaderV1' -as [type])) {
                Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
namespace AikClientEvidence {
    public sealed class TokenObservationV1 {
        public bool Elevated;
        public bool AdministratorGroupMember;
    }
    public static class TokenReaderV1 {
        [StructLayout(LayoutKind.Sequential)]
        private struct SidAndAttributes { public IntPtr Sid; public uint Attributes; }
        [StructLayout(LayoutKind.Sequential)]
        private struct TokenGroupsFirst { public uint Count; public SidAndAttributes Group; }
        [DllImport("kernel32.dll")] private static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);
        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr buffer, int length, out int needed);
        public static TokenObservationV1 Read() {
            IntPtr token = IntPtr.Zero, elevation = IntPtr.Zero, groups = IntPtr.Zero;
            try {
                if (!OpenProcessToken(GetCurrentProcess(), 0x0008, out token))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                int needed;
                elevation = Marshal.AllocHGlobal(4);
                if (!GetTokenInformation(token, 20, elevation, 4, out needed) || needed != 4)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot read TokenElevation.");
                int elevationValue = Marshal.ReadInt32(elevation);
                if (elevationValue != 0 && elevationValue != 1)
                    throw new InvalidOperationException("Invalid TokenElevation value.");
                bool unexpected = GetTokenInformation(token, 2, IntPtr.Zero, 0, out needed);
                int sizeError = Marshal.GetLastWin32Error();
                if (unexpected || sizeError != 122 || needed < 4 || needed > 1048576)
                    throw new InvalidOperationException("Cannot bound TokenGroups buffer.");
                int capacity = needed;
                groups = Marshal.AllocHGlobal(capacity);
                if (!GetTokenInformation(token, 2, groups, capacity, out needed) || needed > capacity)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot read TokenGroups.");
                int count = Marshal.ReadInt32(groups);
                int offset = Marshal.OffsetOf(typeof(TokenGroupsFirst), "Group").ToInt32();
                int stride = Marshal.SizeOf(typeof(SidAndAttributes));
                if (count < 1 || count > 4096 || (long)offset + (long)count * stride > needed)
                    throw new InvalidOperationException("Invalid TokenGroups bounds.");
                bool administrator = false;
                for (int index = 0; index < count; index++) {
                    IntPtr entry = IntPtr.Add(groups, offset + index * stride);
                    SidAndAttributes group = (SidAndAttributes)Marshal.PtrToStructure(entry, typeof(SidAndAttributes));
                    if (group.Sid == IntPtr.Zero) throw new InvalidOperationException("TokenGroups has a null SID.");
                    // Include all attributes, especially SE_GROUP_USE_FOR_DENY_ONLY.
                    if (new SecurityIdentifier(group.Sid).Value == "S-1-5-32-544") administrator = true;
                }
                return new TokenObservationV1 { Elevated = elevationValue != 0, AdministratorGroupMember = administrator };
            } finally {
                if (groups != IntPtr.Zero) Marshal.FreeHGlobal(groups);
                if (elevation != IntPtr.Zero) Marshal.FreeHGlobal(elevation);
                if (token != IntPtr.Zero) CloseHandle(token);
            }
        }
    }
}
'@ -ErrorAction Stop
            }
            $TokenObservation = [AikClientEvidence.TokenReaderV1]::Read()
            $Result.user.elevated = [bool] $TokenObservation.Elevated
            $Result.user.administrator_group_member = [bool] $TokenObservation.AdministratorGroupMember
        } catch { Add-ProbeError 'user_token' $_.Exception.Message }

        $CommandFiles = [ordered]@{
            'python.exe' = 'python'; 'python3.exe' = 'python'; 'pythonw.exe' = 'python'; 'py.exe' = 'python'
            'node.exe' = 'node'; 'npm.cmd' = 'npm'; 'npm.ps1' = 'npm'; 'git.exe' = 'git'
            'rustc.exe' = 'rust'; 'cargo.exe' = 'rust'; 'rustup.exe' = 'rust'
            'devenv.exe' = 'visual_studio'; 'Code.exe' = 'visual_studio'; 'MSBuild.exe' = 'msbuild'
            'java.exe' = 'java'; 'javac.exe' = 'java'; 'jshell.exe' = 'java'
        }
        try {
            if ([string]::IsNullOrWhiteSpace($env:PATH)) { throw 'The original host PATH is empty or unavailable.' }
            $PathDirectories = @($env:PATH.Split(';') | ForEach-Object { $_.Trim().Trim('"') } |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
            if ($PathDirectories.Count -gt 256) { throw 'The original PATH exceeds the 256-directory limit.' }
            foreach ($PathDirectory in $PathDirectories) {
                $PathItem = Get-LocalItem $PathDirectory 'path_commands'
                if ($null -eq $PathItem) { continue }
                if (-not $PathItem.PSIsContainer) {
                    Add-ProbeError 'path_commands' ('A PATH entry is not a directory: ' + $PathDirectory)
                    continue
                }
                if (($PathItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    Add-ProbeError 'path_commands' ('A PATH reparse-point directory was not searched: ' + $PathDirectory)
                    continue
                }
                foreach ($CommandFile in $CommandFiles.Keys) {
                    Inspect-Executable (Join-Path $PathItem.FullName $CommandFile) $CommandFiles[$CommandFile] 'path_commands'
                }
                $DotnetHost = Get-LocalItem (Join-Path $PathItem.FullName 'dotnet.exe') 'path_commands'
                if ($null -ne $DotnetHost -and -not $DotnetHost.PSIsContainer) {
                    Inspect-DotnetSdk $PathItem.FullName 'path_commands'
                }
            }
        } catch { Add-ProbeError 'path_commands' $_.Exception.Message }

        # Read explicit registry views; never execute UninstallString or a tool.
        $RegistryPatterns = [ordered]@{
            python = '(?i)\bPython(?:\s|\d|$)|\bAnaconda\b|\bMiniconda\b'
            node = '(?i)\bNode\.js\b|\bNVM for Windows\b'
            git = '(?i)^Git(?:\s|$)|\bGit for Windows\b'
            rust = '(?i)\bRust(?:\s|$)|\bRustup\b'
            visual_studio = '(?i)\bVisual Studio\b(?!.*\b(?:Runtime|Redistributable)\b)'
            msbuild = '(?i)\bMSBuild\b|\bMicrosoft Build Tools\b'
            java = '(?i)\bJava\b|\bJDK\b|\bOpenJDK\b|\bTemurin\b|\bCorretto\b|\bZulu\b'
            dotnet_sdk = '(?i)\.NET(?:\s+Core)?\s+SDK\b|\.NET.*\bSoftware Development Kit\b'
        }
        foreach ($HiveName in @('LocalMachine', 'CurrentUser')) {
            foreach ($ViewName in @('Registry64', 'Registry32')) {
                $BaseKey = $null
                $UninstallKey = $null
                try {
                    $Hive = [Microsoft.Win32.RegistryHive] [Enum]::Parse([Microsoft.Win32.RegistryHive], $HiveName)
                    $View = [Microsoft.Win32.RegistryView] [Enum]::Parse([Microsoft.Win32.RegistryView], $ViewName)
                    $BaseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, $View)
                    $UninstallKey = $BaseKey.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', $false)
                    if ($null -eq $UninstallKey) { continue }
                    if ($UninstallKey.SubKeyCount -gt 4096) { throw 'The uninstall registry exceeds the 4096-entry limit.' }
                    $SubkeyNames = @($UninstallKey.GetSubKeyNames())
                    if ($SubkeyNames.Count -gt 4096) { throw 'The uninstall registry exceeds the 4096-entry limit.' }
                    foreach ($SubkeyName in $SubkeyNames) {
                        $ProductKey = $null
                        try {
                            $ProductKey = $UninstallKey.OpenSubKey($SubkeyName, $false)
                            if ($null -eq $ProductKey) { throw 'An enumerated product key disappeared or could not be read.' }
                            $DisplayName = $ProductKey.GetValue('DisplayName', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                            if ($null -eq $DisplayName) { continue }
                            if ($DisplayName -isnot [string] -or $DisplayName.Length -gt 4096) { throw 'Invalid uninstall display name.' }
                            foreach ($ToolName in $RegistryPatterns.Keys) {
                                if ($DisplayName -match $RegistryPatterns[$ToolName]) {
                                    Add-Detection $ToolName 'uninstall_registry' ($HiveName + '/' + $ViewName +
                                        '/SOFTWARE/Microsoft/Windows/CurrentVersion/Uninstall/' + $SubkeyName)
                                }
                            }
                        } catch {
                            Add-ProbeError 'uninstall_registry' ($HiveName + '/' + $ViewName + '/' + $SubkeyName + ': ' + $_.Exception.Message)
                        } finally { if ($null -ne $ProductKey) { $ProductKey.Dispose() } }
                    }
                } catch {
                    Add-ProbeError 'uninstall_registry' ($HiveName + '/' + $ViewName + ': ' + $_.Exception.Message)
                } finally {
                    if ($null -ne $UninstallKey) { $UninstallKey.Dispose() }
                    if ($null -ne $BaseKey) { $BaseKey.Dispose() }
                }
            }
        }

        try {
            if (@($ProgramRoots).Count -eq 0 -or [string]::IsNullOrWhiteSpace($LocalApplicationData) -or
                [string]::IsNullOrWhiteSpace($RoamingApplicationData) -or [string]::IsNullOrWhiteSpace($UserProfileRoot)) {
                throw 'The conventional machine/user install roots are unavailable.'
            }
            foreach ($ProgramRoot in $ProgramRoots) {
                Inspect-Executable (Join-Path $ProgramRoot 'nodejs\node.exe') 'node' 'known_locations'
                Inspect-Executable (Join-Path $ProgramRoot 'nodejs\npm.cmd') 'npm' 'known_locations'
                Inspect-Executable (Join-Path $ProgramRoot 'Git\cmd\git.exe') 'git' 'known_locations'
                Inspect-Executable (Join-Path $ProgramRoot 'Microsoft VS Code\Code.exe') 'visual_studio' 'known_locations'
                Inspect-DotnetSdk (Join-Path $ProgramRoot 'dotnet') 'known_locations'
                foreach ($PythonRoot in @(Get-ChildDirectories $ProgramRoot 'known_locations' 'Python*')) {
                    Inspect-Executable (Join-Path $PythonRoot 'python.exe') 'python' 'known_locations'
                }
                foreach ($JavaFamily in @('Java', 'Eclipse Adoptium', 'Amazon Corretto', 'Zulu')) {
                    foreach ($JavaRoot in @(Get-ChildDirectories (Join-Path $ProgramRoot $JavaFamily) 'known_locations')) {
                        Inspect-Executable (Join-Path $JavaRoot 'bin\java.exe') 'java' 'known_locations'
                    }
                }
                foreach ($JavaRoot in @(Get-ChildDirectories (Join-Path $ProgramRoot 'Microsoft') 'known_locations' 'jdk*')) {
                    Inspect-Executable (Join-Path $JavaRoot 'bin\javac.exe') 'java' 'known_locations'
                }
                foreach ($StudioYear in @(Get-ChildDirectories (Join-Path $ProgramRoot 'Microsoft Visual Studio') 'known_locations')) {
                    foreach ($StudioEdition in @(Get-ChildDirectories $StudioYear 'known_locations')) {
                        Inspect-Executable (Join-Path $StudioEdition 'Common7\IDE\devenv.exe') 'visual_studio' 'known_locations'
                        Inspect-Executable (Join-Path $StudioEdition 'MSBuild\Current\Bin\MSBuild.exe') 'msbuild' 'known_locations'
                    }
                }
                foreach ($BuildRoot in @(Get-ChildDirectories (Join-Path $ProgramRoot 'MSBuild') 'known_locations')) {
                    Inspect-Executable (Join-Path $BuildRoot 'Bin\MSBuild.exe') 'msbuild' 'known_locations'
                }
            }
            foreach ($PythonRoot in @(Get-ChildDirectories (Join-Path $LocalApplicationData 'Programs\Python') 'known_locations')) {
                Inspect-Executable (Join-Path $PythonRoot 'python.exe') 'python' 'known_locations'
            }
            Inspect-Executable (Join-Path $LocalApplicationData 'Programs\Git\cmd\git.exe') 'git' 'known_locations'
            Inspect-Executable (Join-Path $LocalApplicationData 'Programs\Microsoft VS Code\Code.exe') 'visual_studio' 'known_locations'
            Inspect-Executable (Join-Path $RoamingApplicationData 'npm\npm.cmd') 'npm' 'known_locations'
            foreach ($RustCommand in @('rustc.exe', 'cargo.exe', 'rustup.exe')) {
                Inspect-Executable (Join-Path $UserProfileRoot ('.cargo\bin\' + $RustCommand)) 'rust' 'known_locations'
            }
            foreach ($CondaRoot in @('anaconda3', 'miniconda3')) {
                Inspect-Executable (Join-Path $UserProfileRoot ($CondaRoot + '\python.exe')) 'python' 'known_locations'
            }
            foreach ($PythonCommand in @('python.exe', 'python3.exe', 'pythonw.exe', 'py.exe')) {
                Inspect-Executable (Join-Path $LocalApplicationData ('Microsoft\WindowsApps\' + $PythonCommand)) 'python' 'known_locations'
            }
            Inspect-DotnetSdk (Join-Path $UserProfileRoot '.dotnet') 'known_locations'
            foreach ($DotnetVariable in @('DOTNET_ROOT', 'DOTNET_ROOT(x86)')) {
                $AdditionalDotnetRoot = [Environment]::GetEnvironmentVariable($DotnetVariable)
                if (-not [string]::IsNullOrWhiteSpace($AdditionalDotnetRoot)) {
                    Inspect-DotnetSdk $AdditionalDotnetRoot 'known_locations'
                }
            }
        } catch { Add-ProbeError 'known_locations' $_.Exception.Message }

        if (@($ProbeStates.Values | Where-Object { $_ -ne 'PASS' }).Count -eq 0) {
            $Result.status = 'COMPLETE'
            $Result.tool_inventory.status = 'COMPLETE'
        }
    }

    $Result.tool_inventory.probes = @($ProbeNames | ForEach-Object {
        [ordered]@{ name = $_; status = $ProbeStates[$_] }
    })
    $Result.tool_inventory.detections = @($Detections.ToArray())
    $Result.tool_inventory.app_execution_aliases = @($Aliases.ToArray())
    $Result.errors = @($ObservedErrors.ToArray())
    # COMPLETE describes completed bounded probes, never a clean-machine verdict.
    return $Result
}
