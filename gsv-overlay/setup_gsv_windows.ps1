param(
    [string]$GsvDir = "",
    [string]$PythonExe = "",
    [string]$CudaIndexUrl = "https://download.pytorch.org/whl/cu128",
    [string]$PypiMirror = "",
    [switch]$SkipModels
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$overlayDir = $PSScriptRoot
$serverDir = Split-Path -Parent $overlayDir
if (-not $GsvDir) {
    $GsvDir = Join-Path (Split-Path -Parent $serverDir) "gpt-sovits-studio"
}
$GsvDir = [System.IO.Path]::GetFullPath($GsvDir)
$patchScript = Join-Path $overlayDir "apply_xiaozhi_gsv_patch.py"

Write-Host "=========================================================" -ForegroundColor Cyan
Write-Host "  GPT-SoVITS Windows 免 MSVC 环境配置与小智补丁安装向导" -ForegroundColor Cyan
Write-Host "=========================================================" -ForegroundColor Cyan
Write-Host "目标目录: $GsvDir" -ForegroundColor DarkCyan

if (-not (Test-Path (Join-Path $GsvDir "api_v2.py"))) {
    Write-Host "[错误] 未在 $GsvDir 找到 api_v2.py！请先克隆或解压 GPT-SoVITS 源码仓库，或通过 -GsvDir 指定正确路径。" -ForegroundColor Red
    exit 1
}

function Resolve-HostPython {
    param([string]$ExplicitExe)
    if ($ExplicitExe -and (Test-Path $ExplicitExe)) { return $ExplicitExe }
    $snippet = "import sys; assert (3, 10) <= sys.version_info[:2] <= (3, 12); print(sys.executable)"
    foreach ($probe in @(
        @{ Cmd = "py"; Args = @("-3.12") },
        @{ Cmd = "py"; Args = @("-3.11") },
        @{ Cmd = "py"; Args = @("-3.10") },
        @{ Cmd = "python"; Args = @() }
    )) {
        $ci = Get-Command $probe.Cmd -ErrorAction SilentlyContinue
        if (-not $ci) { continue }
        if ($ci.Source -and ($ci.Source -match '(?i)\\WindowsApps\\') -and $probe.Cmd -ne "py") { continue }
        try {
            $allArgs = @($probe.Args) + @("-c", $snippet)
            $out = & $ci.Source @allArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $exe = ($out | Select-Object -First 1).ToString().Trim()
                if ($exe -and (Test-Path $exe)) { return $exe }
            }
        } catch {}
    }
    return ""
}

$venvDir = Join-Path $GsvDir ".venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $hostPy = Resolve-HostPython -ExplicitExe $PythonExe
    if (-not $hostPy) {
        Write-Host "[错误] 未找到可用的 Python 3.10～3.12，请先安装 Python 3.11 或 3.12（勾选 Add python.exe to PATH）。" -ForegroundColor Red
        exit 1
    }
    Write-Host "==> [1/4] 正在创建虚拟环境 $venvDir (基于 $hostPy)..." -ForegroundColor Cyan
    & $hostPy -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        Write-Host "[错误] 创建虚拟环境失败！" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "==> [1/4] 检测到已有虚拟环境: $venvPy" -ForegroundColor Green
}

$rawReqFile = Join-Path $GsvDir "requirements.txt"
$cleanReqFile = Join-Path $GsvDir "requirements_xiaozhi_win.txt"
$filteredLines = [System.Collections.Generic.List[string]]::new()
if (Test-Path $rawReqFile) {
    foreach ($ln in [System.IO.File]::ReadAllLines($rawReqFile, [System.Text.Encoding]::UTF8)) {
        $t = $ln.Trim()
        if (-not $t -or $t.StartsWith("#")) { continue }
        if ($t -match '^--no-binary') { continue }
        if ($t -match '^(opencc|jieba[_-]fast|pyopenjtalk|python_mecab_ko)(\b|[><=~;\s])') { continue }
        $filteredLines.Add($t)
    }
}
foreach ($extraPkg in @(
    "opencc-python-reimplemented>=0.1.7",
    "resampy>=0.4.2,<0.5",
    "jieba>=0.42.1",
    "soundfile",
    "fastapi[standard]>=0.115.2",
    "uvicorn"
)) {
    $filteredLines.Add($extraPkg)
}
[System.IO.File]::WriteAllLines($cleanReqFile, $filteredLines, [System.Text.UTF8Encoding]::new($false))

Write-Host "==> [2/4] 正在安装免 MSVC 二进制依赖 (PyTorch CUDA + opencc-python-reimplemented + resampy)..." -ForegroundColor Cyan
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
$mirrorArgs = @()
if ($PypiMirror) {
    $mirrorArgs = @("-i", $PypiMirror)
} elseif ($env:PIP_INDEX_URL) {
    $mirrorArgs = @("-i", $env:PIP_INDEX_URL)
}

try {
    if ($uvCmd) {
        $env:UV_LINK_MODE = "copy"
        & $uvCmd.Source pip install --python $venvPy "torch==2.8.0+cu128" "torchaudio==2.8.0+cu128" --extra-index-url $CudaIndexUrl @mirrorArgs -r $cleanReqFile
        if ($LASTEXITCODE -ne 0) { throw "uv pip install 失败 (exit=$LASTEXITCODE)" }
        & $uvCmd.Source pip install --python $venvPy @mirrorArgs faster-whisper --no-deps
        if ($LASTEXITCODE -ne 0) { throw "faster-whisper 安装失败 (exit=$LASTEXITCODE)" }
    } else {
        & $venvPy -m pip install --upgrade pip wheel
        & $venvPy -m pip install "torch==2.8.0+cu128" "torchaudio==2.8.0+cu128" --extra-index-url $CudaIndexUrl @mirrorArgs -r $cleanReqFile
        if ($LASTEXITCODE -ne 0) { throw "pip install 失败 (exit=$LASTEXITCODE)" }
        & $venvPy -m pip install @mirrorArgs faster-whisper --no-deps
        if ($LASTEXITCODE -ne 0) { throw "faster-whisper 安装失败 (exit=$LASTEXITCODE)" }
    }
} finally {
    Remove-Item -Path $cleanReqFile -Force -ErrorAction SilentlyContinue
}

function Save-ArchiveWithFallback {
    param([string]$FileName, [string]$OutFile)
    $msUrl = "https://www.modelscope.cn/models/XXXXRT/GPT-SoVITS-Pretrained/resolve/master/$FileName"
    $hfUrl = "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/$FileName"
    Write-Host "  正在从 ModelScope 下载 $FileName ..." -ForegroundColor DarkCyan
    & curl.exe -sS -L --fail --retry 3 -o $OutFile $msUrl
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ModelScope 下载失败，回退至 HuggingFace 下载 $FileName ..." -ForegroundColor Yellow
        & curl.exe -sS -L --fail --retry 3 -o $OutFile $hfUrl
        if ($LASTEXITCODE -ne 0) {
            throw "下载 $FileName 失败，请检查网络连接"
        }
    }
}

if (-not $SkipModels) {
    Write-Host "==> [3/4] 正在检查基础预训练模型与中文多音字前端 (G2PW / NLTK)..." -ForegroundColor Cyan
    Push-Location $GsvDir
    try {
        if (-not (Test-Path "GPT_SoVITS\pretrained_models\chinese-roberta-wwm-ext-large")) {
            $tmpPre = Join-Path $GsvDir "pretrained_models.zip"
            Save-ArchiveWithFallback -FileName "pretrained_models.zip" -OutFile $tmpPre
            & tar.exe -xf $tmpPre -C "GPT_SoVITS"
            Remove-Item -Path $tmpPre -Force -ErrorAction SilentlyContinue
        }
        if (-not (Test-Path "GPT_SoVITS\text\G2PWModel")) {
            $tmpG2p = Join-Path $GsvDir "G2PWModel.zip"
            Save-ArchiveWithFallback -FileName "G2PWModel.zip" -OutFile $tmpG2p
            & tar.exe -xf $tmpG2p -C "GPT_SoVITS\text"
            Remove-Item -Path $tmpG2p -Force -ErrorAction SilentlyContinue
        }
        if (-not (Test-Path (Join-Path $venvDir "nltk_data"))) {
            $tmpNltk = Join-Path $GsvDir "nltk_data.zip"
            Save-ArchiveWithFallback -FileName "nltk_data.zip" -OutFile $tmpNltk
            & tar.exe -xf $tmpNltk -C $venvDir
            Remove-Item -Path $tmpNltk -Force -ErrorAction SilentlyContinue
        }
        $ttsPyFile = Join-Path $GsvDir "GPT_SoVITS\TTS_infer_pack\TTS.py"
        if ((Test-Path $ttsPyFile) -and ([System.IO.File]::ReadAllText($ttsPyFile, [System.Text.Encoding]::UTF8) -match "gsv-v5-pretrained")) {
            $v5Dir = Join-Path $GsvDir "GPT_SoVITS\pretrained_models\gsv-v5-pretrained"
            New-Item -ItemType Directory -Path $v5Dir -Force | Out-Null
            foreach ($v5File in @("s2Gv5turbo.pth", "vocoder.pth")) {
                $dstV5 = Join-Path $v5Dir $v5File
                if (-not (Test-Path $dstV5)) {
                    $v5Urls = @(
                        "https://www.modelscope.cn/models/lj1995/GPT-SoVITS/resolve/master/gsv-v5-pretrained/$v5File",
                        "https://hf-mirror.com/lj1995/GPT-SoVITS/resolve/main/gsv-v5-pretrained/$v5File",
                        "https://huggingface.co/lj1995/GPT-SoVITS/resolve/main/gsv-v5-pretrained/$v5File"
                    )
                    $downloadedV5 = $false
                    foreach ($u in $v5Urls) {
                        Write-Host "  正在下载 V5-Turbo 底模 $v5File ($u) ..." -ForegroundColor DarkCyan
                        & curl.exe -sS -L --fail --retry 2 -o "$dstV5.part" $u
                        if ($LASTEXITCODE -eq 0 -and (Test-Path "$dstV5.part") -and ((Get-Item "$dstV5.part").Length -gt 1048576)) {
                            Move-Item -Path "$dstV5.part" -Destination $dstV5 -Force
                            $downloadedV5 = $true
                            break
                        }
                        Remove-Item -Path "$dstV5.part" -Force -ErrorAction SilentlyContinue
                    }
                    if (-not $downloadedV5) {
                        Write-Host "  [提示] 未能自动下载 gsv-v5-pretrained\$v5File；若需使用 V5-Turbo 音色，请从 https://huggingface.co/lj1995/GPT-SoVITS/tree/main/gsv-v5-pretrained 手动放入 GPT_SoVITS\pretrained_models\gsv-v5-pretrained\" -ForegroundColor Yellow
                    }
                }
            }
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "==> [3/4] 已按 -SkipModels 跳过基础预训练模型下载" -ForegroundColor DarkGray
}

Write-Host "==> [4/4] 正在注入小智专用补丁 (--log_file / /prime / CUDA Graph OOM 安全回退 / jieba_fast 兼容层)..." -ForegroundColor Cyan
& $venvPy -X utf8 $patchScript --gsv-dir $GsvDir --install-venv-shims
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 注入小智 GPT-SoVITS 补丁失败！" -ForegroundColor Red
    exit 1
}

& $venvPy -c "import torch, resampy, jieba_fast; from opencc import OpenCC; print('[自检通过] PyTorch:', torch.__version__, '| CUDA:', torch.cuda.is_available(), '| OpenCC/resampy/jieba_fast OK')"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] Python 运行时依赖自检未通过！" -ForegroundColor Red
    exit 1
}

Write-Host "`n[完成] 宿主机 GPT-SoVITS (19880) 环境与补丁已就绪！现在可直接双击「启动或重启服务.bat」。" -ForegroundColor Green
