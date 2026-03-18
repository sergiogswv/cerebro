import socketio
import logging

logger = logging.getLogger("cerebro.sockets")

# Crear servidor Socket.IO asíncrono
# allow_allowed_origins="*" para desarrollo
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio)

# Cola de eventos pendientes para clientes que se conecten después
pending_interaction_events = []

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

@sio.event
async def disconnect(sid):
    logger.info(f"🔌 Cliente desconectado: {sid}")

async def emit_agent_event(event_data: dict):
    """
    Emite un evento de agente a todos los clientes conectados.
    Guarda eventos de interacción pendientes para clientes futuros.
    """
    try:
        # Guardar eventos de interacción para clientes que se conecten después
        if event_data.get('type') == 'interaction_required':
            pending_interaction_events.append(event_data)
            logger.info(f"💾 Evento de interacción guardado (pendientes: {len(pending_interaction_events)})")

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
