"""
routes/warden.py — Endpoints de Warden (acciones + configuración ADK + memoria).

Rutas:
  POST /warden/scan
  POST /warden/predict-critical
  POST /warden/risk-assess
  POST /warden/churn-report
  GET  /warden/config           ← modo core|adk + LLM provider
  POST /warden/config           ← persiste en .env
  GET  /warden/memory           ← proxy al sidecar ADK
  POST /warden/command          ← dispatcher genérico (core + adk)
"""

import logging
import os
import re
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from app.models import ApiResponse
from app.orchestrator import orchestrator
from app.dispatcher import send_raw_command

router = APIRouter(tags=["warden"])
logger = logging.getLogger("cerebro.routes.warden")


# ─── Helper: emitir resultado como evento WS ──────────────────────────────────

async def _emit(event_type: str, severity: str, payload: dict) -> None:
    from app.sockets import emit_agent_event
    await emit_agent_event({"source": "warden", "type": event_type,
                            "severity": severity, "payload": payload})


# ─── Acciones clásicas (legacy, usan orchestrator) ────────────────────────────

@router.post("/warden/scan", response_model=ApiResponse)
async def warden_scan():
    result = await orchestrator.warden_scan()
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(ok=False, message=result["error"])
    await _emit("scan_completed", "info", {"message": "Warden scan completado", "result": result})
    return ApiResponse(ok=True, message="Escaneo de Warden ejecutado", data=result)


@router.post("/warden/predict-critical", response_model=ApiResponse)
async def warden_predict_critical():
    result = await orchestrator.warden_predict_critical()
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(ok=False, message=result["error"])
    await _emit("predict_critical_completed", "info", {"message": "Predicción de críticos completada", "result": result})
    return ApiResponse(ok=True, message="Predicción de críticos ejecutada", data=result)


@router.post("/warden/risk-assess", response_model=ApiResponse)
async def warden_risk_assess():
    result = await orchestrator.warden_risk_assess()
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(ok=False, message=result["error"])
    await _emit("risk_assess_completed", "info", {"message": "Evaluación de riesgos completada", "result": result})
    return ApiResponse(ok=True, message="Evaluación de riesgos completada", data=result)


@router.post("/warden/churn-report", response_model=ApiResponse)
async def warden_churn_report():
    result = await orchestrator.warden_churn_report()
    if isinstance(result, dict) and result.get("error"):
        return ApiResponse(ok=False, message=result["error"])
    await _emit("churn_report_completed", "info", {"message": "Reporte de churn generado", "result": result})
    return ApiResponse(ok=True, message="Reporte de churn generado", data=result)


# ─── Configuración ADK ────────────────────────────────────────────────────────

@router.get("/warden/config", response_model=ApiResponse,
            summary="Obtener configuración del Warden Agent")
async def get_warden_config():
    from app.config import get_settings
    s = get_settings()
    return ApiResponse(ok=True, data={
        "warden_mode":         s.warden_mode,
        "warden_url":          s.warden_url,
        "warden_adk_url":      s.warden_adk_url,
        "active_url":          s.warden_adk_url if s.warden_mode == "adk" else s.warden_url,
        "warden_llm_provider": s.warden_llm_provider,
    })


@router.post("/warden/config", response_model=ApiResponse,
             summary="Actualizar modo Warden (core | adk)")
async def save_warden_config(request: Request):
    data         = await request.json()
    new_mode     = data.get("warden_mode")
    new_provider = data.get("warden_llm_provider")
    ollama_url   = data.get("ollama_base_url")
    ollama_model = data.get("ollama_model")

    if new_mode not in ("core", "adk"):
        raise HTTPException(status_code=400, detail="warden_mode debe ser 'core' o 'adk'")

    env_path = Path(__file__).parent.parent.parent / ".env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

    def _upsert(text: str, key: str, value: str) -> str:
        if re.search(rf"^{key}=", text, re.MULTILINE):
            return re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.MULTILINE)
        return text + f"\n{key}={value}"

    # 1. Guardar en cerebro/.env (para estado del Dashboard)
    env_text = _upsert(env_text, "WARDEN_MODE", new_mode)
    if new_provider:
        env_text = _upsert(env_text, "WARDEN_LLM_PROVIDER", new_provider)
    if ollama_url:
        env_text = _upsert(env_text, "OLLAMA_BASE_URL", ollama_url)
    if ollama_model:
        env_text = _upsert(env_text, "OLLAMA_MODEL", ollama_model)
    env_path.write_text(env_text, encoding="utf-8")

    # 2. Sincronizar hacia warden/warden_agent/.env (para que el sidecar Python lo use real)
    warden_env_path = Path(__file__).parent.parent.parent.parent / "warden" / "warden_agent" / ".env"
    
    # Crear o leer
    we_text = warden_env_path.read_text(encoding="utf-8") if warden_env_path.exists() else ""
    
    if new_provider:
        we_text = _upsert(we_text, "LLM_PROVIDER", new_provider)
    if ollama_url:
        we_text = _upsert(we_text, "OLLAMA_BASE_URL", ollama_url)
    if ollama_model:
        we_text = _upsert(we_text, "OLLAMA_MODEL", ollama_model)
    
    warden_env_path.write_text(we_text, encoding="utf-8")

    await _emit("config_updated", "info", {
        "warden_mode": new_mode, "warden_llm_provider": new_provider,
        "message": f"Warden → modo '{new_mode}'. Reinicia Cerebro para aplicar.",
    })
    return ApiResponse(ok=True,
                       message=f"Guardado. Reinicia Cerebro para activar modo '{new_mode}'.",
                       data={"warden_mode": new_mode})


# ─── Memoria histórica (proxy al sidecar ADK) ─────────────────────────────────

@router.get("/warden/memory", response_model=ApiResponse,
            summary="Historial de memoria del Warden ADK")
async def get_warden_memory():
    import httpx
    from app.config import get_settings
    s = get_settings()

    if s.warden_mode != "adk":
        return ApiResponse(ok=False,
                           message="El modo actual es 'core'. Activa 'adk' para ver la memoria.")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{s.warden_adk_url}/memory/context")
            resp.raise_for_status()
            return ApiResponse(ok=True, data=resp.json())
    except httpx.ConnectError:
        return ApiResponse(ok=False, message=f"Sidecar ADK no disponible en {s.warden_adk_url}")
    except Exception as exc:
        return ApiResponse(ok=False, message=str(exc))


# ─── Comando genérico (core + adk) ───────────────────────────────────────────

@router.post("/warden/command", response_model=ApiResponse,
             summary="Enviar comando genérico a Warden")
async def warden_generic_command(request: Request):
    """
    Dispatcher genérico que el Dashboard usa para todas las acciones.
    Compatible con modo core y adk (mismo contrato /command).
    En modo ADK el campo { analysis } contiene la síntesis del LLM.
    Emite eventos WS de inicio y fin para visualización en tiempo real.
    """
    data   = await request.json()
    action = data.get("action", "scan")
    target = data.get("target", orchestrator.active_project)

    project_path = "."
    if target and target != "Ninguno":
        project_path = os.path.join(orchestrator.workspace_root, target).replace("\\", "/")

    await _emit(f"warden_{action.replace('-', '_')}_started", "info", {
        "action": action, "target": target,
        "message": f"⏳ Warden ejecutando '{action}'...",
    })

    ack        = await send_raw_command("warden", {
        "action": action, "target": project_path,
        "request_id": f"warden-{uuid.uuid4().hex[:8]}",
    })
    result_obj = ack.get("result") if isinstance(ack, dict) else {}
    analysis   = result_obj.get("analysis") if isinstance(result_obj, dict) else None
    severity   = result_obj.get("severity", "info") if isinstance(result_obj, dict) else "info"

    await _emit(f"warden_{action.replace('-', '_')}_completed", severity, {
        "action": action, "target": target,
        "status": ack.get("status") if isinstance(ack, dict) else "error",
        "analysis": analysis,
        "message": analysis[:200] if analysis else f"✅ Warden '{action}' completado",
    })

    if isinstance(ack, dict) and ack.get("status") == "rejected":
        return ApiResponse(ok=False, message=ack.get("error", "Warden rechazó el comando"))

    return ApiResponse(ok=True, message=f"Warden '{action}' ejecutado", data=ack)
