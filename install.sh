#!/bin/bash
#
# Cerebro Installer
# Instala dependencias de Python y configura el entorno virtual
# Incluye soporte para Vector DB (Chroma + sentence-transformers)
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"

log_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║              Cerebro Installer                             ║"
echo "║         (with Vector DB support)                           ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Check Python version
log_info "Checking Python version..."
python_version=$(python3 --version 2>/dev/null || python --version 2>/dev/null || echo "not_found")

if [[ "$python_version" == "not_found" ]]; then
    log_error "Python not found. Please install Python 3.9 or higher."
    exit 1
fi

log_success "Python found: $python_version"

# Create virtual environment
if [[ -d "$VENV_DIR" ]]; then
    log_warning "Virtual environment already exists at $VENV_DIR"
    read -p "Recreate? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_info "Removing existing virtual environment..."
        rm -rf "$VENV_DIR"
    fi
fi

if [[ ! -d "$VENV_DIR" ]]; then
    log_info "Creating virtual environment..."
    python3 -m venv "$VENV_DIR" || python -m venv "$VENV_DIR"
    log_success "Virtual environment created"
fi

# Activate virtual environment
log_info "Activating virtual environment..."
source "$VENV_DIR/bin/activate"

# Upgrade pip
log_info "Upgrading pip..."
pip install --upgrade pip

# Install requirements
if [[ -f "$REQUIREMENTS_FILE" ]]; then
    log_info "Installing dependencies from requirements.txt..."
    log_info "This includes Vector DB packages (chromadb, sentence-transformers)..."
    echo ""

    # Install base requirements first
    pip install -r "$REQUIREMENTS_FILE"

    log_success "Dependencies installed"
else
    log_error "requirements.txt not found at $REQUIREMENTS_FILE"
    exit 1
fi

# Verify installation
log_info "Verifying installation..."
echo ""

# Check FastAPI
type -P fastapi &>/dev/null || python -c "import fastapi" 2>/dev/null && log_success "FastAPI: OK" || log_warning "FastAPI: not found"

# Check Chroma (optional but important)
if python -c "import chromadb" 2>/dev/null; then
    log_success "ChromaDB: OK (Vector DB ready)"
else
    log_warning "ChromaDB: not installed (Vector search will be disabled)"
fi

# Check sentence-transformers
if python -c "import sentence_transformers" 2>/dev/null; then
    log_success "Sentence Transformers: OK (Embeddings ready)"
else
    log_warning "Sentence Transformers: not installed (Vector search will be disabled)"
fi

# Check scikit-learn
if python -c "import sklearn" 2>/dev/null; then
    log_success "Scikit-learn: OK (Clustering ready)"
else
    log_warning "Scikit-learn: not installed (Clustering will be disabled)"
fi

echo ""
log_success "Installation complete!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "To start Cerebro:"
echo "  cd cerebro"
echo "  ./start.sh"
echo ""
echo "Or manually:"
echo "  source venv/bin/activate"
echo "  uvicorn app.main:app --host 0.0.0.0 --port 4000"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
