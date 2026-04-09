import socketio
import logging
import uuid
from datetime import datetime

from app.schemas.events import validate_event, create_event, EventType, EventValidationError

logger = logging.getLogger("cerebro.sockets")

# Crear servidor Socket.IO asíncrono
# allow_allowed_origins="*" para desarrollo
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio)

# Cola de eventos pendientes para clientes que se conecten después
pending_interaction_events = []

# Estado de readiness de los agentes (para enviar a clientes que se conecten)
agent_ready_state = {
    'sentinel': False,
    'architect': False,
    'warden': False,
}

@sio.event
async def connect(sid, environ):
    logger.info(f"🔌 Cliente conectado: {sid}")
    # Enviar eventos de interacción pendientes al nuevo cliente
    for event_data in pending_interaction_events:
        try:
            await sio.emit('agent_event', event_data, to=sid)
            logger.debug(f"📤 Evento pendiente enviado a {sid}: {event_data.get('type')}")
        except Exception as e:
            logger.error(f"❌ Error enviando evento pendiente: {e}")

    # Enviar estado de readiness de los agentes
    for agent, ready in agent_ready_state.items():
        if ready:
            try:
                await sio.emit('agent_event', {
                    'source': agent,
                    'type': f'{agent}_ready',
                    'severity': 'info',
                    'payload': {'ready': True}
                }, to=sid)
                logger.debug(f"📤 Estado ready de {agent} enviado a {sid}")
            except Exception as e:
                logger.error(f"❌ Error enviando estado ready de {agent}: {e}")

@sio.event
async def disconnect(sid):
    logger.info(f"🔌 Cliente desconectado: {sid}")

async def emit_agent_event(event_data: dict):
    """
    Emite un evento de agente a todos los clientes conectados.
    Valida contra schema antes de emitir.
    Agrega timestamp e ID único para que el dashboard los renderice.
    Guarda eventos de interacción pendientes para clientes futuros.
    También guarda el estado de readiness de los agentes.
    """
    import uuid
    from datetime import datetime, timezone

    # Asegurar que el evento tenga id y timestamp (requerido por el frontend)
    if not event_data.get('id'):
        event_data['id'] = str(uuid.uuid4())
    if not event_data.get('timestamp'):
        event_data['timestamp'] = datetime.now(timezone.utc).isoformat()

    try:
        event_type = event_data.get('type', 'unknown')
        event_source = event_data.get('source', 'unknown')
        print(f"[SOCKET DEBUG] emit_agent_event llamado: source={event_source}, type={event_type}, id={event_data.get('id')}")

        # Validate event against schema
        validated = validate_event(event_data)
        event_with_metadata = validated.model_dump()
        print(f"[SOCKET DEBUG] Evento validado correctamente")

        # Guardar estado de readiness de los agentes
        if event_data.get('type') in ['sentinel_ready', 'architect_ready', 'warden_ready',
                                       'sentinel_adk_ready', 'architect_adk_ready', 'warden_adk_ready']:
            agent = event_data.get('source')
            if agent and agent in agent_ready_state:
                agent_ready_state[agent] = True
                logger.info(f"💾 Estado {agent}_ready guardado")

        # Guardar eventos de interacción para clientes que se conecten después
        if event_data.get('type') == 'interaction_required':
            pending_interaction_events.append(event_with_metadata)
            logger.info(f"💾 Evento de interacción guardado (pendientes: {len(pending_interaction_events)})")

        print(f"[SOCKET DEBUG] Emitiendo evento por Socket.IO...")
        await sio.emit('agent_event', event_with_metadata)
        print(f"[SOCKET DEBUG] ✅ Evento emitido exitosamente: {event_type}")
        logger.debug(f"📤 Evento emitido por Socket.IO: {event_data.get('type')}")

    except EventValidationError as e:
        logger.warning(f"⚠️ Evento con schema inválido: {e}")
        print(f"[SOCKET DEBUG] ⚠️ Schema inválido, emitiendo de todos modos: {e}")
        # Still emit but log the issue
        await sio.emit('agent_event', event_data)
        print(f"[SOCKET DEBUG] ✅ Evento emitido (con warning de schema)")
    except Exception as e:
        logger.error(f"❌ Error emitiendo por Socket.IO: {e}")
        print(f"[SOCKET DEBUG] ❌ Error emitiendo: {e}")

async def emit_system_status(status_data: dict):
    """
    Emite cambios en el estado del sistema (ej: nuevo proyecto activo).
    """
    try:
        await sio.emit('system_status', status_data)
    except Exception as e:
        logger.error(f"❌ Error emitiendo status por Socket.IO: {e}")

async def emit_pipeline_event(event_type: str, payload: dict):
    """Emit pipeline event to dashboard with schema validation."""
    try:
        from app.schemas.events import EventType

        event_type_enum = EventType(f"pipeline_{event_type}")
        event_data = create_event(
            source='cerebro',
            event_type=event_type_enum,
            payload=payload
        )
        await sio.emit('agent_event', event_data)
        logger.debug(f"📤 Pipeline event emitted: {event_type}")
    except Exception as e:
        logger.error(f"❌ Error emitting pipeline event: {e}")
