#Requires -Version 5.1
<#
.SYNOPSIS
  Build JonathanSalwan/Triton for Windows: static lib (e.g. IDA plugins) + shared Python module.

.DESCRIPTION
  Drop this script into a Triton source checkout (or pass -RepoRoot) and run it.
  It configures/builds/installs:
    - static  -> <InstallRoot>/triton-static  (triton.lib for C++ consumers like Ponce)
    - shared  -> <InstallRoot>/triton         (DLL) + copies triton.pyd into Python site-packages

  Pre-flight checks fail fast with actionable errors if cmake / MSVC / vcpkg / LLVM / Python
  are missing. Paths can be overridden via parameters or environment variables:
    TRITON_LLVM_PREFIX, TRITON_PYTHON_ROOT, VCPKG_ROOT, CMAKE_EXE

.EXAMPLE
  .\build.ps1
  .\build.ps1 -SyncUpstream
  .\build.ps1 -StaticOnly -Clean
  .\build.ps1 -SharedOnly -PythonRoot D:\python\3.13
  .\build.ps1 -RepoRoot C:\src\Triton -InstallRoot C:\opt\triton-dist

.NOTES
  Requires: Windows, Visual Studio with MSVC (detected via vswhere), CMake 3.15+, vcpkg, LLVM,
  CPython with libs/headers. VS generator / cmake / VS-integrated vcpkg are resolved with vswhere
  unless you override -VsGenerator / -CMake / -VcpkgToolchain.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$InstallRoot = '',

    [switch]$SyncUpstream,
    [switch]$StaticOnly,
    [switch]$SharedOnly,
    [switch]$Clean,
    [switch]$SkipPythonImportCheck,

    [int]$Jobs = 0,
    [ValidateSet('Release', 'RelWithDebInfo', 'Debug')]
    [string]$Config = 'Release',

    [string]$LlvmPrefix = $env:TRITON_LLVM_PREFIX,
    [string]$PythonRoot = $env:TRITON_PYTHON_ROOT,
    [string]$VcpkgRoot = $env:VCPKG_ROOT,
    [string]$VcpkgToolchain = '',
    [string]$CMake = $env:CMAKE_EXE,
    [string]$Triplet = 'x64-windows-static-md-release',
    # Empty = auto-detect from vswhere (e.g. "Visual Studio 17 2022")
    [string]$VsGenerator = '',

    [string]$UpstreamRemote = 'upstream',
    [string]$UpstreamUrl = 'https://github.com/JonathanSalwan/Triton.git',
    [string]$UpstreamBranch = 'dev-v1.0'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "  [ok] $Message" -ForegroundColor Green
}

function Write-Fail([string]$Message) {
    Write-Host "  [FAIL] $Message" -ForegroundColor Red
}

function Fail([string]$Message, [string[]]$Hints = @()) {
    Write-Fail $Message
    foreach ($h in $Hints) {
        Write-Host "         -> $h" -ForegroundColor Yellow
    }
    throw $Message
}

function Test-Cmd([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Resolve-Existing([string[]]$Candidates, [string]$Label) {
    foreach ($c in $Candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) {
            return (Resolve-Path -LiteralPath $c).Path
        }
    }
    return $null
}

function Get-PythonTag([string]$PythonExe) {
    $code = @'
import sys
print(f"cp{sys.version_info[0]}{sys.version_info[1]}-win_amd64")
'@
    $tag = & $PythonExe -c $code
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($tag)) {
        Fail "Could not determine Python extension tag from $PythonExe"
    }
    return $tag.Trim()
}

function Get-PythonLibPath([string]$PythonRootDir, [string]$PythonExe) {
    $verCode = @'
import sys
print(f"{sys.version_info[0]}{sys.version_info[1]}")
'@
    $ver = (& $PythonExe -c $verCode).Trim()
    $candidates = @(
        (Join-Path $PythonRootDir "libs/python$ver.lib")
        (Join-Path $PythonRootDir "lib/python$ver.lib")
    )
    $hit = Resolve-Existing $candidates 'Python import library'
    if (-not $hit) {
        Fail "Python import library not found for version tag '$ver'." @(
            "Expected something like: $(Join-Path $PythonRootDir "libs/python$ver.lib")"
            "Install the CPython Windows distribution that includes libs/ and include/ (not the Store stub)."
            "Override with -PythonRoot <path-to-python-prefix>"
        )
    }
    return $hit
}

function Get-VsWhere {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe')
        (Join-Path $env:ProgramFiles 'Microsoft Visual Studio/Installer/vswhere.exe')
    )
    return Resolve-Existing $candidates 'vswhere'
}

function Find-VisualStudio {
    $vswhere = Get-VsWhere
    if (-not $vswhere) { return $null }

    # Prefer an install with MSVC toolset; fall back to any latest VS.
    $json = & $vswhere -latest -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -format json 2>$null
    if (-not $json) {
        $json = & $vswhere -latest -products * -format json 2>$null
    }
    if (-not $json) { return $null }

    $info = $json | ConvertFrom-Json | Select-Object -First 1
    if (-not $info) { return $null }

    $installPath = [string]$info.installationPath
    $version = [string]$info.installationVersion
    $productLine = [string]$info.catalog.productLineVersion
    $major = 0
    if ($version -match '^(\d+)\.') { $major = [int]$Matches[1] }

    # CMake VS generator names track the toolset major (16/17/18...), not the marketing year alone.
    $generatorMap = @{
        16 = 'Visual Studio 16 2019'
        17 = 'Visual Studio 17 2022'
        18 = 'Visual Studio 18 2025'
    }
    $generator = $null
    if ($generatorMap.ContainsKey($major)) {
        $generator = $generatorMap[$major]
    }
    elseif ($productLine -and $major -gt 0) {
        $generator = "Visual Studio $major $productLine"
    }

    return [pscustomobject]@{
        VsWhere       = $vswhere
        InstallPath   = $installPath
        Version       = $version
        ProductLine   = $productLine
        Major         = $major
        Generator     = $generator
        DisplayName   = [string]$info.displayName
    }
}

function Find-CMake([object]$Vs) {
    if ($CMake -and (Test-Path -LiteralPath $CMake)) {
        return (Resolve-Path -LiteralPath $CMake).Path
    }
    if (Test-Cmd 'cmake') {
        return (Get-Command cmake).Source
    }

    $vswhere = if ($Vs) { $Vs.VsWhere } else { Get-VsWhere }
    if ($vswhere) {
        $found = & $vswhere -latest -products * `
            -requires Microsoft.VisualStudio.Component.VC.CMake.Project `
            -find '**/CMake/CMake/bin/cmake.exe' 2>$null |
            Select-Object -First 1
        if (-not $found) {
            $found = & $vswhere -latest -products * -find '**/cmake.exe' 2>$null |
                Select-Object -First 1
        }
        if ($found -and (Test-Path $found)) { return $found }
    }

    if ($Vs -and $Vs.InstallPath) {
        $underVs = Join-Path $Vs.InstallPath 'Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe'
        if (Test-Path $underVs) { return (Resolve-Path $underVs).Path }
    }

    return $null
}

function Find-VcpkgToolchain([object]$Vs) {
    if ($VcpkgToolchain -and (Test-Path -LiteralPath $VcpkgToolchain)) {
        return (Resolve-Path -LiteralPath $VcpkgToolchain).Path
    }
    if ($VcpkgRoot) {
        $t = Join-Path $VcpkgRoot 'scripts/buildsystems/vcpkg.cmake'
        if (Test-Path $t) { return (Resolve-Path $t).Path }
    }
    if ($env:VCPKG_INSTALLATION_ROOT) {
        $t = Join-Path $env:VCPKG_INSTALLATION_ROOT 'scripts/buildsystems/vcpkg.cmake'
        if (Test-Path $t) { return (Resolve-Path $t).Path }
    }

    if ($Vs -and $Vs.InstallPath) {
        $t = Join-Path $Vs.InstallPath 'VC/vcpkg/scripts/buildsystems/vcpkg.cmake'
        if (Test-Path $t) { return (Resolve-Path $t).Path }
    }

    $vswhere = if ($Vs) { $Vs.VsWhere } else { Get-VsWhere }
    if ($vswhere) {
        $found = & $vswhere -latest -products * -find '**/vcpkg/scripts/buildsystems/vcpkg.cmake' 2>$null |
            Select-Object -First 1
        if ($found -and (Test-Path $found)) { return $found }
    }

    return $null
}

function Find-LlvmPrefix {
    if ($LlvmPrefix -and (Test-Path -LiteralPath $LlvmPrefix)) {
        return (Resolve-Path -LiteralPath $LlvmPrefix).Path
    }
    if ($env:LLVM_DIR) {
        # LLVM_DIR is often <prefix>/lib/cmake/llvm
        $p = $env:LLVM_DIR.TrimEnd('\', '/')
        if ($p -match '[\\/]lib[\\/]cmake[\\/]llvm$') {
            $prefix = Split-Path (Split-Path (Split-Path $p) -Parent) -Parent
            if (Test-Path $prefix) { return (Resolve-Path $prefix).Path }
        }
        elseif (Test-Path (Join-Path $p 'lib/cmake/llvm/LLVMConfig.cmake')) {
            return (Resolve-Path $p).Path
        }
    }
    return $null
}

function Find-PythonRoot {
    if ($PythonRoot -and (Test-Path -LiteralPath $PythonRoot)) {
        return (Resolve-Path -LiteralPath $PythonRoot).Path
    }
    if ($env:TRITON_PYTHON_ROOT -and (Test-Path $env:TRITON_PYTHON_ROOT)) {
        return (Resolve-Path $env:TRITON_PYTHON_ROOT).Path
    }
    # Prefer python on PATH if it looks like a full install (has include + libs)
    if (Test-Cmd 'python') {
        $exe = (Get-Command python).Source
        $prefixCode = "import sys; print(sys.base_prefix)"
        $prefix = (& $exe -c $prefixCode 2>$null)
        if ($LASTEXITCODE -eq 0 -and $prefix) {
            $prefix = $prefix.Trim()
            $inc = Join-Path $prefix 'include'
            $libs = Join-Path $prefix 'libs'
            if ((Test-Path $inc) -and (Test-Path $libs)) {
                return (Resolve-Path $prefix).Path
            }
        }
    }
    return $null
}

function Ensure-GitRepo([string]$Path) {
    if (-not (Test-Path (Join-Path $Path '.git'))) {
        Fail "Not a git repository: $Path" @(
            "Clone Triton first: git clone https://github.com/JonathanSalwan/Triton.git"
            "Or pass -RepoRoot <path-to-triton-checkout>"
        )
    }
}

function Ensure-TritonSources([string]$Path) {
    $marker = Join-Path $Path 'src/libtriton/CMakeLists.txt'
    if (-not (Test-Path $marker)) {
        Fail "Does not look like a Triton source tree: $Path" @(
            "Expected file: src/libtriton/CMakeLists.txt"
            "Run this script from the Triton repo root, or pass -RepoRoot"
        )
    }
}

function Ensure-TripletOverlay([string]$Repo, [string]$TripletName) {
    $dir = Join-Path $Repo 'vcpkg/triplets'
    $dst = Join-Path $dir "$TripletName.cmake"
    if (Test-Path $dst) { return $dir }

    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    $toolchain = Find-VcpkgToolchain
    $vcpkgHome = Split-Path (Split-Path (Split-Path $toolchain -Parent) -Parent) -Parent
    $community = Join-Path $vcpkgHome "triplets/community/$TripletName.cmake"
    $official = Join-Path $vcpkgHome "triplets/$TripletName.cmake"

    if (Test-Path $community) {
        Copy-Item -Force $community $dst
        Write-Ok "Created overlay triplet from vcpkg community: $dst"
        return $dir
    }
    if (Test-Path $official) {
        Copy-Item -Force $official $dst
        Write-Ok "Created overlay triplet from vcpkg: $dst"
        return $dir
    }

    if ($TripletName -eq 'x64-windows-static-md-release') {
        @"
set(VCPKG_TARGET_ARCHITECTURE x64)
set(VCPKG_CRT_LINKAGE dynamic)
set(VCPKG_LIBRARY_LINKAGE static)
set(VCPKG_BUILD_TYPE release)
"@ | Set-Content -Path $dst -Encoding ASCII
        Write-Ok "Wrote default overlay triplet: $dst"
        return $dir
    }

    Fail "vcpkg triplet '$TripletName' not found and no default generator available." @(
        "Install/create the triplet, or pass -Triplet x64-windows-static-md-release"
        "Looked in: $community"
    )
}

function Invoke-Preflight {
    Write-Step 'Pre-flight checks'

    if ($StaticOnly -and $SharedOnly) {
        Fail "Use only one of -StaticOnly or -SharedOnly (or neither to build both)."
    }

    if ($Jobs -le 0) {
        $script:Jobs = [Math]::Max(1, [int]$env:NUMBER_OF_PROCESSORS)
    }

    Ensure-TritonSources $script:Root
    if ($SyncUpstream) { Ensure-GitRepo $script:Root }

    $script:Vs = Find-VisualStudio
    if ($script:Vs) {
        Write-Ok ("VS: {0} ({1}) @ {2}" -f $script:Vs.DisplayName, $script:Vs.Version, $script:Vs.InstallPath)
    }
    else {
        Write-Host '  [warn] vswhere did not find Visual Studio with MSVC; configure may fail.' -ForegroundColor Yellow
    }

    if (-not $VsGenerator) {
        if ($script:Vs -and $script:Vs.Generator) {
            $script:VsGenerator = $script:Vs.Generator
        }
        else {
            Fail 'Could not auto-detect a CMake Visual Studio generator.' @(
                'Install Visual Studio with the Desktop development with C++ workload.'
                'Or pass -VsGenerator "Visual Studio 17 2022" (adjust for your VS version).'
            )
        }
    }
    else {
        $script:VsGenerator = $VsGenerator
    }
    Write-Ok "generator: $script:VsGenerator"

    $script:CMake = Find-CMake $script:Vs
    if (-not $script:CMake) {
        Fail 'CMake not found.' @(
            'Install CMake, or Visual Studio with the CMake component.'
            'Or set CMAKE_EXE / pass -CMake <path-to-cmake.exe>'
        )
    }
    Write-Ok "cmake: $script:CMake"

    $script:VcpkgToolchain = Find-VcpkgToolchain $script:Vs
    if (-not $script:VcpkgToolchain) {
        Fail 'vcpkg toolchain file not found.' @(
            'Install vcpkg (or VS-integrated vcpkg).'
            'Set VCPKG_ROOT or VCPKG_INSTALLATION_ROOT, or pass -VcpkgToolchain <path-to-vcpkg.cmake>'
        )
    }
    Write-Ok "vcpkg: $script:VcpkgToolchain"

    $script:LlvmPrefix = Find-LlvmPrefix
    if (-not $script:LlvmPrefix) {
        Fail 'LLVM prefix not found (needed for -DLLVM_INTERFACE=ON).' @(
            'Build/install LLVM and pass -LlvmPrefix <prefix> (directory containing lib/cmake/llvm).'
            'Or set TRITON_LLVM_PREFIX / LLVM_DIR.'
        )
    }
    $llvmConfig = Join-Path $script:LlvmPrefix 'lib/cmake/llvm/LLVMConfig.cmake'
    if (-not (Test-Path $llvmConfig)) {
        Fail "LLVMConfig.cmake missing under LLVM prefix: $llvmConfig" @(
            'Pass -LlvmPrefix to the LLVM install prefix (not the source tree).'
        )
    }
    Write-Ok "LLVM: $script:LlvmPrefix"

    $script:PythonRoot = Find-PythonRoot
    if (-not $script:PythonRoot) {
        Fail 'Python root not found.' @(
            'Pass -PythonRoot <prefix> where include/ and libs/ exist.'
            'Or set TRITON_PYTHON_ROOT.'
        )
    }
    $script:PythonExe = Join-Path $script:PythonRoot 'python.exe'
    if (-not (Test-Path $script:PythonExe)) {
        Fail "python.exe not found at $script:PythonExe"
    }
    $script:PythonInclude = Join-Path $script:PythonRoot 'include'
    if (-not (Test-Path (Join-Path $script:PythonInclude 'Python.h'))) {
        Fail "Python.h not found under $script:PythonInclude" @(
            'Use a full CPython install with development headers.'
        )
    }
    $script:PythonLib = Get-PythonLibPath $script:PythonRoot $script:PythonExe
    $script:SitePackages = Join-Path $script:PythonRoot 'Lib/site-packages'
    if (-not (Test-Path $script:SitePackages)) {
        New-Item -ItemType Directory -Force -Path $script:SitePackages | Out-Null
    }
    $script:PythonTag = Get-PythonTag $script:PythonExe
    Write-Ok "Python: $script:PythonExe (tag=$script:PythonTag)"
    Write-Ok "Python lib: $script:PythonLib"

    if (-not (Test-Cmd 'git') -and $SyncUpstream) {
        Fail 'git not found on PATH (required for -SyncUpstream).'
    }

    $script:OverlayTriplets = Ensure-TripletOverlay $script:Root $Triplet
    Write-Ok "triplet overlay: $script:OverlayTriplets ($Triplet)"
    Write-Ok "jobs: $Jobs  config: $Config"
}

function Sync-Upstream {
    Write-Step "Sync upstream $UpstreamUrl ($UpstreamBranch)"
    Push-Location $script:Root
    try {
        $remotes = @(git remote)
        if ($remotes -notcontains $UpstreamRemote) {
            git remote add $UpstreamRemote $UpstreamUrl
            if ($LASTEXITCODE -ne 0) { Fail "git remote add $UpstreamRemote failed" }
            Write-Ok "added remote $UpstreamRemote"
        }
        git fetch $UpstreamRemote $UpstreamBranch
        if ($LASTEXITCODE -ne 0) { Fail "git fetch $UpstreamRemote $UpstreamBranch failed" }

        git merge "$UpstreamRemote/$UpstreamBranch" --no-edit
        if ($LASTEXITCODE -ne 0) {
            Fail "Merge conflict merging $UpstreamRemote/$UpstreamBranch" @(
                'Resolve conflicts, commit, then re-run without -SyncUpstream (or after fixing).'
                'To abort: git merge --abort'
            )
        }
        Write-Ok "merged $UpstreamRemote/$UpstreamBranch"
    }
    finally {
        Pop-Location
    }
}

function Invoke-Configure([string]$BuildDir, [bool]$Shared, [string]$Prefix) {
    Write-Step "Configure $(if ($Shared) { 'shared' } else { 'static' }) -> $BuildDir"
    $cmakeArgs = @(
        '-S', $script:Root
        '-B', $BuildDir
        '-G', $script:VsGenerator
        '-A', 'x64'
        "-DBUILD_SHARED_LIBS:BOOL=$(if ($Shared) { 'ON' } else { 'OFF' })"
        '-DMSVC_STATIC:BOOL=OFF'
        '-DPYTHON_BINDINGS:BOOL=ON'
        '-DPYTHON_BINDINGS_AUTOCOMPLETE:BOOL=ON'
        "-DPYTHON_EXECUTABLE=$($script:PythonExe)"
        "-DPYTHON_INCLUDE_DIRS=$($script:PythonInclude)"
        "-DPYTHON_LIBRARIES=$($script:PythonLib)"
        "-DPYTHON_SITE_PACKAGES=$($script:SitePackages)"
        '-DLLVM_INTERFACE:BOOL=ON'
        '-DZ3_INTERFACE:BOOL=ON'
        "-DVCPKG_TARGET_TRIPLET=$Triplet"
        "-DVCPKG_OVERLAY_TRIPLETS=$($script:OverlayTriplets)"
        "-DCMAKE_PREFIX_PATH=$($script:LlvmPrefix)"
        "-DCMAKE_TOOLCHAIN_FILE=$($script:VcpkgToolchain)"
        "-DCMAKE_INSTALL_PREFIX=$Prefix"
    )
    & $script:CMake @cmakeArgs
    if ($LASTEXITCODE -ne 0) {
        Fail "CMake configure failed ($BuildDir)" @(
            'Scroll up for the first CMake/vcpkg error.'
            'Common causes: wrong LLVM prefix, broken Python libs, missing vcpkg triplet, first-time vcpkg package build still running.'
        )
    }
}

function Invoke-Build([string]$BuildDir, [string[]]$Targets) {
    foreach ($t in $Targets) {
        Write-Step "Build $t ($BuildDir)"
        $args = @('--build', $BuildDir, '--config', $Config, '--parallel', "$Jobs", '--target', $t)
        if ($Clean -and $t -eq $Targets[0]) { $args += '--clean-first' }
        & $script:CMake @args
        if ($LASTEXITCODE -ne 0) {
            Fail "CMake build failed for target '$t'" @(
                "Build dir: $BuildDir"
                'Open the .sln in Visual Studio or re-run with -Jobs 1 for clearer logs.'
            )
        }
    }
}

function Invoke-Install([string]$BuildDir, [string]$Prefix) {
    Write-Step "Generate autocomplete stubs ($BuildDir)"
    & $script:CMake --build $BuildDir --config $Config --parallel $Jobs --target python_autocomplete
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [warn] python_autocomplete failed; install may fail if triton.pyi is required." -ForegroundColor Yellow
    }

    Write-Step "Install -> $Prefix"
    & $script:CMake --install $BuildDir --config $Config --prefix $Prefix
    if ($LASTEXITCODE -ne 0) {
        Fail "CMake install failed ($BuildDir -> $Prefix)" @(
            'If the error mentions triton.pyi, ensure python_autocomplete succeeded.'
            'Close programs locking output files and retry.'
        )
    }
}

function Install-PythonModule([string]$SharedBuildDir) {
    Write-Step 'Install Python module into site-packages'
    $releaseDir = Join-Path $SharedBuildDir 'src/libtriton/Release'
    # Multi-config VS generator uses Config subdir; single-config may not
    if (-not (Test-Path $releaseDir)) {
        $alt = Join-Path $SharedBuildDir "src/libtriton/$Config"
        if (Test-Path $alt) { $releaseDir = $alt }
    }

    $dll = Join-Path $releaseDir 'triton.dll'
    $pyd = Join-Path $releaseDir 'triton.pyd'
    if (-not (Test-Path $pyd)) {
        if (Test-Path $dll) {
            Copy-Item -Force $dll $pyd
            Write-Ok "created triton.pyd from triton.dll"
        }
        else {
            $found = Get-ChildItem $SharedBuildDir -Recurse -Filter 'triton.pyd' -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if (-not $found) {
                $foundDll = Get-ChildItem $SharedBuildDir -Recurse -Filter 'triton.dll' -ErrorAction SilentlyContinue |
                    Select-Object -First 1
                if ($foundDll) {
                    $pyd = Join-Path $foundDll.DirectoryName 'triton.pyd'
                    Copy-Item -Force $foundDll.FullName $pyd
                }
            }
            else {
                $pyd = $found.FullName
            }
        }
    }

    if (-not (Test-Path $pyd)) {
        Fail "triton.pyd not found after shared build." @(
            "Looked under: $releaseDir"
            'Ensure -DPYTHON_BINDINGS=ON and BUILD_SHARED_LIBS=ON (this script sets both for shared).'
        )
    }

    New-Item -ItemType Directory -Force -Path $script:SitePackages | Out-Null
    Copy-Item -Force $pyd (Join-Path $script:SitePackages 'triton.pyd')
    $tagged = Join-Path $script:SitePackages "triton.$($script:PythonTag).pyd"
    Copy-Item -Force $pyd $tagged
    Write-Ok "installed $tagged"

    $pyi = Get-ChildItem $SharedBuildDir -Recurse -Filter 'triton.pyi' -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    if ($pyi) {
        Copy-Item -Force $pyi (Join-Path $script:SitePackages 'triton.pyi')
        Write-Ok 'installed triton.pyi'
    }

    if ($SkipPythonImportCheck) {
        Write-Host '  [warn] skipping Python import check (-SkipPythonImportCheck)' -ForegroundColor Yellow
        return
    }

    Write-Step 'Verify import triton'
    & $script:PythonExe -c "import triton; print('file=', triton.__file__); print('VERSION=', triton.VERSION.MAJOR, triton.VERSION.MINOR, triton.VERSION.BUILD); triton.TritonContext(); print('TritonContext OK')"
    if ($LASTEXITCODE -ne 0) {
        Fail 'import triton failed after install.' @(
            "Check that $tagged is not locked by another process."
            'Remove older triton*.pyd / dist-info leftovers from site-packages if a stale module is preferred.'
            "site-packages: $($script:SitePackages)"
        )
    }
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

try {
    if (-not $RepoRoot) {
        if ($PSScriptRoot) { $RepoRoot = $PSScriptRoot }
        else { $RepoRoot = (Get-Location).Path }
    }
    $script:Root = (Resolve-Path -LiteralPath $RepoRoot).Path

    if (-not $InstallRoot) {
        $InstallRoot = Split-Path $script:Root -Parent
    }
    if (-not (Test-Path $InstallRoot)) {
        New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    }
    $InstallRoot = (Resolve-Path -LiteralPath $InstallRoot).Path

    $StaticBuildDir = Join-Path $script:Root 'build-static'
    $SharedBuildDir = Join-Path $script:Root 'build-shared'
    $StaticPrefix = Join-Path $InstallRoot 'triton-static'
    $SharedPrefix = Join-Path $InstallRoot 'triton'

    $doStatic = -not $SharedOnly
    $doShared = -not $StaticOnly

    Write-Host "Triton Windows build" -ForegroundColor White
    Write-Host "  repo:    $script:Root"
    Write-Host "  install: $InstallRoot"

    Invoke-Preflight

    if ($SyncUpstream) { Sync-Upstream }

    if ($doStatic) {
        Invoke-Configure -BuildDir $StaticBuildDir -Shared:$false -Prefix $StaticPrefix
        Invoke-Build -BuildDir $StaticBuildDir -Targets @('triton')
        Invoke-Install -BuildDir $StaticBuildDir -Prefix $StaticPrefix
        $lib = Join-Path $StaticPrefix 'lib/triton.lib'
        if (-not (Test-Path $lib)) { Fail "Static install missing $lib" }
        Write-Ok "static lib: $lib"
    }

    if ($doShared) {
        Invoke-Configure -BuildDir $SharedBuildDir -Shared:$true -Prefix $SharedPrefix
        Invoke-Build -BuildDir $SharedBuildDir -Targets @('triton', 'python-triton')
        Invoke-Install -BuildDir $SharedBuildDir -Prefix $SharedPrefix
        Install-PythonModule -SharedBuildDir $SharedBuildDir
    }

    Write-Host "`nDone." -ForegroundColor Green
    if ($doStatic) { Write-Host "  static: $StaticPrefix\lib\triton.lib" }
    if ($doShared) { Write-Host "  python: $($script:SitePackages)\triton.$($script:PythonTag).pyd" }
    exit 0
}
catch {
    Write-Host "`nBuild failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
