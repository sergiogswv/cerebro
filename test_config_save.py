
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.append(r"c:\Users\Sergio\skrymir-suite\cerebro")

from app.config_manager import UnifiedConfigManager
from app.models.config import UnifiedConfig, CerebroConfig

def test_save():
    manager = UnifiedConfigManager.get_instance()
    cfg = manager.get_config()
    
    print(f"Current config has cerebro: {hasattr(cfg, 'cerebro')}")
    if hasattr(cfg, 'cerebro'):
        print(f"Cerebro model: {cfg.cerebro.auto_fix_model}")
    
    # Force set something in cerebro and save
    if not hasattr(cfg, 'cerebro') or cfg.cerebro is None:
        cfg.cerebro = CerebroConfig()
    
    cfg.cerebro.auto_fix_model = "deepseek-coder-v2:16b-lite-instruct-q4_K_M"
    manager._config = cfg
    success = manager._save()
    
    print(f"Save success: {success}")
    print(f"Path: {manager._config_path}")

if __name__ == "__main__":
    test_save()
