import socketio
import logging
import uuid
from datetime import datetime

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
    Agrega timestamp e ID único para que el dashboard los renderice.
    Guarda eventos de interacción pendientes para clientes futuros.
    También guarda el estado de readiness de los agentes.
    """
    try:
        # Agregar timestamp e ID único para cada evento
        event_with_metadata = {
            **event_data,
            'id': str(uuid.uuid4()),
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }

        # Guardar estado de readiness de los agentes (antes de modificar event_data)
        if event_data.get('type') in ['sentinel_ready', 'architect_ready', 'warden_ready']:
            agent = event_data.get('source')
            if agent and agent in agent_ready_state:
                agent_ready_state[agent] = True
                logger.info(f"💾 Estado {agent}_ready guardado")

        # Guardar eventos de interacción para clientes que se conecten después
        # Y emitir inmediatamente a clientes conectados
        if event_data.get('type') == 'interaction_required':
            pending_interaction_events.append(event_with_metadata)
            logger.info(f"💾 Evento de interacción guardado (pendientes: {len(pending_interaction_events)})")
            # Emitir el evento CON metadata (id, timestamp)
            await sio.emit('agent_event', event_with_metadata)
            logger.info(f"📤 interaction_required emitido: step={event_with_metadata.get('payload', {}).get('wizard_step')}, prompt_id={event_with_metadata.get('payload', {}).get('prompt_id')}")
        else:
            # Para eventos normales, usamos metadata y emitimos
            event_data = event_with_metadata
            await sio.emit('agent_event', event_data)
            logger.debug(f"📤 Evento emitido por Socket.IO: {event_data.get('type')}")
    except Exception as e:
        logger.error(f"❌ Error emitiendo por Socket.IO: {e}")

async def emit_system_status(status_data: dict):
    """
    Emite cambios en el estado del sistema (ej: nuevo proyecto activo).
    """
    try:
        await sio.emit('system_status', status_data)
    except Exception as e:
        logger.error(f"❌ Error emitiendo status por Socket.IO: {e}")
