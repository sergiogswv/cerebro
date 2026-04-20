"""Project Manager - Handles project lifecycle and bootstrap."""

import os
import logging
from typing import Dict, List, Optional
from pathlib import Path

from app.sockets import emit_system_status
from app.dispatcher import notify

import shutil
import subprocess

logger = logging.getLogger("cerebro.project")


class ProjectManager:
    """
    Manages project selection and workspace scanning.

    Responsibilities:
    - Scan workspace for projects
    - Handle project selection/activation
    - Bootstrap initial setup
    """

    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root
        self._active_project: Optional[str] = None
        self._monitored_project: Optional[str] = None

    @property
    def active_project(self) -> Optional[str]:
        return self._active_project

    @property
    def monitored_project(self) -> Optional[str]:
        return self._monitored_project

    def set_monitored(self, project: str):
        """Mark project as successfully monitored."""
        self._monitored_project = project

    async def scan_projects(self) -> List[str]:
        """Scan workspace for available projects."""
        try:
            projects = [
                d for d in os.listdir(self.workspace_root)
                if os.path.isdir(os.path.join(self.workspace_root, d))
                and not d.startswith(".")
            ]
            return sorted(projects)[:20]  # Limit to 20
        except Exception as e:
            logger.error(f"Error scanning projects: {e}")
            return []

    async def bootstrap(self) -> Dict:
        """Bootstrap system by scanning projects and requesting selection."""
        logger.info("🚀 Starting bootstrap")

        projects = await self.scan_projects()

        # Request project selection via notifier
        from app.config import get_settings
        import httpx

        settings = get_settings()

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{settings.notifier_url}/ask-project",
                    json={"projects": projects[:10]},  # Limit for Telegram
                    timeout=10.0
                )
        except Exception as e:
            logger.warning(f"Notifier unavailable: {e}")

        return {"status": "ok", "projects": projects}

    async def set_active(
        self,
        project: str,
        on_activate=None
    ) -> Dict:
        """
        Set active project and notify systems.

        Args:
            project: Project name
            on_activate: Optional callback when project changes

        Returns:
            Status dict
        """
        # Skip if same and already monitored
        if self._active_project == project and self._monitored_project == project:
            logger.info(f"Project {project} already active and monitored")
            return {"status": "ok", "project": project, "restarted": False}

        self._active_project = project
        logger.info(f"📁 Active project set: {project}")

        # Persist state
        from app.context_db import get_context_db
        db = get_context_db()
        # Mark others as idle, this one as active
        active_projects = db.get_projects_by_state('active')
        for p in active_projects:
            if p != project:
                db.set_project_state(p, 'idle')
        db.set_project_state(project, 'active')

        # Notify dashboard
        await emit_system_status({
            "type": "project_selected",
            "project": project
        })

        # Call activation callback if provided
        if on_activate:
            try:
                await on_activate(project)
            except Exception as e:
                logger.error(f"Activation callback error: {e}")

        return {"status": "ok", "project": project, "restarted": True}

    async def create_project(self, name: str, project_type: str = "generic", description: str = "", base_path: Optional[str] = None) -> Dict:
        """Create a new project directory in workspace root or custom path."""
        if not name:
            return {"status": "error", "message": "Nombre del proyecto es requerido"}

        # Sanitizar nombre
        name = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()
        
        # Determinar directorio padre
        parent_dir = base_path or self.workspace_root
        parent_dir = os.path.expanduser(parent_dir)
        parent_dir = os.path.abspath(parent_dir)
        
        path = os.path.join(parent_dir, name)

        if os.path.exists(path):
            return {"status": "error", "message": f"El proyecto '{name}' ya existe en esa ubicación"}

        try:
            os.makedirs(path, exist_ok=True)
            
            # Inicializar con un README mínimo que incluya la descripción
            readme_path = os.path.join(path, "README.md")
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(f"# {name}\n\n{description or 'Proyecto creado desde Skrymir Suite.'}\n")
                if project_type != "generic":
                    f.write(f"\nTipo de proyecto: {project_type}\n")

            logger.info(f"✨ Proyecto nuevo creado: {name} en {path} (Tipo: {project_type})")

            # Opcional: git init si está disponible
            try:
                subprocess.run(["git", "init"], cwd=path, capture_output=True)
            except:
                pass

            return {
                "status": "ok", 
                "project": name, 
                "path": path.replace("\\", "/"),
                "project_type": project_type,
                "description": description
            }
        except Exception as e:
            logger.error(f"Error creando proyecto: {e}")
            return {"status": "error", "message": str(e)}

    def get_project_path(self, project: Optional[str] = None) -> str:
        """Get full path to project directory."""
        proj = project or self._active_project
        if not proj:
            return "."
        return os.path.join(self.workspace_root, proj).replace("\\", "/")

    def is_valid_project(self, project: str) -> bool:
        """Check if project exists in workspace."""
        path = self.get_project_path(project)
        return os.path.exists(path) and os.path.isdir(path)
