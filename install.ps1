#
# Cerebro Installer for Windows (PowerShell)
# Instala dependencias de Python y configura el entorno virtual
# Incluye soporte para Vector DB (Chroma + sentence-transformers)
#

$ErrorActionPreference = "Stop"

$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$VENV_DIR = Join-Path $SCRIPT_DIR "venv"
$REQUIREMENTS_FILE = Join-Path $SCRIPT_DIR "requirements.txt"

function Write-Info { param($msg) Write-Host "ℹ️  $msg" -ForegroundColor Blue }
function Write-Success { param($msg) Write-Host "✅ $msg" -ForegroundColor Green }
function Write-Warning { param($msg) Write-Host "⚠️  $msg" -ForegroundColor Yellow }
function Write-Error { param($msg) Write-Host "❌ $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════��═════╗" -ForegroundColor Cyan
Write-Host "║              Cerebro Installer                             ║" -ForegroundColor Cyan
Write-Host "║         (with Vector DB support)                           ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# Check Python
Write-Info "Checking Python..."
try {
    $pythonVersion = python --version 2>$null
    if (-not $pythonVersion) {
        $pythonVersion = python3 --version 2>$null
    }
    Write-Success "Python found: $pythonVersion"
} catch {
    Write-Error "Python not found. Please install Python 3.9 or higher from https://python.org"
    exit 1
}

# Create virtual environment
if (Test-Path $VENV_DIR) {
    Write-Warning "Virtual environment already exists at $VENV_DIR"
    $recreate = Read-Host "Recreate? (y/N)"
    if ($recreate -eq 'y' -or $recreate -eq 'Y') {
        Write-Info "Removing existing virtual environment..."
        Remove-Item -Recurse -Force $VENV_DIR
    }
}

if (-not (Test-Path $VENV_DIR)) {
    Write-Info "Creating virtual environment..."
    python -m venv $VENV_DIR
    Write-Success "Virtual environment created"
}

# Activate virtual environment (for package installation)
Write-Info "Activating virtual environment..."
$venvPython = Join-Path $VENV_DIR "Scripts\python.exe"
$venvPip = Join-Path $VENV_DIR "Scripts\pip.exe"

# Upgrade pip
Write-Info "Upgrading pip..."
& $venvPython -m pip install --upgrade pip

# Install requirements
if (Test-Path $REQUIREMENTS_FILE) {
    Write-Info "Installing dependencies from requirements.txt..."
    Write-Info "This includes Vector DB packages (chromadb, sentence-transformers)..."
    Write-Host ""

    & $venvPip install -r $REQUIREMENTS_FILE

    Write-Success "Dependencies installed"
} else {
    Write-Error "requirements.txt not found at $REQUIREMENTS_FILE"
    exit 1
}

# Verify installation
Write-Info "Verifying installation..."
Write-Host ""

# Check Chroma
try {
    & $venvPython -c "import chromadb" 2>$null
    Write-Success "ChromaDB: OK (Vector DB ready)"
} catch {
    Write-Warning "ChromaDB: not installed (Vector search will be disabled)"
}

# Check sentence-transformers
try {
    & $venvPython -c "import sentence_transformers" 2>$null
    Write-Success "Sentence Transformers: OK (Embeddings ready)"
} catch {
    Write-Warning "Sentence Transformers: not installed (Vector search will be disabled)"
}

# Check scikit-learn
try {
    & $venvPython -c "import sklearn" 2>$null
    Write-Success "Scikit-learn: OK (Clustering ready)"
} catch {
    Write-Warning "Scikit-learn: not installed (Clustering will be disabled)"
}

Write-Host ""
Write-Success "Installation complete!"
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
Write-Host ""
Write-Host "To start Cerebro:"
Write-Host "  cd cerebro"
Write-Host "  .\start.bat"
Write-Host ""
Write-Host "Or manually:"
Write-Host "  .\venv\Scripts\activate"
Write-Host "  uvicorn app.main:app --host 0.0.0.0 --port 4000"
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

Pause
