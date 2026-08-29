$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location $PSScriptRoot

$LogPath = Join-Path $PSScriptRoot 'build.log'
$ErrorLogPath = Join-Path $PSScriptRoot 'build_error.log'
Remove-Item $LogPath -Force -ErrorAction SilentlyContinue
Remove-Item $ErrorLogPath -Force -ErrorAction SilentlyContinue

function Log([string]$Message) {
    $stamp = Get-Date -Format 'HH:mm:ss'
    "[$stamp] $Message" | Tee-Object -FilePath $LogPath -Append
}

function Invoke-LoggedNative {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(Mandatory=$false)][string[]]$ArgumentList = @(),
        [Parameter(Mandatory=$false)][string]$FailureMessage = 'Native command failed.'
    )

    # Windows PowerShell 5.x turns *any* native stderr output into a
    # NativeCommandError when ErrorActionPreference=Stop, even if the process
    # exits with code 0. pip/PyInstaller legitimately write warnings/progress
    # to stderr, so native-process success must be decided by ExitCode only.
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $FilePath @ArgumentList 2>&1 | Tee-Object -FilePath $LogPath -Append
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }

    if ($null -eq $exitCode) { $exitCode = 0 }
    if ($exitCode -ne 0) {
        throw "$FailureMessage Exit code: $exitCode"
    }
    return $exitCode
}

try {
    $Work = Join-Path $PSScriptRoot '.build'
    $PythonDir = Join-Path $Work 'python'
    $PythonExe = Join-Path $PythonDir 'python.exe'
    $Dist = Join-Path $PSScriptRoot 'dist'
    $BuildDir = Join-Path $PSScriptRoot 'pyinstaller-build'
    $Bin = Join-Path $PSScriptRoot 'bin'
    New-Item -ItemType Directory -Force -Path $Work, $Bin | Out-Null

    $PyVersion = '3.12.10'
    $PythonZip = Join-Path $Work "python-$PyVersion-embed-amd64.zip"

    if (!(Test-Path $PythonExe)) {
        Log '1/7 Downloading Python 3.12 x64 embeddable runtime...'
        if (!(Test-Path $PythonZip)) {
            Invoke-WebRequest -UseBasicParsing "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip" -OutFile $PythonZip
        }

        Log '2/7 Extracting private Python build environment...'
        Remove-Item $PythonDir -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $PythonDir | Out-Null
        Expand-Archive -Path $PythonZip -DestinationPath $PythonDir -Force
        if (!(Test-Path $PythonExe)) {
            throw "Embedded Python archive extracted, but python.exe was not found at: $PythonExe"
        }

        # Python's embeddable distribution ships in isolated mode. Enable 'site'
        # so pip-installed packages under Lib\site-packages are importable.
        $Pth = Get-ChildItem $PythonDir -Filter 'python*._pth' | Select-Object -First 1
        if (!$Pth) { throw 'Python embeddable runtime is missing python*._pth.' }
        $PthText = Get-Content $Pth.FullName -Raw
        if ($PthText -match '(?m)^#import site\s*$') {
            $PthText = $PthText -replace '(?m)^#import site\s*$', 'import site'
            [System.IO.File]::WriteAllText($Pth.FullName, $PthText, [System.Text.Encoding]::ASCII)
        } elseif ($PthText -notmatch '(?m)^import site\s*$') {
            Add-Content -Encoding ASCII -Path $Pth.FullName -Value "`r`nimport site"
        }

        # Bootstrap pip without invoking any Windows installer. This avoids all
        # TargetDir/quoting problems when the build kit path contains spaces.
        $GetPip = Join-Path $Work 'get-pip.py'
        if (!(Test-Path $GetPip)) {
            Invoke-WebRequest -UseBasicParsing 'https://bootstrap.pypa.io/get-pip.py' -OutFile $GetPip
        }
        Invoke-LoggedNative -FilePath $PythonExe -ArgumentList @($GetPip, '--disable-pip-version-check') -FailureMessage 'pip bootstrap failed.' | Out-Null
        Invoke-LoggedNative -FilePath $PythonExe -ArgumentList @('-m','pip','--version') -FailureMessage 'pip was bootstrapped but cannot be invoked.' | Out-Null
        Log "Embedded Python ready at: $PythonExe"
    } else {
        Log '1/7 Private embedded Python already exists; reusing it.'
        Log '2/7 Python extraction step skipped.'
    }

    Log '3/7 Installing PySide6 and PyInstaller...'
    Invoke-LoggedNative -FilePath $PythonExe -ArgumentList @('-m','pip','install','--disable-pip-version-check','--upgrade','pip') -FailureMessage 'pip upgrade failed.' | Out-Null
    Invoke-LoggedNative -FilePath $PythonExe -ArgumentList @('-m','pip','install','--disable-pip-version-check','PySide6>=6.8,<7','pyinstaller>=6.10,<7') -FailureMessage 'Python dependency installation failed.' | Out-Null
    Invoke-LoggedNative -FilePath $PythonExe -ArgumentList @('-c',"import PySide6, PyInstaller; print('PYTHON_DEPS_OK', PySide6.__version__, PyInstaller.__version__)") -FailureMessage 'Installed Python dependencies cannot be imported.' | Out-Null

    $Ffmpeg = Join-Path $Bin 'ffmpeg.exe'
    $Ffprobe = Join-Path $Bin 'ffprobe.exe'
    if (!(Test-Path $Ffmpeg) -or !(Test-Path $Ffprobe)) {
        Log '4/7 Downloading FFmpeg and FFprobe...'
        $Zip = Join-Path $Work 'ffmpeg-release-essentials.zip'
        Invoke-WebRequest -UseBasicParsing 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $Zip
        $FFDir = Join-Path $Work 'ffmpeg-unpack'
        Remove-Item $FFDir -Recurse -Force -ErrorAction SilentlyContinue
        Expand-Archive -Path $Zip -DestinationPath $FFDir -Force
        $FoundFfmpeg = Get-ChildItem $FFDir -Filter ffmpeg.exe -Recurse | Select-Object -First 1
        $FoundFfprobe = Get-ChildItem $FFDir -Filter ffprobe.exe -Recurse | Select-Object -First 1
        if (!$FoundFfmpeg -or !$FoundFfprobe) { throw 'FFmpeg archive did not contain ffmpeg.exe and ffprobe.exe.' }
        Copy-Item $FoundFfmpeg.FullName $Ffmpeg -Force
        Copy-Item $FoundFfprobe.FullName $Ffprobe -Force
    } else {
        Log '4/7 FFmpeg already exists; reusing it.'
    }

    Log '5/7 Running source import smoke test...'
    # The embeddable Python distribution uses python*._pth, so the current
    # working directory is intentionally NOT added to sys.path. Explicitly
    # insert the project root for source imports instead of relying on cwd.
    $ProjectRootPy = $PSScriptRoot.Replace('\','\\').Replace("'","\'")
    $SmokeCode = "import sys; sys.path.insert(0, r'$ProjectRootPy'); from PySide6.QtWidgets import QApplication; import app.main, app.ui, app.media, app.model; print('IMPORT_OK')"
    Invoke-LoggedNative -FilePath $PythonExe -ArgumentList @('-c',$SmokeCode) -FailureMessage 'Source import smoke test failed.' | Out-Null

    Log '6/7 Building native Windows EXE with PyInstaller...'
    Remove-Item $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $Dist -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $PSScriptRoot 'LosslessVideoSlicer.spec') -Force -ErrorAction SilentlyContinue

    $Sep = ';'
    $PyInstallerArgs = @(
        '-m','PyInstaller',
        '--noconfirm','--clean','--windowed','--onedir',
        '--name','LosslessVideoSlicer',
        '--hidden-import','PySide6.QtMultimedia',
        '--hidden-import','PySide6.QtMultimediaWidgets',
        '--collect-submodules','PySide6.QtMultimedia',
        '--paths',$PSScriptRoot,
        '--add-data',("assets${Sep}assets"),
        '--add-binary',("bin\ffmpeg.exe${Sep}bin"),
        '--add-binary',("bin\ffprobe.exe${Sep}bin"),
        '--distpath',$Dist,
        '--workpath',$BuildDir,
        (Join-Path $PSScriptRoot 'entry.py')
    )
    Invoke-LoggedNative -FilePath $PythonExe -ArgumentList $PyInstallerArgs -FailureMessage 'PyInstaller build failed.' | Out-Null

    $Exe = Join-Path $Dist 'LosslessVideoSlicer\LosslessVideoSlicer.exe'
    if (!(Test-Path $Exe)) { throw 'PyInstaller completed but the final EXE was not found.' }

    $FinalFfmpeg = Join-Path $Dist 'LosslessVideoSlicer\_internal\bin\ffmpeg.exe'
    $FinalFfprobe = Join-Path $Dist 'LosslessVideoSlicer\_internal\bin\ffprobe.exe'
    if (!(Test-Path $FinalFfmpeg) -or !(Test-Path $FinalFfprobe)) {
        throw 'Final package is missing FFmpeg or FFprobe.'
    }

    Log '7/7 Creating diagnostic launcher and final checks...'
    $Diagnostic = @'
@echo off
setlocal
cd /d "%~dp0"
set QT_DEBUG_PLUGINS=1
set QT_LOGGING_RULES=qt.multimedia.*=true
"LosslessVideoSlicer.exe" > startup_diagnostic.log 2>&1
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%
echo If the app did not open, send startup_diagnostic.log to ChatGPT.
pause
exit /b %RC%
'@
    [System.IO.File]::WriteAllText((Join-Path $Dist 'LosslessVideoSlicer\diagnose_startup.cmd'), $Diagnostic, [System.Text.Encoding]::ASCII)

    $Readme = @'
BUILD SUCCEEDED

Run:
  LosslessVideoSlicer.exe

This is a native Windows x64 PyInstaller onedir build.
Python, PySide6, FFmpeg, and FFprobe are bundled inside the output folder.
Do not move only the EXE out of this folder.

If the EXE does not start, run diagnose_startup.cmd and send startup_diagnostic.log to ChatGPT.
'@
    [System.IO.File]::WriteAllText((Join-Path $Dist 'LosslessVideoSlicer\README.txt'), $Readme, [System.Text.Encoding]::ASCII)

    Log "BUILD_SUCCESS: $Exe"
    Write-Host ''
    Write-Host 'BUILD SUCCEEDED' -ForegroundColor Green
    Write-Host $Exe -ForegroundColor Green
    exit 0
}
catch {
    $Text = ($_ | Out-String)
    $Text | Set-Content -Encoding UTF8 $ErrorLogPath
    try { "`n--- build.log tail ---`n" + ((Get-Content $LogPath -Tail 80) -join "`n") | Add-Content -Encoding UTF8 $ErrorLogPath } catch {}
    Write-Host ''
    Write-Host 'BUILD FAILED' -ForegroundColor Red
    Write-Host $Text -ForegroundColor Red
    Write-Host "See: $ErrorLogPath" -ForegroundColor Yellow
    exit 1
}
