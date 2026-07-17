#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# =============================================================================
# arbol-de-nodos — v0.2
#
# Muestra la topologia del mesh como GRAFO DE FUERZAS (no arbol estricto),
# con raiz anclada al borde izquierdo. Se abandono d3.stratify()/d3.tree()
# porque en Meshtastic el camino de ida y el de vuelta pueden pasar por
# relays distintos (ruteo asimetrico) — un modelo de "un solo padre por
# nodo" no alcanza para representar eso. Ahora se guardan DOS rutas por
# nodo (ida/vuelta), cada una con su propia politica "ultima gana".
#
# Cambios sobre v0.1:
#   - "aristas" ahora se llama "rutas" en todo el codigo/UI/CSV.
#   - Ruteo asimetrico: routes keyed por (direccion, node_id), no un solo
#     dict child->parent. direccion = "fwd" | "back".
#   - Layout: d3-force en vez de arbol jerarquico. Raiz fija (fx/fy) a la
#     izquierda; el resto se acomoda solo. Sesgo horizontal suave por
#     profundidad (hop_index) para que igual "fluya" de izquierda a derecha.
#   - GPS (POSITION_APP) y telemetria (TELEMETRY_APP: uptime, bateria,
#     voltaje, channel util, air util tx) ahora se capturan y se exportan
#     en nodos.csv, aunque no haya un mapa visual.
#   - BIND_HOST = 127.0.0.1 (antes 0.0.0.0): uso pensado como una sola
#     persona mirando desde la misma notebook durante un survey de campo,
#     no expuesto a la LAN. cors_allowed_origins ya no es "*" (default de
#     python-socketio = mismo origen solamente).
#
# Diferencias deliberadas contra mapa-mesh (se mantienen de v0.1):
#   - La raiz es un NODO (el conectado), no una coordenada fija.
#   - Traceroute se dispara para CUALQUIER nodo escuchado, no solo GPS.
#   - Atajo "direct": 0 saltos = vecino directo de la raiz en AMBAS
#     direcciones, sin gastar traceroute.
#   - Hardware/puerto serie propio, separado de mapa-mesh.
# =============================================================================

import time
import json
import queue as _queue
import threading
import logging
import logging.handlers
import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from flask import Flask, Response, jsonify
from flask_socketio import SocketIO
from pubsub import pub

import meshtastic.serial_interface
from meshtastic.protobuf import mesh_pb2, portnums_pb2

# =============================================================================
#                                   LOGGING
# =============================================================================

_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arbol_de_nodos.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        ),
    ]
)
log = logging.getLogger("arbol-de-nodos")
log.info(f"Log guardado en: {_LOG_FILE}")

# =============================================================================
#                                CONFIGURACION
# =============================================================================

SERIAL_PORT   = "/dev/ttyACM0"

# Solo localhost: uso pensado para una sola persona mirando desde la misma
# notebook durante un survey de campo. Si mas adelante hace falta ver esto
# desde el celular por wifi, hay que sumar autenticacion antes de exponerlo.
BIND_HOST     = "127.0.0.1"
BIND_PORT     = 8090

# Cooldown minimo entre traceroutes a un mismo nodo (segundos). Critico
# para no saturar la malla: no bajar de esto sin pensarlo dos veces.
TRACEROUTE_COOLDOWN_SEC = 5 * 60

# Cooldown hardcodeado por firmware entre traceroutes consecutivos.
# En prueba desde 31s -> 120s (fecha de este cambio: ver historial). Todavia
# no hay criterio escrito de que contar como "funciono" — definirlo antes de
# dar esto por confirmado.
FIRMWARE_COOLDOWN_SEC = 120

# Intervalo del barrido periodico que re-evalua TODOS los nodos conocidos
TRACEROUTE_PERIODIC_SEC = 10 * 60

# Borrar nodos no escuchados despues de N horas
PRUNE_AFTER_SEC = 36 * 60 * 60

# Intervalo de polling de fallback del frontend (segundos)
POLL_REFRESH_SEC = 10

# Backup de estado (nodos + rutas) para sobrevivir reinicios
BACKUP_INTERVAL_SEC = 15 * 60
BACKUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arbol_backup.json")

# =============================================================================
#                               MODELO DE DATOS
# =============================================================================

@dataclass
class NodeEntry:
    node_id:    str
    short_name: str            = ""
    long_name:  str            = ""
    role:       str            = ""   # ROUTER, ROUTER_LATE, CLIENT_MUTE, CLIENT, CLIENT_HIDDEN, etc.
    last_seen:  float          = 0.0
    rssi:       Optional[float] = None
    snr:        Optional[float] = None
    hops:       Optional[int]   = None   # saltos totales del ultimo broadcast recibido (informativo)
    is_root:    bool           = False
    last_traceroute_ts: float  = 0.0
    traceroute_pending: bool   = False
    packet_count: int          = 0   # cuantos paquetes recibimos de este nodo en la sesion
    # Telemetria (TELEMETRY_APP / deviceMetrics)
    uptime_sec:    Optional[int]   = None
    battery_level: Optional[int]   = None   # 0-100, o 101 = "alimentado externamente"
    voltage:       Optional[float] = None
    channel_util:  Optional[float] = None
    air_util_tx:   Optional[float] = None
    telemetry_ts:  float            = 0.0
    # Posicion (POSITION_APP)
    lat:        Optional[float] = None
    lon:        Optional[float] = None
    alt:        Optional[float] = None
    position_ts: float          = 0.0


@dataclass
class RouteEntry:
    """
    Describe el "proximo salto" de node_id EN UNA DIRECCION dada, hacia la
    raiz. direction="fwd" viene de decomponer route (ida, raiz->destino).
    direction="back" viene de decomponer routeBack (vuelta, destino->raiz).
    Se guardan por separado porque en Meshtastic pueden diferir.
    """
    node_id:    str
    next_hop:   str
    direction:  str       # "fwd" | "back"
    hop_index:  int       # posicion dentro de la cadena reconstruida (informativo)
    timestamp:  float
    source:     str = "traceroute"   # "traceroute" | "direct"


# =============================================================================
#                               ESTADO GLOBAL
# =============================================================================

nodes_lock: threading.Lock = threading.Lock()
nodes: Dict[str, NodeEntry] = {}

routes_lock: threading.Lock = threading.Lock()
# keyed por (direccion, node_id) -> unica ruta vigente en esa direccion ("ultima gana")
routes: Dict[Tuple[str, str], RouteEntry] = {}

state_lock     = threading.Lock()
last_packet_ts = 0.0
connected      = False
last_error     = ""

ROOT_ID: Optional[str] = None   # se completa al conectar, formato "!xxxxxxxx"

iface_lock = threading.Lock()
iface_ref: Optional[meshtastic.serial_interface.SerialInterface] = None

traceroute_queue = _queue.Queue()
_queued_nodes: set = set()
_queued_lock  = threading.Lock()
last_traceroute_sent_ts: float = 0.0
traceroute_global_lock = threading.Lock()


# =============================================================================
#                                   HELPERS
# =============================================================================

def now() -> float:
    return time.time()


def nodeid_to_hex(x) -> str:
    """
    Normaliza IDs de nodo a formato '!hex'. El SDK a veces entrega enteros
    decimales (nodos intermedios de traceroute) y a veces strings '!hex'.
    """
    try:
        n = int(x)
        return f"!{n:08x}"
    except (ValueError, TypeError):
        s = str(x)
        return s if s.startswith("!") else f"!{s}"


def update_node(node_id: str, **kwargs) -> "NodeEntry":
    with nodes_lock:
        entry = nodes.get(node_id)
        if not entry:
            entry = NodeEntry(node_id=node_id, last_seen=now())
            nodes[node_id] = entry
        for k, v in kwargs.items():
            if hasattr(entry, k) and v is not None:
                setattr(entry, k, v)
        entry.last_seen = now()
    return entry


def increment_packet_count(node_id: str):
    with nodes_lock:
        entry = nodes.get(node_id)
        if entry:
            entry.packet_count += 1


def update_route(node_id: str, next_hop: str, direction: str, hop_index: int, source: str = "traceroute"):
    """
    Fija/actualiza la ruta vigente de node_id EN ESA DIRECCION. Politica:
    'ultima gana' — CON UNA EXCEPCION explicita: un traceroute confirmado
    (verificacion activa) nunca es pisado por una deteccion "direct"
    pasiva (heuristica de hops==0 en un paquete cualquiera), sin importar
    cual sea mas reciente. Entre dos traceroutes, o entre dos detecciones
    "direct", sigue ganando el mas nuevo como antes.
    """
    if node_id == ROOT_ID or node_id == next_hop:
        return
    with routes_lock:
        key = (direction, node_id)
        existing = routes.get(key)
        if existing is not None and existing.source == "traceroute" and source == "direct":
            return
        routes[key] = RouteEntry(
            node_id=node_id, next_hop=next_hop, direction=direction,
            hop_index=hop_index, timestamp=now(), source=source,
        )


def prune_nodes():
    cutoff = now() - PRUNE_AFTER_SEC
    with nodes_lock:
        to_del = [nid for nid, e in nodes.items() if e.last_seen < cutoff and not e.is_root]
        for nid in to_del:
            del nodes[nid]
            log.info(f"Nodo purgado por inactividad: {nid}")
    with routes_lock:
        stale = [key for key in routes if key[1] not in nodes]
        for key in stale:
            del routes[key]


def serialize_nodes() -> list:
    with nodes_lock:
        return [
            {
                "node_id":    e.node_id,
                "short_name": e.short_name,
                "long_name":  e.long_name,
                "role":       e.role,
                "last_seen":  e.last_seen,
                "rssi":       e.rssi,
                "snr":        e.snr,
                "hops":       e.hops,
                "is_root":    e.is_root,
                "traceroute_pending": e.traceroute_pending,
                "packet_count": e.packet_count,
                "uptime_sec":    e.uptime_sec,
                "battery_level": e.battery_level,
                "voltage":       e.voltage,
                "channel_util":  e.channel_util,
                "air_util_tx":   e.air_util_tx,
                "lat": e.lat, "lon": e.lon, "alt": e.alt,
            }
            for e in nodes.values()
        ]


def serialize_routes() -> list:
    with routes_lock:
        return [
            {
                "node_id":   r.node_id,
                "next_hop":  r.next_hop,
                "direction": r.direction,
                "hop_index": r.hop_index,
                "timestamp": r.timestamp,
                "source":    r.source,
            }
            for r in routes.values()
        ]


def get_status() -> dict:
    with state_lock:
        age = (now() - last_packet_ts) if last_packet_ts else None

    with nodes_lock:
        total = len(nodes)
        snr_vals  = [e.snr  for e in nodes.values() if e.snr  is not None]
    with routes_lock:
        resolved_ids = {r.node_id for r in routes.values()}
    resolved = len(resolved_ids)
    orphans = max(total - resolved - 1, 0)  # -1 por la raiz, que nunca tiene ruta propia

    avg_snr  = round(sum(snr_vals)  / len(snr_vals),  1) if snr_vals  else None

    return {
        "connected":           connected,
        "root_id":             ROOT_ID,
        "last_packet_age_sec": age,
        "last_error":          last_error or None,
        "refresh_sec":         POLL_REFRESH_SEC,
        "total_nodes":         total,
        "resolved_routes":     resolved,
        "orphans":             orphans,
        "avg_snr":             avg_snr,
    }


def _csv_safe(value):
    """Neutraliza CSV/Formula Injection (CWE-1236), igual que en mapa-mesh."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


# =============================================================================
#                    TRACEROUTE ACTIVO — WORKER SERIALIZADO
# =============================================================================

def traceroute_worker():
    global last_traceroute_sent_ts

    while True:
        node_id = traceroute_queue.get()

        with _queued_lock:
            _queued_nodes.discard(node_id)

        with nodes_lock:
            exists = node_id in nodes
        if not exists:
            traceroute_queue.task_done()
            continue

        with traceroute_global_lock:
            elapsed = now() - last_traceroute_sent_ts
            wait_sec = FIRMWARE_COOLDOWN_SEC - elapsed
            if wait_sec > 0:
                time.sleep(wait_sec)

        with iface_lock:
            iface = iface_ref

        if iface is None:
            with nodes_lock:
                if node_id in nodes:
                    nodes[node_id].traceroute_pending = False
            traceroute_queue.task_done()
            continue

        log.info(f"Traceroute -> {node_id}")
        try:
            r = mesh_pb2.RouteDiscovery()
            iface.sendData(
                r,
                destinationId=node_id,
                portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                wantResponse=True,
                hopLimit=7,
            )
        except Exception as e:
            log.warning(f"sendData traceroute excepcion para {node_id}: {e}")

        with traceroute_global_lock:
            last_traceroute_sent_ts = now()

        with nodes_lock:
            if node_id in nodes:
                nodes[node_id].traceroute_pending = False
                nodes[node_id].last_traceroute_ts = now()

        socketio.emit("tree_update", {
            "status": get_status(), "nodes": serialize_nodes(), "routes": serialize_routes(),
        })
        traceroute_queue.task_done()


def maybe_schedule_traceroute(node_id: str):
    """
    Encola un traceroute para node_id si no es la raiz, no esta ya
    encolado/pendiente, y paso el cooldown por-nodo (5 min, ver comentario
    en TRACEROUTE_COOLDOWN_SEC: es asi de largo para no saturar la malla).
    """
    if node_id == ROOT_ID:
        return

    with nodes_lock:
        entry = nodes.get(node_id)
        if not entry:
            return
        pending     = entry.traceroute_pending
        elapsed     = now() - entry.last_traceroute_ts
        cooldown_ok = elapsed >= TRACEROUTE_COOLDOWN_SEC

    if pending or not cooldown_ok:
        return

    with _queued_lock:
        if node_id in _queued_nodes:
            return
        _queued_nodes.add(node_id)

    with nodes_lock:
        if node_id in nodes:
            nodes[node_id].traceroute_pending = True

    traceroute_queue.put(node_id)


def periodic_traceroute_thread():
    """
    Barrido de respaldo: cada TRACEROUTE_PERIODIC_SEC evalua TODOS los
    nodos conocidos (no solo los recien escuchados). Sirve de red de
    contencion para nodos silenciosos que igual siguen relayando trafico
    ajeno (y por eso los conocemos, pero no nos hablan directo).
    """
    time.sleep(TRACEROUTE_PERIODIC_SEC)
    while True:
        with nodes_lock:
            todos = [nid for nid in nodes.keys() if nid != ROOT_ID]
        log.info(f"Ciclo periodico de traceroute: {len(todos)} nodos conocidos")
        for nid in todos:
            maybe_schedule_traceroute(nid)
        time.sleep(TRACEROUTE_PERIODIC_SEC)


# =============================================================================
#                       HANDLERS MESHTASTIC
# =============================================================================

def on_receive(packet: dict, interface):
    global last_packet_ts

    with state_lock:
        last_packet_ts = now()

    from_id = str(packet.get("fromId") or packet.get("from") or "")
    if not from_id or from_id == ROOT_ID:
        return

    decoded = packet.get("decoded") or {}
    portnum = decoded.get("portnum", "")

    # ── Metricas validas para CUALQUIER paquete (no solo posicion) ──────────
    rssi = packet.get("rxRssi")
    snr  = packet.get("rxSnr")
    hop_limit = packet.get("hopLimit")
    hop_start = packet.get("hopStart")
    hl = int(hop_limit) if hop_limit is not None else None
    hs = int(hop_start) if hop_start is not None else None
    hops = None
    if hl is not None:
        hops = (hs if hs is not None else 7) - hl
        if hops < 0:
            hops = None

    update_node(
        from_id,
        rssi = float(rssi) if rssi is not None else None,
        snr  = float(snr)  if snr  is not None else None,
        hops = hops,
    )
    increment_packet_count(from_id)

    # ── Atajo "direct": 0 saltos = vecino directo de la raiz. Asumimos
    # simetria en este caso puntual (adyacencia fisica de RF), a diferencia
    # de rutas multi-hop donde ida/vuelta si pueden diferir de verdad ──────
    if hops == 0:
        update_route(from_id, ROOT_ID, direction="fwd",  hop_index=0, source="direct")
        update_route(from_id, ROOT_ID, direction="back", hop_index=0, source="direct")

    # Cualquier paquete es excusa para evaluar si conviene tracear a from_id
    maybe_schedule_traceroute(from_id)

    # ── Traceroute response: reconstruye AMBAS cadenas (ida y vuelta) ──────
    if portnum == "TRACEROUTE_APP":
        tr = decoded.get("traceroute") or {}
        route_fwd_arr  = [nodeid_to_hex(x) for x in tr.get("route", [])]
        route_back_arr = [nodeid_to_hex(x) for x in tr.get("routeBack", [])]
        target_id = from_id

        chain_fwd = [ROOT_ID] + route_fwd_arr + [target_id]
        log.info(f"Traceroute IDA de {target_id}: cadena={chain_fwd}")
        for i in range(len(chain_fwd) - 1):
            parent, child = chain_fwd[i], chain_fwd[i + 1]
            update_node(child)   # puede ser un relay que nunca nos hablo directo
            update_route(child, parent, direction="fwd", hop_index=i, source="traceroute")

        # routeBack puede venir vacio si el firmware no lo reporto (versiones
        # viejas) — en ese caso no inventamos nada, simplemente no hay dato
        # de vuelta para esa traza puntual.
        if tr.get("routeBack") is not None:
            chain_back = [target_id] + route_back_arr + [ROOT_ID]
            log.info(f"Traceroute VUELTA de {target_id}: cadena={chain_back}")
            n = len(chain_back) - 1
            for i in range(n):
                sender, nxt = chain_back[i], chain_back[i + 1]
                update_node(sender)
                # OJO: chain_back esta ordenada destino->...->raiz, o sea "i"
                # cuenta pasos DESDE EL DESTINO, no desde la raiz. Si guardara
                # hop_index=i directo, el destino (i=0) siempre quedaria con
                # hop_index=0 sin importar cuantos saltos reales tenga — eso
                # era el bug: todo nodo traceroute-eado terminaba pegado a la
                # primera columna en la direccion "vuelta". Se invierte para
                # que hop_index signifique lo mismo que en "ida": distancia a
                # la raiz, 0 = adyacente a la raiz.
                hop_desde_raiz = n - 1 - i
                update_route(sender, nxt, direction="back", hop_index=hop_desde_raiz, source="traceroute")

        socketio.emit("tree_update", {
            "status": get_status(), "nodes": serialize_nodes(), "routes": serialize_routes(),
        })
        return

    # ── NodeInfo — nombre y rol ──────────────────────────────────────────────
    if portnum == "NODEINFO_APP":
        user = decoded.get("user") or {}
        update_node(
            from_id,
            short_name = user.get("shortName") or "",
            long_name  = user.get("longName")  or "",
            role       = str(user.get("role") or ""),
        )
        return

    # ── Position — GPS. No hay mapa, pero se guarda para el CSV/survey ─────
    if portnum == "POSITION_APP":
        pos = decoded.get("position") or {}
        lat_i = pos.get("latitudeI")
        lon_i = pos.get("longitudeI")
        alt   = pos.get("altitude")
        lat = (lat_i * 1e-7) if lat_i else None
        lon = (lon_i * 1e-7) if lon_i else None
        update_node(
            from_id,
            lat = lat, lon = lon,
            alt = float(alt) if alt is not None else None,
            position_ts = now(),
        )
        return

    # ── Telemetry — uptime, bateria, voltaje, uso de canal ──────────────────
    if portnum == "TELEMETRY_APP":
        tel = decoded.get("telemetry") or {}
        dm  = tel.get("deviceMetrics") or {}
        if dm:
            update_node(
                from_id,
                uptime_sec    = dm.get("uptimeSeconds"),
                battery_level = dm.get("batteryLevel"),
                voltage       = dm.get("voltage"),
                channel_util  = dm.get("channelUtilization"),
                air_util_tx   = dm.get("airUtilTx"),
                telemetry_ts  = now(),
            )
        return


def on_connection_changed(is_connected: bool):
    global connected
    with state_lock:
        connected = is_connected


# =============================================================================
#                        HILO MESHTASTIC
# =============================================================================

def meshtastic_thread():
    global iface_ref, last_error, ROOT_ID

    while True:
        try:
            on_connection_changed(False)
            with state_lock:
                last_error = ""

            log.info(f"Conectando a {SERIAL_PORT}...")
            iface = meshtastic.serial_interface.SerialInterface(devPath=SERIAL_PORT, debugOut=False)

            with iface_lock:
                iface_ref = iface

            # Identificar la raiz: el propio nodo conectado
            my_num = iface.myInfo.my_node_num
            ROOT_ID = f"!{my_num:08x}"

            my_rec  = (iface.nodesByNum or {}).get(my_num, {})
            my_user = my_rec.get("user") or {}
            update_node(
                ROOT_ID,
                short_name = my_user.get("shortName") or "root",
                long_name  = my_user.get("longName")  or "",
                role       = str(my_user.get("role") or ""),
                is_root    = True,
            )

            on_connection_changed(True)
            log.info(f"Conectado a Meshtastic. Raiz del arbol: {ROOT_ID}")

            # Cargar datos iniciales desde la base de nodos local del dispositivo
            try:
                nodes_db = iface.nodesByNum or {}
                for num, rec in nodes_db.items():
                    nid = f"!{num:08x}"
                    if nid == ROOT_ID:
                        continue
                    user = rec.get("user") or {}
                    update_node(
                        nid,
                        short_name = user.get("shortName") or "",
                        long_name  = user.get("longName")  or "",
                        role       = str(user.get("role") or ""),
                    )
                log.info(f"Cargados {len(nodes_db)} nodos desde nodesByNum")
            except Exception as e:
                log.warning(f"Error cargando nodesByNum: {e}")

            try:
                pub.unsubscribe(on_receive, "meshtastic.receive")
            except Exception:
                pass
            pub.subscribe(on_receive, "meshtastic.receive")

            while True:
                prune_nodes()
                time.sleep(5)

        except Exception as e:
            on_connection_changed(False)
            with iface_lock:
                iface_ref = None
            with state_lock:
                last_error = str(e)
            log.error(f"Error Meshtastic: {e}. Reintentando en 5s...")
            time.sleep(5)


# =============================================================================
#                        BACKUP DE ESTADO PERIODICO
# =============================================================================

def save_state_backup():
    try:
        with nodes_lock:
            nodes_data = [
                {
                    "node_id": e.node_id, "short_name": e.short_name, "long_name": e.long_name,
                    "role": e.role, "last_seen": e.last_seen, "rssi": e.rssi, "snr": e.snr,
                    "hops": e.hops, "is_root": e.is_root, "packet_count": e.packet_count,
                    "uptime_sec": e.uptime_sec, "battery_level": e.battery_level,
                    "voltage": e.voltage, "channel_util": e.channel_util, "air_util_tx": e.air_util_tx,
                    "lat": e.lat, "lon": e.lon, "alt": e.alt,
                }
                for e in nodes.values()
            ]
        with routes_lock:
            routes_data = [
                {
                    "node_id": r.node_id, "next_hop": r.next_hop, "direction": r.direction,
                    "hop_index": r.hop_index, "timestamp": r.timestamp, "source": r.source,
                }
                for r in routes.values()
            ]
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            json.dump({"saved_at": now(), "nodes": nodes_data, "routes": routes_data}, f, ensure_ascii=False, indent=2)
        log.info(f"Backup guardado: {len(nodes_data)} nodos, {len(routes_data)} rutas")
    except Exception as e:
        log.error(f"Error guardando backup: {e}")


def load_state_backup():
    if not os.path.exists(BACKUP_FILE):
        log.info("Sin backup previo, arrancando limpio")
        return
    try:
        with open(BACKUP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        saved_at = data.get("saved_at", 0)
        if (now() - saved_at) > PRUNE_AFTER_SEC:
            log.info("Backup demasiado viejo, ignorando")
            return

        with nodes_lock:
            for e in data.get("nodes", []):
                nid = e.get("node_id", "")
                if not nid:
                    continue
                entry = NodeEntry(node_id=nid)
                entry.short_name    = e.get("short_name") or ""
                entry.long_name     = e.get("long_name")  or ""
                entry.role          = e.get("role")       or ""
                entry.rssi          = e.get("rssi")
                entry.snr           = e.get("snr")
                entry.hops          = e.get("hops")
                entry.is_root       = bool(e.get("is_root"))
                entry.packet_count  = e.get("packet_count") or 0
                entry.last_seen     = e.get("last_seen") or now()
                entry.uptime_sec    = e.get("uptime_sec")
                entry.battery_level = e.get("battery_level")
                entry.voltage       = e.get("voltage")
                entry.channel_util  = e.get("channel_util")
                entry.air_util_tx   = e.get("air_util_tx")
                entry.lat           = e.get("lat")
                entry.lon           = e.get("lon")
                entry.alt           = e.get("alt")
                nodes[nid] = entry

        with routes_lock:
            for r in data.get("routes", []):
                nid = r.get("node_id", "")
                direction = r.get("direction", "fwd")
                if not nid:
                    continue
                routes[(direction, nid)] = RouteEntry(
                    node_id=nid, next_hop=r.get("next_hop", ""), direction=direction,
                    hop_index=r.get("hop_index", 0), timestamp=r.get("timestamp", 0.0),
                    source=r.get("source", "traceroute"),
                )

        log.info(f"Backup restaurado: {len(nodes)} nodos, {len(routes)} rutas")
    except Exception as e:
        log.error(f"Error cargando backup: {e}")


def backup_thread():
    while True:
        time.sleep(BACKUP_INTERVAL_SEC)
        save_state_backup()


# =============================================================================
#                           FLASK + SOCKETIO
# =============================================================================

app = Flask(__name__)
# cors_allowed_origins NO se pasa a proposito: el default de python-socketio
# es "mismo origen solamente" (verificado). El frontend lo sirve esta misma
# app, no hace falta abrir CORS a nadie.
socketio = SocketIO(app, async_mode="threading")


@app.get("/export/nodes.csv")
def export_nodes_csv():
    import io, csv, datetime

    rows = []
    with nodes_lock:
        for e in nodes.values():
            last_seen_str = datetime.datetime.fromtimestamp(e.last_seen).strftime("%Y-%m-%d %H:%M:%S")
            rows.append([
                last_seen_str, _csv_safe(e.node_id), _csv_safe(e.short_name), _csv_safe(e.long_name),
                _csv_safe(e.role), "si" if e.is_root else "no",
                e.rssi if e.rssi is not None else "", e.snr if e.snr is not None else "",
                e.hops if e.hops is not None else "",
                e.lat if e.lat is not None else "", e.lon if e.lon is not None else "",
                e.alt if e.alt is not None else "",
                e.uptime_sec if e.uptime_sec is not None else "",
                e.battery_level if e.battery_level is not None else "",
                e.voltage if e.voltage is not None else "",
                e.channel_util if e.channel_util is not None else "",
                e.air_util_tx if e.air_util_tx is not None else "",
                e.packet_count,
            ])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "last_seen", "node_id", "short_name", "long_name", "role", "es_raiz", "rssi", "snr", "hops",
        "lat", "lon", "alt", "uptime_sec", "battery_level", "voltage", "channel_util", "air_util_tx",
        "packet_count",
    ])
    writer.writerows(rows)

    resp = Response(output.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = 'attachment; filename="nodos_arbol.csv"'
    return resp


@app.get("/export/rutas.csv")
def export_routes_csv():
    import io, csv, datetime

    with nodes_lock:
        name_of = {nid: (e.short_name or e.long_name or nid) for nid, e in nodes.items()}

    rows = []
    with routes_lock:
        for r in routes.values():
            ts = datetime.datetime.fromtimestamp(r.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            rows.append([
                ts, _csv_safe(r.node_id), _csv_safe(name_of.get(r.node_id, "")),
                _csv_safe(r.next_hop), _csv_safe(name_of.get(r.next_hop, "")),
                _csv_safe(r.direction), r.hop_index, _csv_safe(r.source),
            ])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "node_id", "node_name", "next_hop_id", "next_hop_name", "direccion", "hop_index", "origen"])
    writer.writerows(rows)

    resp = Response(output.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = 'attachment; filename="rutas_arbol.csv"'
    return resp


@app.get("/vendor/<path:filename>")
def serve_vendor(filename):
    """
    Sirve d3.min.js y socket.io.min.js vendorizados localmente en ./vendor/
    para que la app funcione sin salida a internet. Nada de CDN.
    """
    safe_name = os.path.basename(filename)   # evita path traversal
    vendor_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
    full_path = os.path.join(vendor_dir, safe_name)
    if not os.path.exists(full_path):
        return Response(f"Falta {safe_name} en ./vendor/ — ver instrucciones de instalacion.", status=404)
    mimetype = "application/javascript" if safe_name.endswith(".js") else "application/octet-stream"
    with open(full_path, "rb") as f:
        return Response(f.read(), mimetype=mimetype)


@app.get("/logo")
def serve_logo():
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo_mesharg.png")
    if os.path.exists(logo_path):
        with open(logo_path, "rb") as f:
            return Response(f.read(), mimetype="image/png")
    return Response("", status=404)


@app.get("/api/tree")
def api_tree():
    return jsonify({"status": get_status(), "nodes": serialize_nodes(), "routes": serialize_routes()})


@app.get("/")
def index():
    html = f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>arbol-de-nodos</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg:       #0f1117;
      --surface:  #181c25;
      --border:   #252a38;
      --text:     #d4d8e8;
      --muted:    #5a6080;
      --accent:   #3b82f6;
      --root:     #22c55e;
      --router:   #ef4444;
      --base:     #facc15;
      --pending-ring: #a78bfa;
      --ok:       #22c55e;
      --bad:      #ef4444;
      --route-fwd:  #38bdf8;
      --route-back: #fb923c;
      --mono:     "JetBrains Mono", "Fira Code", ui-monospace, monospace;
      --sans:     "Inter", system-ui, sans-serif;
    }}

    html, body {{ height: 100%; background: var(--bg); color: var(--text);
                  font-family: var(--sans); font-size: 15px; overflow: hidden; }}

    .wrap {{ display: flex; height: 100%; }}

    #tree-wrap {{ flex: 1; position: relative; overflow: hidden; cursor: grab; }}
    #tree-wrap:active {{ cursor: grabbing; }}
    #tree-svg  {{ width: 100%; height: 100%; display: block; }}

    .link-fwd  {{ stroke: var(--route-fwd);  stroke-width: 1.8px; opacity: .85; }}
    .link-back {{ stroke: var(--route-back); stroke-width: 1.8px; opacity: .85; stroke-dasharray: 4 3; }}
    .link-fwd.highlight, .link-back.highlight {{ stroke-width: 4.5px; opacity: 1; }}
    .link-fwd.dimmed, .link-back.dimmed {{ opacity: .1; }}

    .node circle.main {{ stroke: var(--bg); stroke-width: 2px; }}
    .node circle.pending-ring {{ fill: none; stroke: var(--pending-ring); stroke-width: 1.6px; stroke-dasharray: 3 2; }}
    .node text   {{ fill: var(--text); font-size: 11px; font-family: var(--sans); }}
    .node text.root-label {{ font-weight: 700; fill: #fff; font-size: 12px; }}
    .node.highlight circle.main {{ stroke: var(--accent); stroke-width: 3px; }}
    .node.dimmed {{ opacity: .2; }}

    #tooltip {{
      position: absolute; pointer-events: none; z-index: 10;
      background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
      padding: 10px 12px; font-size: 12px; line-height: 1.6; color: var(--text);
      box-shadow: 0 8px 24px rgba(0,0,0,.4); display: none; max-width: 280px;
      font-family: var(--mono);
    }}
    #tooltip b {{ color: #fff; }}

    /* ── SIDEBAR ── */
    #side {{
      width: 320px; background: var(--surface); border-left: 1px solid var(--border);
      display: flex; flex-direction: column; overflow: hidden;
    }}
    .side-header {{ padding: 16px 16px 12px; border-bottom: 1px solid var(--border); flex-shrink: 0; }}
    .side-title  {{ font-size: 17px; font-weight: 700; letter-spacing: .03em; color: #fff; margin-bottom: 4px; }}

    .pill {{ display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11px;
             font-weight: 600; background: var(--border); color: var(--muted); }}
    .pill.ok  {{ background: rgba(34,197,94,.15);  color: var(--ok);  }}
    .pill.bad {{ background: rgba(239,68,68,.15);  color: var(--bad); }}

    .stats-bar {{ display: flex; border-bottom: 1px solid var(--border); flex-shrink: 0; }}
    .stat-box  {{ flex: 1; text-align: center; padding: 10px 4px; border-right: 1px solid var(--border); }}
    .stat-box:last-child {{ border-right: none; }}
    .stat-num  {{ font-size: 18px; font-weight: 700; color: #fff; font-family: var(--mono); }}
    .stat-lbl  {{ font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin-top: 2px; }}

    .legend {{ display: flex; flex-direction: column; gap: 8px; padding: 10px 16px;
               border-bottom: 1px solid var(--border); flex-shrink: 0; font-size: 11px; color: var(--muted); }}
    .legend-row {{ display: flex; align-items: center; gap: 10px; }}
    .swatch {{ width: 18px; height: 0; border-top-width: 2.5px; border-top-style: solid; flex-shrink: 0; }}
    .swatch.fwd  {{ border-color: var(--route-fwd); }}
    .swatch.back {{ border-color: var(--route-back); border-top-style: dashed; }}
    .dl {{ font-size: 11px; color: var(--muted); text-decoration: none; border: 1px solid var(--border);
           border-radius: 4px; padding: 2px 8px; white-space: nowrap; transition: color .15s, border-color .15s; }}
    .dl:hover {{ color: var(--text); border-color: var(--text); }}
    #fit-all-btn:hover {{ border-color: var(--text); color: #fff; }}
    #node-search:focus {{ border-color: var(--accent); }}

    .tsb-item {{ display: flex; flex-direction: column; gap: 4px; }}
    .tsb-label {{ font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
    .tsb-value {{ font-size: 15px; font-weight: 700; color: #fff; font-family: var(--mono); }}
    .tsb-hist-bar {{ background: var(--accent); border-radius: 1px; }}
    .tsb-pie-legend div {{ display: flex; align-items: center; gap: 4px; white-space: nowrap; }}
    .tsb-pie-legend .sw {{ width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }}

    .list-label {{ padding: 10px 16px 6px; font-size: 11px; color: var(--muted); text-transform: uppercase;
                   letter-spacing: .05em; flex-shrink: 0; }}
    #list {{ flex: 1; overflow-y: auto; padding: 0 8px 12px; }}

    .node-row {{ display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; font-size: 12px; cursor: pointer; }}
    .node-row:hover {{ background: var(--border); }}
    .node-row.selected {{ background: rgba(59,130,246,.18); outline: 1px solid var(--accent); }}
    .node-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--accent); }}
    .node-dot.root   {{ background: var(--root); }}
    .node-dot.router {{ background: var(--router); }}
    .node-dot.base   {{ background: var(--base); }}
    .node-dot.pending-ring {{ outline: 1.5px dashed var(--pending-ring); outline-offset: 1.5px; }}
    .node-name {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .node-meta {{ color: var(--muted); font-size: 10px; font-family: var(--mono); }}
  </style>
</head>
<body>
<div class="wrap">
  <div id="tree-wrap">
    <svg id="tree-svg"></svg>
    <div id="tooltip"></div>
    <div id="orphan-badge" style="position:absolute;left:12px;bottom:12px;font-size:11px;color:var(--muted);
         background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:4px 10px;display:none;"></div>
    <div id="fun-stats" style="position:absolute;left:12px;bottom:44px;font-size:11px;color:var(--text);
         background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:8px 12px;
         display:none;line-height:1.7;max-width:270px;font-family:var(--mono);"></div>
    <button id="fit-all-btn" title="Encuadrar todo el grafo"
            style="position:absolute;right:12px;bottom:12px;font-size:12px;color:var(--text);
                   background:var(--surface);border:1px solid var(--border);border-radius:6px;
                   padding:6px 12px;cursor:pointer;font-family:var(--sans);">&#10021; Encuadrar todo</button>
    <div id="neighbor-panel" style="position:absolute;top:12px;right:12px;width:250px;max-height:60vh;
         overflow-y:auto;font-size:12px;color:var(--text);background:var(--surface);
         border:1px solid var(--border);border-radius:8px;padding:10px 12px;display:none;
         font-family:var(--mono);"></div>
    <div id="top-stats-bar" style="position:absolute;top:12px;left:12px;width:fit-content;max-width:calc(100% - 286px);
         background:var(--surface);border:1px solid var(--border);border-radius:8px;
         padding:8px 16px;display:none;flex-wrap:wrap;gap:22px;align-items:center;z-index:4;"></div>
  </div>

  <div id="side">
    <div class="side-header">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
        <div>
          <div class="side-title">arbol-de-nodos</div>
          <div id="conn" class="pill">conectando...</div>
        </div>
        <img src="/logo" alt="MeshArg"
             style="height:58px;width:58px;object-fit:contain;border-radius:8px;flex-shrink:0"
             onerror="this.style.display='none'"/>
      </div>
    </div>

    <div class="stats-bar">
      <div class="stat-box"><div class="stat-num" id="st-total">0</div><div class="stat-lbl">nodos</div></div>
      <div class="stat-box"><div class="stat-num" id="st-resolved">0</div><div class="stat-lbl">c/ruta</div></div>
      <div class="stat-box"><div class="stat-num" id="st-orphans">0</div><div class="stat-lbl">pendientes</div></div>
      <div class="stat-box"><div class="stat-num" id="st-snr" style="font-size:14px">-</div><div class="stat-lbl">SNR</div></div>
    </div>

    <div class="legend">
      <div class="legend-row">
        <span>raiz: <span id="root-name" style="color:var(--root)">-</span></span>
      </div>
      <div class="legend-row">
        <div class="swatch fwd"></div><span>ruta de ida</span>
        <div class="swatch back" style="margin-left:8px"></div><span>ruta de vuelta</span>
      </div>
      <div class="legend-row">
        <a class="dl" href="/export/rutas.csv" download title="Exportar rutas (ida/vuelta)">&#8595; rutas</a>
        <a class="dl" href="/export/nodes.csv" download title="Exportar nodos">&#8595; nodos</a>
      </div>
      <div class="legend-row" style="flex-wrap:wrap; row-gap:6px;">
        <div class="node-dot root"></div><span>raiz</span>
        <div class="node-dot router" style="margin-left:8px"></div><span>router</span>
        <div class="node-dot base" style="margin-left:8px"></div><span>base</span>
        <div class="node-dot" style="margin-left:8px"></div><span>cliente</span>
      </div>
      <div class="legend-row">
        <div class="node-dot pending-ring"></div><span>re-chequeando ruta (traceroute en curso)</span>
      </div>
    </div>

    <div class="list-label" id="list-count-label">Nodos conocidos</div>
    <div style="padding: 0 16px 8px;">
      <input id="node-search" type="text" placeholder="Buscar nodo..."
             style="width:100%;background:var(--bg);border:1px solid var(--border);border-radius:6px;
                    color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--sans);outline:none;" />
    </div>
    <div id="list"></div>
  </div>
</div>

<script src="/vendor/d3.min.js"></script>
<script src="/vendor/socket.io.min.js"></script>

<script>
function escapeHtml(s) {{
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, c => ({{
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }})[c]);
}}

function fmtAgo(sec) {{
  if (sec === null || sec === undefined) return "-";
  if (sec < 60) return Math.round(sec) + "s";
  if (sec < 3600) return Math.round(sec / 60) + "m";
  return Math.round(sec / 3600) + "h";
}}

function fmtUptime(sec) {{
  if (sec === null || sec === undefined) return "-";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${{h}}h${{m}}m` : `${{m}}m`;
}}

function dotClass(node) {{
  if (node.is_root) return "root";
  const r = (node.role || "").toUpperCase();
  if (r === "ROUTER" || r === "ROUTER_LATE") return "router";
  if (r === "CLIENT_BASE") return "base";
  return "";
}}

function isPending(node) {{
  return !!node.traceroute_pending;
}}

// Nombre largo (corto) para la barra lateral; si falta uno de los dos, cae al que haya.
function displayName(n) {{
  const ln = (n.long_name || "").trim();
  const sn = (n.short_name || "").trim();
  if (ln && sn) return `${{ln}} (${{sn}})`;
  if (ln) return ln;
  if (sn) return sn;
  return n.node_id;
}}

// ─── SVG + zoom/pan ──────────────────────────────────────────────────────────
const svg = d3.select("#tree-svg");
const g   = svg.append("g");
const linksLayer = g.append("g").attr("class", "links-layer");
const nodesLayer = g.append("g").attr("class", "nodes-layer");
const zoom = d3.zoom().scaleExtent([0.2, 3]).on("zoom", (ev) => g.attr("transform", ev.transform));
svg.call(zoom);

let didInitialCenter = false;

// ─── Grafo de fuerzas persistente ───────────────────────────────────────────
// Se mantienen los objetos-nodo entre actualizaciones (mismo objeto JS por
// node_id) para que la simulacion no "salte": solo se les actualizan los
// campos de datos, nunca x/y/vx/vy/fx/fy salvo la raiz (que queda fija).
const nodeById = new Map();
let simNodes = [];
let simLinks = [];
let sim = null;
let lastRoutes = [];
let lastStatus = {{}};
let lastNodesArr = [];

function treeWrapSize() {{
  const el = document.getElementById("tree-wrap");
  return {{ w: el.clientWidth || 800, h: el.clientHeight || 600 }};
}}

function depthMap(nodesArr, routesArr) {{
  const d = new Map();
  for (const n of nodesArr) if (n.is_root) d.set(n.node_id, 0);
  for (const r of routesArr) {{
    const v = r.hop_index + 1;
    const cur = d.get(r.node_id);
    if (cur === undefined || v < cur) d.set(r.node_id, v);
  }}
  return d;
}}

function ensureSim() {{
  if (sim) return sim;
  // Nota: a proposito NO hay force("x", ...). La posicion horizontal se fija
  // "a mano" (node.fx) segun profundidad en mergeData() — eso es lo que
  // garantiza raiz-a-la-izquierda-todo-crece-a-la-derecha de forma estricta,
  // en vez de una tendencia suave que otras fuerzas podian contrarrestar.
  //
  // El eje Y en cambio tira hacia _targetY (el Y actual del padre en la ruta
  // de ida, calculado en mergeData) en vez de un centro fijo comun a todos.
  // Eso agrupa a los hijos de un mismo nodo cerca de su altura, en vez de
  // esparcidos al azar por toda la columna — es lo que evita el zigzag al
  // resaltar un camino: cuantos menos saltos verticales grandes, mas prolija
  // se ve la linea resaltada.
  sim = d3.forceSimulation()
    .force("link", d3.forceLink().id(d => d.node_id).distance(75).strength(0.35))
    .force("charge", d3.forceManyBody().strength(-220))
    .force("collide", d3.forceCollide().radius(d => d.is_root ? 22 : 16))
    .force("y", d3.forceY(d => d._targetY ?? treeWrapSize().h / 2).strength(0.28))
    .alphaDecay(0.03)
    .on("tick", ticked);
  return sim;
}}

// Padre "preferido" para alinear Y: el de la ruta de ida si esta resuelta,
// si no el de vuelta. Es el mismo criterio que ya usa depthMap() para elegir
// entre direcciones, aplicado ahora a la posicion vertical.
function preferredParentId(nodeId) {{
  for (const dir of ["fwd", "back"]) {{
    for (const r of lastRoutes) {{
      if (r.direction === dir && r.node_id === nodeId) return r.next_hop;
    }}
  }}
  return null;
}}

// Mapa de vecinos compartido: nodeId -> Map(neighborId -> Set de direcciones
// "fwd"/"back" que los conectan). Se recalcula UNA VEZ por actualizacion de
// datos (mergeData) y de ahi lo reusan tanto las estadisticas del recuadro
// como el panel de vecinos al hacer hover — recalcularlo en cada mousemove
// seria tirar ciclos a la basura con 100+ nodos.
// ── Histograma de profundidad ────────────────────────────────────────────────
// Usa el mismo depthMap() que ya posiciona el grafo — un nodo sin ruta
// resuelta (huerfano) no tiene profundidad conocida y queda afuera, igual
// que ya queda afuera del dibujo del grafo.
function computeDepthHistogram(nodesArr, routesArr) {{
  const dmap = depthMap(nodesArr, routesArr);
  const counts = new Map();
  for (const n of nodesArr) {{
    if (!dmap.has(n.node_id)) continue;
    const d = dmap.get(n.node_id);
    counts.set(d, (counts.get(d) || 0) + 1);
  }}
  return [...counts.entries()].sort((a, b) => a[0] - b[0]).map(([depth, count]) => ({{ depth, count }}));
}}

// ── Porcentaje de asimetria ida/vuelta ───────────────────────────────────────
// Solo cuentan los nodos que tienen AMBAS direcciones resueltas — si un nodo
// solo tiene ida (o solo vuelta), no hay nada que comparar, no es "simetrico"
// ni "asimetrico", es simplemente desconocido para esta metrica en particular.
// "Asimetrico" = el proximo salto hacia la raiz difiere entre ida y vuelta.
function computeAsymmetryStats(routesArr) {{
  const fwdMap = new Map(routesArr.filter(r => r.direction === "fwd").map(r => [r.node_id, r.next_hop]));
  const backMap = new Map(routesArr.filter(r => r.direction === "back").map(r => [r.node_id, r.next_hop]));
  let both = 0, asym = 0;
  for (const [nodeId, fwdNext] of fwdMap) {{
    if (backMap.has(nodeId)) {{
      both++;
      if (backMap.get(nodeId) !== fwdNext) asym++;
    }}
  }}
  return {{ both, asym, pct: both > 0 ? (asym / both * 100) : null }};
}}

// ── Distribucion de roles ────────────────────────────────────────────────────
// La raiz queda afuera, igual que en el resto de las estadisticas — no es
// parte de la "distribucion del mesh", es el punto de referencia.
function computeRoleDistribution(nodesArr) {{
  const rootId = lastStatus.root_id;
  const counts = new Map();
  for (const n of nodesArr) {{
    if (n.node_id === rootId) continue;
    const r = (n.role || "").toUpperCase();
    counts.set(r, (counts.get(r) || 0) + 1);
  }}
  return counts;
}}

// ── Convex hull geografico (area aproximada en km2) ─────────────────────────
// Mismo enfoque que ya usaban en mapa-mesh: proyeccion simple lat/lon -> km
// usando 111 km/grado de latitud y 111*cos(lat_media) km/grado de longitud
// (razonable para el area chica que cubre una mesh local, no para distancias
// continentales). Se descartan coordenadas (0,0) sin fix real, no puntos
// legitimos en la interseccion del ecuador con el meridiano de Greenwich.
function convexHull(points) {{
  const pts = [...points].sort((a, b) => a.x - b.x || a.y - b.y);
  if (pts.length < 3) return pts;
  const cross = (o, a, b) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  const lower = [];
  for (const p of pts) {{
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
    lower.push(p);
  }}
  const upper = [];
  for (let i = pts.length - 1; i >= 0; i--) {{
    const p = pts[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
    upper.push(p);
  }}
  upper.pop(); lower.pop();
  return lower.concat(upper);
}}

function shoelaceArea(pts) {{
  let area = 0;
  for (let i = 0; i < pts.length; i++) {{
    const j = (i + 1) % pts.length;
    area += pts[i].x * pts[j].y - pts[j].x * pts[i].y;
  }}
  return Math.abs(area) / 2;
}}

function computeHullAreaKm2(nodesArr) {{
  const valid = nodesArr.filter(n => n.lat != null && n.lon != null && (Math.abs(n.lat) > 0.001 || Math.abs(n.lon) > 0.001));
  if (valid.length < 3) return {{ area: null, count: valid.length }};
  const latMean = valid.reduce((s, n) => s + n.lat, 0) / valid.length;
  const kmPerDegLat = 111.0;
  const kmPerDegLon = 111.0 * Math.cos(latMean * Math.PI / 180);
  const projected = valid.map(n => ({{ x: n.lon * kmPerDegLon, y: n.lat * kmPerDegLat }}));
  const hull = convexHull(projected);
  return {{ area: shoelaceArea(hull), count: valid.length }};
}}

// Mapa de vecinos compartido: nodeId -> Map(neighborId -> Set de direcciones
// "fwd"/"back" que los conectan). Se recalcula UNA VEZ por actualizacion de
// datos (mergeData) y de ahi lo reusan tanto las estadisticas del recuadro
// como el panel de vecinos al hacer hover.
function buildNeighborMap(routesArr) {{
  const map = new Map();
  function add(a, b, dir) {{
    if (!map.has(a)) map.set(a, new Map());
    const inner = map.get(a);
    if (!inner.has(b)) inner.set(b, new Set());
    inner.get(b).add(dir);
  }}
  for (const r of routesArr) {{
    add(r.node_id, r.next_hop, r.direction);
    add(r.next_hop, r.node_id, r.direction);
  }}
  return map;
}}
let neighborMapCache = new Map();

function mergeData(nodesArr, routesArr, status) {{
  lastRoutes = routesArr;
  lastStatus = status;
  lastNodesArr = nodesArr;
  neighborMapCache = buildNeighborMap(routesArr);

  const {{ w, h }} = treeWrapSize();
  const dmap = depthMap(nodesArr, routesArr);
  const seen = new Set();
  let orphanCount = 0;

  for (const n of nodesArr) {{
    // Sin ruta resuelta (ni ida ni vuelta) y no es la raiz: no aporta
    // ninguna arista al grafo de topologia, no entra. Sigue estando en
    // la lista de la barra lateral, solo se saca del dibujo.
    if (!n.is_root && !dmap.has(n.node_id)) {{
      orphanCount++;
      continue;
    }}

    seen.add(n.node_id);
    const depth = dmap.get(n.node_id) ?? 0;
    // fx FIJO (no una fuerza blanda): esto es lo que hace la posicion
    // horizontal obligatoria por profundidad, no solo "preferida".
    const fixedX = Math.min(70 + depth * 95, w - 60);

    let obj = nodeById.get(n.node_id);
    if (!obj) {{
      // Nodo nuevo: arranca cerca del Y actual de su padre (si ya lo
      // conocemos) en vez de un salto aleatorio por toda la columna —
      // menos "acomodo caotico" antes de que la fuerza lo asiente.
      const parentId = n.is_root ? null : preferredParentId(n.node_id);
      const parentObj = parentId ? nodeById.get(parentId) : null;
      const baseY = parentObj ? parentObj.y : h / 2;
      obj = {{ ...n }};
      obj.x = fixedX;
      obj.fx = fixedX;
      obj.y = n.is_root ? h / 2 : (baseY + (Math.random() - 0.5) * 40);
      if (n.is_root) obj.fy = h / 2;
      nodeById.set(n.node_id, obj);
    }} else {{
      Object.assign(obj, n);
      obj.fx = fixedX;
      if (n.is_root && obj.fy === undefined) obj.fy = h / 2;
    }}
    obj._depth = depth;
  }}

  // Segunda pasada: ahora que todos los objetos existen, calcular hacia
  // donde tira el force("y") de cada uno (el Y actual de su padre).
  for (const n of nodesArr) {{
    if (n.is_root || !seen.has(n.node_id)) continue;
    const obj = nodeById.get(n.node_id);
    const parentId = preferredParentId(n.node_id);
    const parentObj = parentId ? nodeById.get(parentId) : null;
    obj._targetY = parentObj ? parentObj.y : h / 2;
  }}

  for (const id of [...nodeById.keys()]) {{
    if (!seen.has(id)) nodeById.delete(id);
  }}

  const badge = document.getElementById("orphan-badge");
  if (orphanCount > 0) {{
    badge.style.display = "block";
    badge.textContent = `+${{orphanCount}} nodos sin ruta resuelta (ver lista)`;
  }} else {{
    badge.style.display = "none";
  }}

  renderFunStats(nodesArr, routesArr);
  renderTopStatsBar(nodesArr, routesArr);

  simNodes = [...nodeById.values()];
  simLinks = routesArr
    .filter(r => nodeById.has(r.node_id) && nodeById.has(r.next_hop))
    .map(r => ({{ source: r.node_id, target: r.next_hop, direction: r.direction, key: r.direction + ":" + r.node_id }}));

  const s = ensureSim();
  s.nodes(simNodes);
  s.force("link").links(simLinks);
  s.alpha(0.5).restart();

  updateStatus(status);
  renderList(nodesArr, status);
}}

let linkSel = null, nodeSel = null;

let hoverNodeIds = new Set();
let hoverLinkKeys = new Set();

// Camina la cadena de "next_hop" en una direccion hasta llegar a la raiz
// (o hasta que se corte por falta de dato). Devuelve la secuencia de
// node_id EN EL ORDEN QUE SE CAMINA (desde nodeId hacia la raiz), y si
// llego a cerrar en la raiz o quedo incompleta.
function chainFor(nodeId, direction) {{
  const nextHopMap = new Map(
    lastRoutes.filter(r => r.direction === direction).map(r => [r.node_id, r.next_hop])
  );
  const seq = [nodeId];
  let cur = nodeId;
  for (let i = 0; i < 24; i++) {{
    if (cur === lastStatus.root_id) break;
    const next = nextHopMap.get(cur);
    if (!next || seq.includes(next)) break;   // sin dato para seguir, o ciclo (no deberia pasar)
    seq.push(next);
    cur = next;
  }}
  const complete = seq[seq.length - 1] === lastStatus.root_id;
  return {{ seq, complete }};
}}

function nameOf(nodeId) {{
  const n = nodeById.get(nodeId);
  return n ? (n.short_name || nodeId) : nodeId;
}}

function pathLabel(seq, complete) {{
  const text = seq.map(nameOf).join(" \u2192 ");
  return complete ? text : text + " (incompleta)";
}}

function applyHover(d) {{
  hoverNodeIds = new Set([d.node_id]);
  hoverLinkKeys = new Set();
  for (const dir of ["fwd", "back"]) {{
    const {{ seq }} = chainFor(d.node_id, dir);
    for (const id of seq) hoverNodeIds.add(id);
    for (let i = 0; i < seq.length - 1; i++) hoverLinkKeys.add(dir + ":" + seq[i]);
  }}
  refreshHighlight();
  showNeighborPanel(d);
}}

function clearHover() {{
  hoverNodeIds = new Set();
  hoverLinkKeys = new Set();
  refreshHighlight();
  hideNeighborPanel();
}}

// Panel "quien se conecta con este nodo" — separado del tooltip chico que
// sigue al mouse porque un hub puede tener muchos vecinos y no entra comodo
// ahi. Reusa neighborMapCache (armado una vez por actualizacion de datos,
// no en cada movimiento del mouse).
function showNeighborPanel(d) {{
  const panel = document.getElementById("neighbor-panel");
  const inner = neighborMapCache.get(d.node_id) || new Map();

  if (inner.size === 0) {{
    panel.style.display = "block";
    panel.innerHTML = `<div style="font-weight:700;color:#fff;margin-bottom:4px">${{escapeHtml(d.short_name || d.node_id)}}</div>
      <div style="color:var(--muted)">Sin conexiones resueltas todavia</div>`;
    return;
  }}

  const rows = [...inner.entries()].map(([nid, dirs]) => {{
    const n = nodeById.get(nid);
    const name = n ? displayName(n) : nid;
    const dirLabel = [...dirs].map(dr => dr === "fwd"
      ? `<span style="color:var(--route-fwd)">ida</span>`
      : `<span style="color:var(--route-back)">vuelta</span>`).join(" + ");
    return `<div style="padding:4px 0;border-top:1px solid var(--border)">
      <div>${{escapeHtml(name)}}</div>
      <div style="font-size:10px">${{dirLabel}}</div>
    </div>`;
  }}).join("");

  panel.style.display = "block";
  panel.innerHTML = `
    <div style="font-weight:700;color:#fff">${{escapeHtml(d.short_name || d.node_id)}}</div>
    <div style="color:var(--muted);margin-bottom:2px">conectado con ${{inner.size}} nodo${{inner.size === 1 ? "" : "s"}}:</div>
    ${{rows}}
  `;
}}

function hideNeighborPanel() {{
  document.getElementById("neighbor-panel").style.display = "none";
}}

let pinnedFromSidebar = null;

function centerOn(d) {{
  const {{ w, h }} = treeWrapSize();
  const scale = 1.3;
  const transform = d3.zoomIdentity.translate(w / 2 - d.x * scale, h / 2 - d.y * scale).scale(scale);
  svg.transition().duration(500).call(zoom.transform, transform);
}}

// Click en la barra lateral: mismo resaltado que el hover sobre el grafico,
// mas centrado/zoom hacia el nodo. Si es huerfano (no esta dibujado, sin
// ruta resuelta) no hay nada que centrar — se limpia el resaltado en vez
// de fallar en silencio. Un segundo click sobre la misma fila deselecciona.
function selectFromSidebar(nodeId) {{
  if (pinnedFromSidebar === nodeId) {{
    pinnedFromSidebar = null;
    clearHover();
    renderList(lastNodesArr, lastStatus);
    return;
  }}
  pinnedFromSidebar = nodeId;
  const obj = nodeById.get(nodeId);
  if (!obj) {{
    clearHover();
  }} else {{
    applyHover(obj);
    centerOn(obj);
  }}
  renderList(lastNodesArr, lastStatus);
}}

function refreshHighlight() {{
  const active = hoverNodeIds.size > 0;
  if (linkSel) {{
    linkSel.classed("highlight", d => hoverLinkKeys.has(d.key));
    linkSel.classed("dimmed", d => active && !hoverLinkKeys.has(d.key));
  }}
  if (nodeSel) {{
    nodeSel.classed("highlight", d => hoverNodeIds.has(d.node_id));
    nodeSel.classed("dimmed", d => active && !hoverNodeIds.has(d.node_id));
  }}
}}

function ticked() {{
  // Clamp duro: ningun nodo puede quedar mas a la izquierda que el minimo
  // de su columna de profundidad. El forceX ya empuja hacia ahi, pero es
  // debil frente a la carga/enlaces — esto es lo que convierte "tiende a"
  // en "no puede no". Se cancela la velocidad negativa para no generar
  // temblor por el choque entre el clamp y la simulacion.
  const {{ w }} = treeWrapSize();
  for (const d of simNodes) {{
    const minX = d.is_root ? 70 : Math.min(70 + (d._depth ?? 5) * 95, w - 60);
    if (d.x < minX) {{
      d.x = minX;
      if (d.vx < 0) d.vx = 0;
    }}
  }}

  linkSel = linksLayer.selectAll("line").data(simLinks, d => d.key);
  linkSel.exit().remove();
  const linkEnter = linkSel.enter().append("line")
    .attr("class", d => d.direction === "back" ? "link-back" : "link-fwd");
  linkSel = linkEnter.merge(linkSel)
    .attr("class", d => d.direction === "back" ? "link-back" : "link-fwd")
    .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x).attr("y2", d => d.target.y);

  nodeSel = nodesLayer.selectAll("g.node").data(simNodes, d => d.node_id);
  nodeSel.exit().remove();
  const nodeEnter = nodeSel.enter().append("g").attr("class", "node")
    .on("mousemove", (ev, d) => {{ showTooltip(ev, d); applyHover(d); }})
    .on("mouseleave", () => {{ hideTooltip(); clearHover(); }});
  nodeEnter.append("circle").attr("class", "main");
  nodeEnter.append("circle").attr("class", "pending-ring");
  nodeEnter.append("text").attr("text-anchor", "middle");

  const merged = nodeEnter.merge(nodeSel);
  merged.attr("class", "node")
        .attr("transform", d => `translate(${{d.x}},${{d.y}})`);

  merged.select("circle.main")
    .attr("r", d => d.is_root ? 16 : 10)
    .attr("fill", d => {{
      const cls = dotClass(d);
      if (cls === "root") return "var(--root)";
      if (cls === "router") return "var(--router)";
      if (cls === "base") return "var(--base)";
      return "var(--accent)";
    }});

  // Anillo punteado independiente del color de rol: "estoy re-chequeando
  // este nodo ahora" es un ESTADO transitorio, no una propiedad del nodo,
  // asi que no deberia competir por el mismo canal de color que el rol.
  merged.select("circle.pending-ring")
    .attr("r", d => (d.is_root ? 16 : 10) + 4)
    .style("display", d => isPending(d) ? null : "none");

  merged.select("text")
    .attr("class", d => d.is_root ? "root-label" : "")
    .attr("dy", d => d.is_root ? "-1.6em" : "1.9em")
    .text(d => (d.short_name || d.node_id).slice(0, 14));

  refreshHighlight();

  if (!didInitialCenter && simNodes.length) {{
    didInitialCenter = true;
    const {{ w }} = treeWrapSize();
    svg.call(zoom.transform, d3.zoomIdentity.translate(40, 0).scale(1));
  }}
}}

const tooltip = document.getElementById("tooltip");
function showTooltip(ev, d) {{
  const wrap = document.getElementById("tree-wrap").getBoundingClientRect();
  tooltip.style.display = "block";
  tooltip.style.left = (ev.clientX - wrap.left + 14) + "px";
  tooltip.style.top  = (ev.clientY - wrap.top + 14) + "px";

  const fwd  = chainFor(d.node_id, "fwd");
  const back = chainFor(d.node_id, "back");
  // "ida" se muestra raiz -> destino (se camina al reves de como se resuelve)
  const fwdLabel  = pathLabel([...fwd.seq].reverse(), fwd.complete);
  const backLabel = pathLabel(back.seq, back.complete);

  tooltip.innerHTML = `
    <div><b>${{escapeHtml(d.short_name || d.node_id)}}</b></div>
    <div>${{escapeHtml(d.long_name || "")}}</div>
    <div>id: ${{escapeHtml(d.node_id)}}</div>
    <div>rol: ${{escapeHtml(d.role || "desconocido")}}</div>
    <div>rssi/snr: ${{d.rssi ?? "-"}} / ${{d.snr ?? "-"}}</div>
    <div>uptime: ${{fmtUptime(d.uptime_sec)}} | bat: ${{d.battery_level ?? "-"}}%</div>
    <div>gps: ${{d.lat != null ? d.lat.toFixed(5) : "-"}}, ${{d.lon != null ? d.lon.toFixed(5) : "-"}}</div>
    <div>visto hace: ${{fmtAgo((Date.now()/1000) - d.last_seen)}}</div>
    <div style="margin-top:6px;color:var(--route-fwd)">ida: ${{escapeHtml(fwdLabel)}}</div>
    <div style="color:var(--route-back)">vuelta: ${{escapeHtml(backLabel)}}</div>
  `;
}}
function hideTooltip() {{ tooltip.style.display = "none"; }}


// ─── Sidebar ─────────────────────────────────────────────────────────────────
function updateStatus(s) {{
  const conn = document.getElementById("conn");
  if (s.connected) {{
    conn.className = "pill ok";
    conn.textContent = "conectado";
  }} else {{
    conn.className = "pill bad";
    conn.textContent = s.last_error ? ("error: " + s.last_error) : "sin conexion";
  }}
  document.getElementById("st-total").textContent    = s.total_nodes ?? 0;
  document.getElementById("st-resolved").textContent = s.resolved_routes ?? 0;
  document.getElementById("st-orphans").textContent  = s.orphans ?? 0;
  document.getElementById("st-snr").textContent      = (s.avg_snr ?? "-") + " dB";
}}

let searchQuery = "";

// Estadisticas curiosas del recuadro inferior izquierdo. "Conexiones" =
// vecinos DISTINTOS (deduplicado ida/vuelta), no cantidad de rutas — un
// nodo con ida y vuelta confirmadas hacia el mismo vecino tiene 1 conexion,
// no 2. La raiz queda afuera de los 4 rankings: no es un "miembro" del
// mesh contra el que tenga sentido comparar, es el punto de referencia fijo.
//
// "Menos conectado" NO es simplemente el minimo grado: en una malla con
// forma de arbol, CUALQUIER final de rama (o un CLIENT_MUTE, que por
// definicion no re-transmite) tiene grado 1 — hay decenas empatadas y el
// resultado no dice nada. En vez de listar roles a mano, se filtra por
// topologia: un nodo "cuenta" para este ranking solo si aparece como
// next_hop de ALGUN OTRO nodo (o sea, si alguien lo usa para llegar a la
// raiz). Un final de malla o un CLIENT_MUTE nunca aparecen ahi, quedan
// afuera solos, sin necesidad de chequear el rol explicitamente.
function computeFunStats(nodesArr, routesArr) {{
  const rootId = lastStatus.root_id;
  const usedAsNextHop = new Set();
  for (const r of routesArr) usedAsNextHop.add(r.next_hop);

  const candidates = nodesArr.filter(n => n.node_id !== rootId);
  if (candidates.length === 0) return null;

  let maxConn = null, minConn = null, maxPkt = null, mostSilent = null;
  for (const n of candidates) {{
    const deg = (neighborMapCache.get(n.node_id) || new Map()).size;
    if (maxConn === null || deg > maxConn.deg) maxConn = {{ n, deg }};
    // Solo entran al ranking de "menos conectado" los nodos que relayan
    // para alguien mas — descarta finales de malla / CLIENT_MUTE.
    if (deg > 0 && usedAsNextHop.has(n.node_id) && (minConn === null || deg < minConn.deg)) {{
      minConn = {{ n, deg }};
    }}

    const pc = n.packet_count || 0;
    if (maxPkt === null || pc > maxPkt.pc) maxPkt = {{ n, pc }};

    if (mostSilent === null || n.last_seen < mostSilent.n.last_seen) mostSilent = {{ n }};
  }}
  return {{ maxConn, minConn, maxPkt, mostSilent }};
}}

function renderFunStats(nodesArr, routesArr) {{
  const box = document.getElementById("fun-stats");
  const s = computeFunStats(nodesArr, routesArr);
  if (!s) {{ box.style.display = "none"; return; }}
  box.style.display = "block";
  const row = (label, val) => `<div><span style="color:var(--muted)">${{label}}:</span> ${{val}}</div>`;
  box.innerHTML =
    row("Más conectado", s.maxConn ? `${{escapeHtml(displayName(s.maxConn.n))}} (${{s.maxConn.deg}})` : "-") +
    row("Menos conectado:", s.minConn ? `${{escapeHtml(displayName(s.minConn.n))}} (${{s.minConn.deg}})` : "sin datos aun") +
    row("Más paquetes", s.maxPkt ? `${{escapeHtml(displayName(s.maxPkt.n))}} (${{s.maxPkt.pc}})` : "-") +
    row("Más silencioso", s.mostSilent ? `${{escapeHtml(displayName(s.mostSilent.n))}} (${{fmtAgo((Date.now()/1000) - s.mostSilent.n.last_seen)}})` : "-");
}}

const ROLE_PIE_COLORS = {{
  "ROUTER":        "var(--router)",
  "ROUTER_LATE":   "var(--router)",
  "CLIENT_BASE":   "var(--base)",
  "CLIENT_MUTE":   "#94a3b8",
  "CLIENT_HIDDEN": "#c084fc",
  "":              "var(--accent)",
}};
const ROLE_PIE_LABELS = {{
  "ROUTER": "router", "ROUTER_LATE": "router_late", "CLIENT_BASE": "client_base",
  "CLIENT_MUTE": "client_mute", "CLIENT_HIDDEN": "client_hidden", "": "cliente / sin dato",
}};

function renderTopStatsBar(nodesArr, routesArr) {{
  const bar = document.getElementById("top-stats-bar");
  const candidates = nodesArr.filter(n => n.node_id !== lastStatus.root_id);
  if (candidates.length === 0) {{ bar.style.display = "none"; return; }}
  bar.style.display = "flex";

  // 1) Histograma de profundidad
  const hist = computeDepthHistogram(nodesArr, routesArr);
  const maxCount = Math.max(1, ...hist.map(h => h.count));
  const histHtml = hist.map(h => `
    <div class="tsb-hist-bar" title="${{h.depth}} saltos: ${{h.count}} nodos"
         style="width:7px;height:${{Math.max(3, Math.round((h.count / maxCount) * 32))}}px;"></div>
  `).join("");

  // 2) Asimetria ida/vuelta
  const asym = computeAsymmetryStats(routesArr);
  const asymText = asym.pct === null ? "sin datos" : `${{asym.pct.toFixed(0)}}%`;
  const asymCaption = asym.pct === null ? "ningun nodo con ambas direcciones" : `${{asym.asym}} de ${{asym.both}} nodos`;

  // 3) Distribucion de roles (torta via conic-gradient)
  const roleCounts = computeRoleDistribution(nodesArr);
  const totalRoles = [...roleCounts.values()].reduce((a, b) => a + b, 0);
  let pieCss = "var(--border)", legendHtml = `<div style="color:var(--muted)">sin datos</div>`;
  if (totalRoles > 0) {{
    let acc = 0;
    const stops = [];
    const legendRows = [];
    for (const [role, count] of [...roleCounts.entries()].sort((a, b) => b[1] - a[1])) {{
      if (count === 0) continue;
      const color = ROLE_PIE_COLORS[role] ?? "var(--accent)";
      const label = ROLE_PIE_LABELS[role] ?? role.toLowerCase();
      const start = (acc / totalRoles) * 360;
      acc += count;
      const end = (acc / totalRoles) * 360;
      stops.push(`${{color}} ${{start.toFixed(1)}}deg ${{end.toFixed(1)}}deg`);
      legendRows.push(`<div><span class="sw" style="background:${{color}}"></span>${{escapeHtml(label)}} (${{count}})</div>`);
    }}
    pieCss = `conic-gradient(${{stops.join(", ")}})`;
    legendHtml = legendRows.join("");
  }}

  // 4) Superficie geografica (convex hull aproximado)
  const hull = computeHullAreaKm2(nodesArr);
  const hullText = hull.area === null ? "sin datos" : `~${{hull.area.toFixed(0)}} km²`;
  const hullCaption = `${{hull.count}} nodos con GPS`;

  bar.innerHTML = `
    <div class="tsb-item">
      <div class="tsb-label">roles</div>
      <div style="display:flex;align-items:center;gap:8px;">
        <div style="width:34px;height:34px;border-radius:50%;flex-shrink:0;background:${{pieCss}}"></div>
        <div class="tsb-pie-legend">${{legendHtml}}</div>
      </div>
    </div>
    <div class="tsb-item">
      <div class="tsb-label">profundidad</div>
      <div style="display:flex;align-items:flex-end;gap:2px;height:34px;">${{histHtml}}</div>
    </div>
    <div class="tsb-item">
      <div class="tsb-label">asimetria ida/vuelta</div>
      <div class="tsb-value">${{asymText}}</div>
      <div style="font-size:10px;color:var(--muted)">${{asymCaption}}</div>
    </div>
    <div class="tsb-item">
      <div class="tsb-label">convex hull</div>
      <div class="tsb-value">${{hullText}}</div>
      <div style="font-size:10px;color:var(--muted)">${{hullCaption}}</div>
    </div>
  `;
}}


function renderList(nodesArr, status) {{
  const list = document.getElementById("list");
  list.innerHTML = "";

  const rootNode = nodesArr.find(n => n.node_id === status.root_id);
  if (rootNode) document.getElementById("root-name").textContent = displayName(rootNode);

  const q = searchQuery.trim().toLowerCase();
  const filtered = q
    ? nodesArr.filter(n => displayName(n).toLowerCase().includes(q) || n.node_id.toLowerCase().includes(q))
    : nodesArr;

  const label = document.getElementById("list-count-label");
  if (label) {{
    label.textContent = q ? `Nodos conocidos (${{filtered.length}} de ${{nodesArr.length}})` : "Nodos conocidos";
  }}

  const sorted = [...filtered].sort((a, b) => (b.is_root - a.is_root) || (b.last_seen - a.last_seen));
  for (const n of sorted) {{
    const row = document.createElement("div");
    row.className = "node-row" + (n.node_id === pinnedFromSidebar ? " selected" : "");
    row.innerHTML = `
      <div class="node-dot ${{dotClass(n)}}${{isPending(n) ? ' pending-ring' : ''}}"></div>
      <div class="node-name">${{escapeHtml(displayName(n))}}</div>
      <div class="node-meta">${{fmtAgo((Date.now()/1000) - n.last_seen)}}</div>
    `;
    row.addEventListener("click", () => selectFromSidebar(n.node_id));
    list.appendChild(row);
  }}
}}

function fitAll() {{
  if (simNodes.length === 0) return;
  const {{ w, h }} = treeWrapSize();
  const xs = simNodes.map(d => d.x), ys = simNodes.map(d => d.y);
  const pad = 50;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
  const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  const scale = Math.min(3, Math.max(0.2, Math.min(w / (maxX - minX || 1), h / (maxY - minY || 1))));
  const midX = (minX + maxX) / 2, midY = (minY + maxY) / 2;
  const transform = d3.zoomIdentity.translate(w / 2 - midX * scale, h / 2 - midY * scale).scale(scale);
  svg.transition().duration(500).call(zoom.transform, transform);
}}
document.getElementById("fit-all-btn").addEventListener("click", fitAll);
document.getElementById("node-search").addEventListener("input", (ev) => {{
  searchQuery = ev.target.value;
  renderList(lastNodesArr, lastStatus);
}});

function applyPayload(payload) {{
  const status = payload.status || {{}};
  const nodesArr = payload.nodes || [];
  const routesArr = payload.routes || [];
  mergeData(nodesArr, routesArr, status);
}}

const socket = io();
socket.on("tree_update", applyPayload);

async function poll() {{
  try {{
    const r = await fetch("/api/tree", {{ cache: "no-store" }});
    applyPayload(await r.json());
  }} catch (e) {{
    console.warn("poll error", e);
  }}
}}
poll();
setInterval(poll, {int(POLL_REFRESH_SEC * 1000)});

window.addEventListener("resize", () => {{ if (sim) sim.alpha(0.2).restart(); }});
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


# =============================================================================
#                                   MAIN
# =============================================================================

def check_vendor_assets():
    vendor_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
    required = ["d3.min.js", "socket.io.min.js"]
    faltantes = [f for f in required if not os.path.exists(os.path.join(vendor_dir, f))]
    if faltantes:
        log.warning(
            f"Faltan en ./vendor/: {', '.join(faltantes)} — el arbol NO va a renderizar "
            f"en el navegador (necesita internet para pedirlos si no estan localmente)."
        )
    else:
        log.info("Assets vendorizados (d3, socket.io) presentes: la UI funciona sin internet.")


def main():
    check_vendor_assets()
    load_state_backup()

    threading.Thread(target=meshtastic_thread, daemon=True, name="meshtastic").start()
    threading.Thread(target=traceroute_worker, daemon=True, name="traceroute-worker").start()
    threading.Thread(target=periodic_traceroute_thread, daemon=True, name="traceroute-periodic").start()
    threading.Thread(target=backup_thread, daemon=True, name="backup").start()

    log.info(f"Servidor en https://{BIND_HOST}:{BIND_PORT} (solo localhost)")

    ssl_cert = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssl", "arbol.pem")
    ssl_key  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssl", "arbol.key")
    if os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        ssl_ctx = (ssl_cert, ssl_key)
    else:
        log.warning("Certificados SSL no encontrados en ./ssl/, usando adhoc (autofirmado)")
        ssl_ctx = "adhoc"

    socketio.run(app, host=BIND_HOST, port=BIND_PORT, debug=False, use_reloader=False,
                 ssl_context=ssl_ctx, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
