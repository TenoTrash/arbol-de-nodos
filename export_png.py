#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# =============================================================================
# export_png.py — Exportador server-side del mapa a PNG (plano A3 apaisado)
#
# Genera un PNG imprimible en hoja A3 apaisada (420x297 mm) a 300 DPI:
#   - fondo blanco
#   - marco doble "tipo plano de ingenieria" (exterior fino + interior grueso,
#     con marcas de centrado)
#   - mapa de topologia en VERTICAL (raiz arriba), layout determinista por
#     capas de profundidad, con "fit all" dentro del area de dibujo
#   - cajetin (rotulo) abajo a la derecha con: leyenda "Arbol de nodos
#     Meshtastic", fecha/hora, URL del proyecto, region LoRa y todas las
#     estadisticas que ya muestra la exploracion web.
#
# Las funciones compute_layout() y compute_stats() NO dependen de Pillow y se
# pueden testear sin el. El render (render_export_png) importa Pillow de forma
# perezosa para que la app principal siga arrancando aunque Pillow no este
# instalado todavia (devuelve un error claro en ese caso).
#
# Esta pensado para correr 100% offline: no usa recursos externos ni CDN.
# =============================================================================

import io
import math
import os
import datetime

# -----------------------------------------------------------------------------
# Constantes de hoja / impresion
# -----------------------------------------------------------------------------
EXPORT_DPI = 300

SHEET_W_MM = 420.0   # A3 apaisado (ancho x alto)
SHEET_H_MM = 297.0

OUTER_MARGIN_MM = 5.0    # linea exterior (fina)
INNER_MARGIN_MM = 12.0   # linea interior (gruesa, marco del dibujo)

TITLE_BLOCK_W_MM = 172.0
TITLE_BLOCK_PAD_MM = 4.0
TITLE_BLOCK_GAP_MM = 8.0
MAP_PAD_MM = 12.0

# Colores RGB (fondo blanco, lineas/texto negro)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

ROUTE_FWD  = (56, 189, 248)   # celeste (ida)
ROUTE_BACK = (251, 146, 60)   # naranja (vuelta)

# Tintas claras para nodos (mejor para imprimir que colores saturados), con
# borde oscuro y texto negro.
NODE_FILL = {
    "root":   (204, 240, 214),
    "router": (250, 219, 219),
    "base":   (255, 244, 196),
    "client": (210, 225, 250),
}
NODE_BORDER = {
    "root":   (21, 128, 61),
    "router": (185, 28, 28),
    "base":   (202, 138, 4),
    "client": (29, 78, 216),
}

# Labels legibles para la region LoRa (las keys son los nombres del enum
# Config.LoRaConfig.RegionCode de Meshtastic).
LORA_REGION_LABELS = {
    "UNSET": "sin configurar",
    "US": "US 915 MHz",
    "EU_433": "EU 433 MHz",
    "EU_868": "EU 868 MHz",
    "CN": "CN 470 MHz",
    "JP": "JP 920 MHz",
    "ANZ": "ANZ 915 MHz",
    "KR": "KR 920 MHz",
    "TW": "TW 920 MHz",
    "RU": "RU 868 MHz",
    "IN": "IN 865 MHz",
    "NZ_865": "NZ 865 MHz",
    "TH": "TH 920 MHz",
    "LORA_24": "2.4 GHz",
    "UA_433": "UA 433 MHz",
    "UA_868": "UA 868 MHz",
    "MY_433": "MY 433 MHz",
    "MY_919": "MY 919 MHz",
    "SG_923": "SG 923 MHz",
    "PH_433": "PH 433 MHz",
    "PH_868": "PH 868 MHz",
    "PH_915": "PH 915 MHz",
    "ANZ_433": "ANZ 433 MHz",
    "KZ_433": "KZ 433 MHz",
    "KZ_863": "KZ 863 MHz",
    "NP_865": "NP 865 MHz",
    "BR_902": "BR 902 MHz",
    "ITU1_2M": "ITU1 2 MHz",
    "ITU2_2M": "ITU2 2 MHz",
    "EU_866": "EU 866 MHz",
    "EU_874": "EU 874 MHz",
    "EU_917": "EU 917 MHz",
    "EU_N_868": "EU N 868 MHz",
    "ITU3_2M": "ITU3 2 MHz",
}

PROJECT_URL = "https://github.com/TenoTrash/arbol-de-nodos"
PROJECT_TITLE = "Arbol de nodos Meshtastic"


# =============================================================================
#                               HELPERS DE DATOS
# =============================================================================

def _display_name(n):
    ln = (n.get("long_name") or "").strip()
    sn = (n.get("short_name") or "").strip()
    if ln and sn:
        return f"{ln} ({sn})"
    if ln:
        return ln
    if sn:
        return sn
    return n.get("node_id", "")


def _node_label(n):
    """Texto corto que se dibuja DENTRO del nodo (igual que la UI)."""
    if not n:
        return ""
    sn = (n.get("short_name") or "").strip()
    return (sn or n.get("node_id", "")).strip()[:14]


def _node_rect_width(n):
    """Ancho del rectangulo en unidades abstractas (mismo criterio que la UI)."""
    lbl = _node_label(n)
    ln = len(lbl)
    is_root = bool(n.get("is_root"))
    mn = 48 if is_root else 38
    return max(mn, ln * 7 + (18 if is_root else 14))


def _node_height(n):
    return 30 if n.get("is_root") else 24


def _dot_class(n):
    if n.get("is_root"):
        return "root"
    r = (n.get("role") or "").upper()
    if r in ("ROUTER", "ROUTER_LATE"):
        return "router"
    if r == "CLIENT_BASE":
        return "base"
    return "client"


def _depth_map(nodes, routes):
    d = {}
    for n in nodes:
        if n.get("is_root"):
            d[n["node_id"]] = 0
    for r in routes:
        nid = r.get("node_id")
        if not nid:
            continue
        v = int(r.get("hop_index", 0)) + 1
        if nid not in d or v < d[nid]:
            d[nid] = v
    return d


def _preferred_parent(node_id, routes):
    for direction in ("fwd", "back"):
        for r in routes:
            if r.get("direction") == direction and r.get("node_id") == node_id:
                return r.get("next_hop")
    return None


def _build_neighbor_map(routes):
    m = {}

    def add(a, b, direction):
        if a is None or b is None or a == b:
            return
        inner = m.setdefault(a, {})
        inner.setdefault(b, set()).add(direction)

    for r in routes:
        a = r.get("node_id")
        b = r.get("next_hop")
        direction = r.get("direction")
        add(a, b, direction)
        add(b, a, direction)
    return m


# =============================================================================
#                                   LAYOUT
# =============================================================================

def compute_layout(nodes, routes, root_id, target_aspect=None,
                  h_gap=28.0, min_spacing=60.0,
                  min_v_gap=120.0, max_v_gap=240.0):
    """Layout vertical determinista (raiz arriba) por capas de profundidad.

    Devuelve dict {node_id: (x, y)} en unidades abstractas. La raiz queda en
    (0, 0) y la profundidad crece hacia abajo. Los nodos sin ruta resuelta
    (huerfanos) quedan afuera, igual que en el dibujo de la UI.

    target_aspect (ancho/alto del area disponible) se usa para elegir la
    separacion VERTICAL entre capas de forma que el arbol aproveche mejor el
    papel, en vez de quedar como una banda ancha y baja centrada.
    """
    by_id = {n["node_id"]: n for n in nodes}
    depth = _depth_map(nodes, routes)

    parent = {}
    for nid in depth:
        p = _preferred_parent(nid, routes)
        if p and p in depth:
            parent[nid] = p

    layers = {}
    for nid, d in depth.items():
        layers.setdefault(d, []).append(nid)

    x = {}
    if root_id in depth:
        x[root_id] = 0.0

    if not layers:
        return {}

    max_depth = max(layers)

    # Primera pasada: asignar X por capa (misma logica de padre preferido).
    layer_extent = {}   # ancho de la capa (distancia entre primer y ultimo centro)
    for d in range(1, max_depth + 1):
        ids = layers.get(d, [])
        if not ids:
            continue
        # Tentativo: la x del padre "preferido" (mismo criterio que la UI).
        for nid in ids:
            p = parent.get(nid)
            x[nid] = x.get(p, 0.0)
        # Orden estable por x tentativa y luego por label.
        ids.sort(key=lambda nid: (round(x.get(nid, 0.0), 6), _node_label(by_id.get(nid))))

        # Separacion minima: el nodo mas ancho de la capa + un hueco.
        spacing = min_spacing
        for nid in ids:
            spacing = max(spacing, _node_rect_width(by_id.get(nid)) + h_gap)

        n = len(ids)
        for i, nid in enumerate(ids):
            xi = (i - (n - 1) / 2.0) * spacing
            x[nid] = xi
        layer_extent[d] = (n - 1) * spacing

    # Elegir la separacion vertical para acercar el arbol al aspect del area.
    widest = max(layer_extent.values(), default=0.0)
    v_gap = min_v_gap
    if target_aspect and max_depth > 0 and widest > 0:
        v_gap = widest / (target_aspect * max_depth)
        v_gap = max(min_v_gap, min(max_v_gap, v_gap))

    pos = {}
    if root_id in depth:
        pos[root_id] = (0.0, 0.0)
    for nid, d in depth.items():
        if nid != root_id:
            pos[nid] = (x.get(nid, 0.0), d * v_gap)

    return pos


# =============================================================================
#                                 ESTADISTICAS
# =============================================================================

def _depth_histogram(depth):
    counts = {}
    for d in depth.values():
        counts[d] = counts.get(d, 0) + 1
    return [{"depth": d, "count": c} for d, c in sorted(counts.items())]


def _asymmetry(routes):
    fwd = {}
    back = {}
    for r in routes:
        if r.get("direction") == "fwd":
            fwd[r["node_id"]] = r.get("next_hop")
        elif r.get("direction") == "back":
            back[r["node_id"]] = r.get("next_hop")
    both = 0
    asym = 0
    for nid, f in fwd.items():
        if nid in back:
            both += 1
            if back[nid] != f:
                asym += 1
    pct = (asym / both * 100) if both else None
    return {"both": both, "asym": asym, "pct": pct}


def _role_distribution(nodes, root_id):
    counts = {}
    for n in nodes:
        if n["node_id"] == root_id:
            continue
        r = (n.get("role") or "").upper()
        counts[r] = counts.get(r, 0) + 1
    return counts


def _convex_hull(points):
    pts = sorted(points, key=lambda p: (p[0], p[1]))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def _shoelace(points):
    if len(points) < 3:
        return 0.0
    area = 0.0
    n = len(points)
    for i in range(n):
        j = (i + 1) % n
        area += points[i][0] * points[j][1] - points[j][0] * points[i][1]
    return abs(area) / 2.0


def _has_fix(n):
    return (
        n
        and n.get("lat") is not None
        and n.get("lon") is not None
        and (abs(n["lat"]) > 0.001 or abs(n["lon"]) > 0.001)
    )


def _hull_area_km2(nodes):
    valid = [n for n in nodes if _has_fix(n)]
    if len(valid) < 3:
        return {"area": None, "count": len(valid), "hull": None}
    lat_mean = sum(n["lat"] for n in valid) / len(valid)
    km_lat = 111.0
    km_lon = 111.0 * math.cos(math.radians(lat_mean))
    projected = [(n["lon"] * km_lon, n["lat"] * km_lat) for n in valid]
    hull = _convex_hull(projected)
    return {"area": _shoelace(hull), "count": len(valid), "hull": hull}


def _hull_perimeter_km(hull):
    if not hull or len(hull) < 3:
        return None
    per = 0.0
    n = len(hull)
    for i in range(n):
        j = (i + 1) % n
        per += math.hypot(hull[j][0] - hull[i][0], hull[j][1] - hull[i][1])
    return per


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    to_rad = math.radians
    dlat = to_rad(lat2 - lat1)
    dlon = to_rad(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) * math.sin(dlon / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _link_distance_stats(nodes, nbr, by_id):
    shortest = None
    longest = None
    seen = set()
    for nid, neighbors in nbr.items():
        a = by_id.get(nid)
        if not _has_fix(a):
            continue
        for nbid in neighbors:
            key = tuple(sorted([nid, nbid]))
            if key in seen:
                continue
            seen.add(key)
            b = by_id.get(nbid)
            if not _has_fix(b):
                continue
            dist = _haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            entry = {"a": a, "b": b, "dist": dist}
            if shortest is None or dist < shortest["dist"]:
                shortest = entry
            if longest is None or dist > longest["dist"]:
                longest = entry
    return {"shortest": shortest, "longest": longest}


def _fun_stats(nodes, routes, nbr, by_id, root_id):
    used_as_next_hop = set()
    for r in routes:
        nh = r.get("next_hop")
        if nh:
            used_as_next_hop.add(nh)

    candidates = [n for n in nodes if n["node_id"] != root_id]
    if not candidates:
        return None

    max_conn = min_conn = max_pkt = most_silent = None
    for n in candidates:
        deg = len(nbr.get(n["node_id"], {}))
        if max_conn is None or deg > max_conn["deg"]:
            max_conn = {"n": n, "deg": deg}
        if deg > 0 and n["node_id"] in used_as_next_hop and (min_conn is None or deg < min_conn["deg"]):
            min_conn = {"n": n, "deg": deg}

        pc = n.get("packet_count") or 0
        if max_pkt is None or pc > max_pkt["pc"]:
            max_pkt = {"n": n, "pc": pc}

        if most_silent is None or n.get("last_seen", 0.0) < most_silent["n"].get("last_seen", 0.0):
            most_silent = {"n": n}

    return {"max_conn": max_conn, "min_conn": min_conn, "max_pkt": max_pkt, "most_silent": most_silent}


def compute_stats(nodes, routes, status):
    root_id = status.get("root_id")
    by_id = {n["node_id"]: n for n in nodes}

    depth = _depth_map(nodes, routes)
    hist = _depth_histogram(depth)
    asym = _asymmetry(routes)
    roles = _role_distribution(nodes, root_id)

    hull = _hull_area_km2(nodes)
    hull_perim = _hull_perimeter_km(hull["hull"])
    circularity = None
    if hull["area"] is not None and hull_perim:
        circularity = 4 * math.pi * hull["area"] / (hull_perim ** 2)

    nbr = _build_neighbor_map(routes)
    links = _link_distance_stats(nodes, nbr, by_id)
    fun = _fun_stats(nodes, routes, nbr, by_id, root_id)

    return {
        "depth": depth,
        "histogram": hist,
        "asymmetry": asym,
        "roles": roles,
        "hull_area": hull["area"],
        "hull_count": hull["count"],
        "circularity": circularity,
        "links": links,
        "fun": fun,
    }


# =============================================================================
#                                RENDER (Pillow)
# =============================================================================

def _mm_to_px(mm):
    return int(round(mm * EXPORT_DPI / 25.4))


def _pt(pts):
    return int(round(pts * EXPORT_DPI / 72.0))


_FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/liberation",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/liberation2",
    "/usr/share/fonts/noto",
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts",
]


def _find_font_path(bold=False):
    names = []
    if bold:
        names += ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf"]
    names += ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf"]

    for d in _FONT_DIRS:
        for name in names:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    return None


def _load_font(size, bold=False):
    from PIL import ImageFont

    path = _find_font_path(bold)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return (bbox[2] - bbox[0], bbox[3] - bbox[1])


# -----------------------------------------------------------------------------
# Emoji: las fuentes tipo DejaVu/Liberation/Noto Sans no traen esos glifos (se
# ven como "tofu", el cuadrito vacio). Si hay una fuente de emoji en color
# instalada (NotoColorEmoji, la mas comun en distros Linux), separamos el
# texto en tramos texto/emoji y componemos el emoji como bitmap aparte,
# porque son fuentes de tamano de bitmap fijo (no se pueden pedir en
# cualquier tamano como una tipografia normal). Si no hay fuente de emoji en
# el sistema, todo sigue funcionando igual que antes (se ve el tofu).
# -----------------------------------------------------------------------------
_EMOJI_RANGES = (
    (0x1F1E6, 0x1F1FF),  # indicadores regionales (banderas)
    (0x1F300, 0x1F5FF),  # simbolos y pictogramas (incluye tonos de piel)
    (0x1F600, 0x1F64F),  # emoticones
    (0x1F680, 0x1F6FF),  # transporte y mapas
    (0x1F700, 0x1F77F),
    (0x1F780, 0x1F7FF),
    (0x1F800, 0x1F8FF),
    (0x1F900, 0x1F9FF),
    (0x1FA00, 0x1FAFF),
    (0x2600, 0x26FF),    # simbolos varios
    (0x2700, 0x27BF),    # dingbats
    (0x2B00, 0x2BFF),    # flechas/estrellas varias
    (0x2300, 0x23FF),    # tecnico varios (reloj, timer, etc.)
    (0xFE0F, 0xFE0F),    # variation selector-16 (fuerza estilo emoji)
    (0x200D, 0x200D),    # ZWJ (secuencias combinadas tipo familia)
)

_EMOJI_FONT_NAMES = ["NotoColorEmoji.ttf", "NotoColorEmoji-Regular.ttf",
                     "TwemojiMozilla.ttf", "Twemoji.ttf"]
_EMOJI_FONT_DIRS = _FONT_DIRS + [
    "/usr/share/fonts/google-noto-emoji",
    "/usr/share/fonts/noto-emoji",
    "/usr/share/fonts/truetype/noto-color-emoji",
]

_EMOJI_FONT_STATE = {"checked": False, "font": None}
_EMOJI_GLYPH_CACHE = {}


def _is_emoji_cp(cp):
    for lo, hi in _EMOJI_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _emoji_runs(text):
    """Parte `text` en tramos (segmento, es_emoji), agrupando codepoints
    consecutivos de la misma clase. Variation selectors y ZWJ quedan del
    lado emoji, asi una secuencia combinada no se corta a la mitad."""
    runs = []
    cur = ""
    cur_is_emoji = None
    for ch in text:
        is_e = _is_emoji_cp(ord(ch))
        if cur_is_emoji is None or is_e == cur_is_emoji:
            cur += ch
            cur_is_emoji = is_e
        else:
            runs.append((cur, cur_is_emoji))
            cur = ch
            cur_is_emoji = is_e
    if cur:
        runs.append((cur, cur_is_emoji))
    return runs


def _find_emoji_font_path():
    for d in _EMOJI_FONT_DIRS:
        for name in _EMOJI_FONT_NAMES:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    return None


def _get_emoji_font():
    """Fuente de emoji en color, cacheada. Son fuentes de bitmap con un
    unico tamano fijo (tipicamente 109px) asi que probamos una lista de
    tamanos comunes y nos quedamos con el primero que acepte la fuente."""
    from PIL import ImageFont

    if not _EMOJI_FONT_STATE["checked"]:
        _EMOJI_FONT_STATE["checked"] = True
        path = _find_emoji_font_path()
        if path:
            for size in (109, 96, 72, 64, 48, 40, 32, 24, 20, 16):
                try:
                    _EMOJI_FONT_STATE["font"] = ImageFont.truetype(path, size)
                    break
                except Exception:
                    continue
    return _EMOJI_FONT_STATE["font"]


def _emoji_target_h(font):
    return max(1, int(round(getattr(font, "size", 14))))


def _render_emoji_run(run_text, target_h):
    """Renderiza un tramo emoji (puede ser una secuencia con ZWJ/variation
    selector) como bitmap RGBA ya escalado a `target_h` px de alto."""
    font = _get_emoji_font()
    if font is None:
        return None
    key = (run_text, target_h)
    if key in _EMOJI_GLYPH_CACHE:
        return _EMOJI_GLYPH_CACHE[key]

    from PIL import Image, ImageDraw

    native = getattr(font, "size", 109)
    canvas = Image.new("RGBA", (native * (len(run_text) + 2), native * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    try:
        d.text((native, native // 2), run_text, font=font, embedded_color=True)
    except Exception:
        _EMOJI_GLYPH_CACHE[key] = None
        return None

    bbox = canvas.getbbox()
    if not bbox:
        _EMOJI_GLYPH_CACHE[key] = None
        return None

    glyph = canvas.crop(bbox)
    ratio = target_h / glyph.height
    new_w = max(1, int(round(glyph.width * ratio)))
    new_h = max(1, int(round(glyph.height * ratio)))
    glyph = glyph.resize((new_w, new_h), Image.LANCZOS)
    _EMOJI_GLYPH_CACHE[key] = glyph
    return glyph


def _mixed_text_size(draw, text, font):
    """Como _text_size pero contemplando tramos de emoji."""
    target_h = _emoji_target_h(font)
    w = 0.0
    h = 0.0
    for seg, is_emoji in _emoji_runs(text):
        glyph = _render_emoji_run(seg, target_h) if is_emoji else None
        if glyph is not None:
            w += glyph.width
            h = max(h, glyph.height)
        else:
            sw, sh = _text_size(draw, seg, font)
            w += sw
            h = max(h, sh)
    return w, h


def _draw_mixed_text(img, draw, x, y, text, font, fill):
    """Dibuja `text` (puede tener emoji) con esquina superior izquierda en
    (x, y), pegando cada tramo de emoji como bitmap y el resto con la
    tipografia normal."""
    target_h = _emoji_target_h(font)
    cx = x
    for seg, is_emoji in _emoji_runs(text):
        glyph = _render_emoji_run(seg, target_h) if is_emoji else None
        if glyph is not None:
            img.paste(glyph, (int(round(cx)), int(round(y))), glyph)
            cx += glyph.width
        else:
            draw.text((cx, y), seg, font=font, fill=fill)
            sw, _ = _text_size(draw, seg, font)
            cx += sw
    return cx - x


def _wrap(draw, text, font, max_w):
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        candidate = (cur + " " + w).strip()
        if not cur or _mixed_text_size(draw, candidate, font)[0] <= max_w:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_text_center(img, draw, cx, cy, text, font, fill):
    w, h = _mixed_text_size(draw, text, font)
    _draw_mixed_text(img, draw, cx - w / 2.0, cy - h / 2.0, text, font, fill)


def _draw_line(draw, p1, p2, color, width, dashed):
    x1, y1 = p1
    x2, y2 = p2
    if not dashed:
        draw.line([p1, p2], fill=color, width=width)
        return
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length <= 0:
        return
    ux = dx / length
    uy = dy / length
    dash = max(width * 4, 12)
    gap = max(width * 2.5, 8)
    t = 0.0
    on = True
    while t < length:
        seg = min(dash if on else gap, length - t)
        if on:
            sx = x1 + ux * t
            sy = y1 + uy * t
            ex = x1 + ux * (t + seg)
            ey = y1 + uy * (t + seg)
            draw.line([(sx, sy), (ex, ey)], fill=color, width=width)
        t += seg
        on = not on


def _draw_frame(draw, W, H):
    outer = [_mm_to_px(OUTER_MARGIN_MM), _mm_to_px(OUTER_MARGIN_MM),
             W - _mm_to_px(OUTER_MARGIN_MM), H - _mm_to_px(OUTER_MARGIN_MM)]
    inner = [_mm_to_px(INNER_MARGIN_MM), _mm_to_px(INNER_MARGIN_MM),
             W - _mm_to_px(INNER_MARGIN_MM), H - _mm_to_px(INNER_MARGIN_MM)]

    draw.rectangle(outer, outline=BLACK, width=2)
    draw.rectangle(inner, outline=BLACK, width=6)

    # Marcas de centrado (clasico) sobre el marco interior.
    mark = _mm_to_px(4)
    cx = (inner[0] + inner[2]) / 2
    cy = (inner[1] + inner[3]) / 2
    for x in (cx,):
        draw.line([(x, inner[1] - mark), (x, inner[1])], fill=BLACK, width=2)
        draw.line([(x, inner[3]), (x, inner[3] + mark)], fill=BLACK, width=2)
    for y in (cy,):
        draw.line([(inner[0] - mark, y), (inner[0], y)], fill=BLACK, width=2)
        draw.line([(inner[2], y), (inner[2] + mark, y)], fill=BLACK, width=2)

    return inner


def _fmt_ago(sec):
    if sec is None:
        return "-"
    if sec < 60:
        return f"{int(round(sec))}s"
    if sec < 3600:
        return f"{int(round(sec / 60))}m"
    return f"{int(round(sec / 3600))}h"


def _fmt_role(role):
    if not role:
        return "cliente/sin dato"
    r = role.upper()
    if r == "ROUTER":
        return "router"
    if r == "ROUTER_LATE":
        return "router_late"
    if r == "CLIENT_BASE":
        return "client_base"
    if r == "CLIENT_MUTE":
        return "client_mute"
    if r == "CLIENT_HIDDEN":
        return "client_hidden"
    return r.lower()


def _fmt_short(n):
    return n.get("short_name") or n.get("node_id", "")


def _fmt_link(entry):
    if not entry:
        return "sin datos"
    a = entry["a"]
    b = entry["b"]
    dist = entry["dist"]
    return f"{dist:.2f}km {_fmt_short(a)} <-> {_fmt_short(b)}"


def _build_title_items(status, stats):
    """Devuelve lista de (texto, tamano_pt, bold) para el cajetin."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total = status.get("total_nodes", 0)
    resolved = status.get("resolved_routes", 0)
    orphans = status.get("orphans", 0)
    snr = status.get("avg_snr")
    snr_txt = f"{snr} dB" if snr is not None else "-"

    region_raw = status.get("lora_region")
    region_txt = LORA_REGION_LABELS.get(region_raw, region_raw or "sin datos")

    items = [
        (PROJECT_TITLE, 11, True),
        (now_str, 8, False),
        (PROJECT_URL, 7, False),
        (f"Region LoRa: {region_txt}", 8, False),
        ("", 5, False),  # separador visual (linea en blanco)
    ]

    # Roles (sin la raiz, igual que en la UI).
    roles = stats["roles"]
    if roles:
        role_parts = []
        for role, count in sorted(roles.items(), key=lambda kv: -kv[1]):
            role_parts.append(f"{_fmt_role(role)} {count}")
        roles_txt = " · ".join(role_parts)
    else:
        roles_txt = "sin datos"

    # Histograma de profundidad.
    if stats["histogram"]:
        hist_parts = [f"{h['depth']}->{h['count']}" for h in stats["histogram"]]
        hist_txt = " · ".join(hist_parts)
    else:
        hist_txt = "sin datos"

    asym = stats["asymmetry"]
    if asym["pct"] is None:
        asym_txt = "sin datos"
    else:
        asym_txt = f"{asym['pct']:.0f}% ({asym['asym']} de {asym['both']})"

    if stats["hull_area"] is not None:
        hull_txt = f"~{stats['hull_area']:.0f} km2 ({stats['hull_count']} nodos GPS)"
    else:
        hull_txt = f"sin datos ({stats['hull_count']} nodos GPS)"

    if stats["circularity"] is not None:
        circ_txt = f"{stats['circularity']:.3f}"
    else:
        circ_txt = "sin datos"

    links = stats["links"]
    fun = stats["fun"]

    items.append((f"Nodos {total} · con ruta {resolved} · pendientes {orphans} · SNR {snr_txt}", 8, False))
    items.append((f"Roles: {roles_txt}", 8, False))
    items.append((f"Profundidad (saltos): {hist_txt}", 8, False))
    items.append((f"Asimetria ida/vuelta: {asym_txt}", 8, False))
    items.append((f"Convex hull: {hull_txt} · circularidad {circ_txt}", 8, False))
    items.append((f"GPS mas corto: {_fmt_link(links['shortest'])}", 8, False))
    items.append((f"GPS mas largo: {_fmt_link(links['longest'])}", 8, False))

    if fun:
        mc = fun["max_conn"]
        lc = fun["min_conn"]
        mp = fun["max_pkt"]
        ms = fun["most_silent"]

        mc_txt = f"{_display_name(mc['n'])} ({mc['deg']})" if mc else "-"
        lc_txt = f"{_display_name(lc['n'])} ({lc['deg']})" if lc else "sin datos aun"
        mp_txt = f"{_display_name(mp['n'])} ({mp['pc']})" if mp else "-"
        ms_txt = (
            f"{_display_name(ms['n'])} ({_fmt_ago(datetime.datetime.now().timestamp() - ms['n'].get('last_seen', 0))})"
            if ms else "-"
        )
        items.append((f"Mas conectado: {mc_txt} · menos: {lc_txt}", 8, False))
        items.append((f"Mas paquetes: {mp_txt} · mas silencioso: {ms_txt}", 8, False))

    return items


def _draw_title_block(img, draw, inner, status, stats):
    tb_w = _mm_to_px(TITLE_BLOCK_W_MM)
    pad = _mm_to_px(TITLE_BLOCK_PAD_MM)
    max_text_w = tb_w - 2 * pad
    line_gap = _pt(2)

    items = _build_title_items(status, stats)

    # Convertir cada item en lineas envueltas con su fuente.
    lines = []
    for text, pts, bold in items:
        font = _load_font(_pt(pts), bold=bold)
        if text == "":
            lines.append(("", font))
            continue
        for wrapped in _wrap(draw, text, font, max_text_w):
            lines.append((wrapped, font))

    content_h = 0
    for text, font in lines:
        _, h = _mixed_text_size(draw, text, font)
        content_h += h + line_gap
    content_h = max(content_h - line_gap, 0)

    tb_h = content_h + 2 * pad
    tb_x0 = inner[2] - tb_w
    tb_y0 = inner[3] - tb_h
    tb_x1 = inner[2]
    tb_y1 = inner[3]

    draw.rectangle([tb_x0, tb_y0, tb_x1, tb_y1], outline=BLACK, width=3, fill=WHITE)

    y = tb_y0 + pad
    for text, font in lines:
        if text:
            _draw_mixed_text(img, draw, tb_x0 + pad, y, text, font, BLACK)
        _, h = _mixed_text_size(draw, text, font)
        y += h + line_gap

    return tb_y0, tb_y1


def _draw_map(img, draw, nodes, routes, root_id, layout, rect):
    x0, y0, x1, y1 = rect
    by_id = {n["node_id"]: n for n in nodes}

    if not layout:
        font = _load_font(_pt(10))
        _draw_text_center(img, draw, (x0 + x1) / 2, (y0 + y1) / 2,
                          "sin datos de mapa (aun no hay nodos/rutas)", font, BLACK)
        return

    xs = [p[0] for p in layout.values()]
    ys = [p[1] for p in layout.values()]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    max_hw = 0.0
    for nid in layout:
        n = by_id.get(nid)
        if n:
            max_hw = max(max_hw, _node_rect_width(n) / 2.0)

    pad = 24.0
    node_h = 30.0
    content_w = (max_x - min_x) + 2 * (max_hw + pad)
    content_h = (max_y - min_y) + (node_h + 2 * pad)
    avail_w = x1 - x0
    avail_h = y1 - y0
    if content_w <= 0 or content_h <= 0 or avail_w <= 0 or avail_h <= 0:
        return

    scale = min(avail_w / content_w, avail_h / content_h)
    content_cx = (min_x + max_x) / 2.0
    content_cy = (min_y + max_y) / 2.0
    ox = (x0 + x1) / 2.0 - content_cx * scale
    oy = (y0 + y1) / 2.0 - content_cy * scale

    edge_w = max(2, int(round(1.8 * scale)))
    for r in routes:
        a = r.get("node_id")
        b = r.get("next_hop")
        if a not in layout or b not in layout:
            continue
        color = ROUTE_FWD if r.get("direction") == "fwd" else ROUTE_BACK
        dashed = r.get("direction") == "back"
        p1 = (ox + layout[a][0] * scale, oy + layout[a][1] * scale)
        p2 = (ox + layout[b][0] * scale, oy + layout[b][1] * scale)
        _draw_line(draw, p1, p2, color, edge_w, dashed)

    font_size = max(6, int(round(14 * scale)))
    font = _load_font(font_size, bold=False)

    for n in nodes:
        nid = n["node_id"]
        if nid not in layout:
            continue
        p = layout[nid]
        cx = ox + p[0] * scale
        cy = oy + p[1] * scale
        w = _node_rect_width(n) * scale
        h = _node_height(n) * scale
        box = [cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0]
        cls = _dot_class(n)
        radius = max(2, int(round(6 * scale)))
        border_w = max(2, int(round(2.2 * scale)))
        draw.rounded_rectangle(box, radius=radius, fill=NODE_FILL[cls],
                               outline=NODE_BORDER[cls], width=border_w)
        _draw_text_center(img, draw, cx, cy, _node_label(n), font, BLACK)


def _draw_legend(draw, x0, y_bottom, max_x):
    """Referencia de lineas y colores, en una sola fila, alineada abajo a la
    izquierda del marco (usa la franja que queda libre a la izquierda del
    cajetin). Se corta si no entra en max_x en vez de invadir el cajetin."""
    font = _load_font(_pt(7.5), bold=False)
    label_font = _load_font(_pt(7.5), bold=True)

    _, fh = _text_size(draw, "Ag", font)
    box_wh = int(round(fh * 0.9))
    line_w = _mm_to_px(6.0)
    line_thick = max(2, _pt(1.3))
    pad_after_swatch = _mm_to_px(1.3)
    gap_between_items = _mm_to_px(4.5)

    row_h = max(fh, box_wh)
    y0 = y_bottom - row_h
    cy = y0 + row_h / 2.0

    items = [
        ("line", ROUTE_FWD, False, "ida"),
        ("line", ROUTE_BACK, True, "vuelta"),
        ("node", "root", None, "raiz"),
        ("node", "router", None, "router"),
        ("node", "base", None, "client_base"),
        ("node", "client", None, "cliente/otros"),
    ]

    x = x0
    prefix = "Referencia:"
    pw, ph = _text_size(draw, prefix, label_font)
    draw.text((x, cy - ph / 2.0), prefix, font=label_font, fill=BLACK)
    x += pw + gap_between_items

    for kind, color, dashed, label in items:
        tw, th = _text_size(draw, label, font)
        item_w = (line_w if kind == "line" else box_wh) + pad_after_swatch + tw
        if x + item_w > max_x:
            break
        if kind == "line":
            ly = int(round(cy))
            _draw_line(draw, (x, ly), (x + line_w, ly), color, line_thick, dashed)
            x += line_w
        else:
            box = [x, cy - box_wh / 2.0, x + box_wh, cy + box_wh / 2.0]
            draw.rounded_rectangle(box, radius=max(1, int(box_wh * 0.25)),
                                   fill=NODE_FILL[color], outline=NODE_BORDER[color],
                                   width=max(1, _pt(1.0)))
            x += box_wh
        x += pad_after_swatch
        draw.text((x, cy - th / 2.0), label, font=font, fill=BLACK)
        x += tw + gap_between_items

    return y0


def render_export_png(nodes, routes, status):
    """Devuelve los bytes del PNG. Levanta RuntimeError si Pillow no esta."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise RuntimeError(
            "Pillow no esta instalado en el servidor. Instala el wheel de Pillow "
            "(ver wheelhouse_uconsole/fetch_pillow.sh) para poder exportar a PNG."
        )

    root_id = status.get("root_id")
    stats = compute_stats(nodes, routes, status)

    W = _mm_to_px(SHEET_W_MM)
    H = _mm_to_px(SHEET_H_MM)

    img = Image.new("RGB", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    inner = _draw_frame(draw, W, H)

    tb_y0, _tb_y1 = _draw_title_block(img, draw, inner, status, stats)

    # El mapa ocupa el ancho COMPLETO del marco interior: como su fila
    # (y0..y1) queda siempre por encima del cajetin (que vive en la franja
    # inferior derecha), nunca se solapan aunque el mapa use todo el ancho.
    # Antes se restaba el ancho del cajetin en toda la altura, dejando una
    # franja vacia enorme arriba a la derecha.
    map_rect = [
        inner[0] + _mm_to_px(MAP_PAD_MM),
        inner[1] + _mm_to_px(MAP_PAD_MM),
        inner[2] - _mm_to_px(MAP_PAD_MM),
        tb_y0 - _mm_to_px(TITLE_BLOCK_GAP_MM),
    ]

    map_w = map_rect[2] - map_rect[0]
    map_h = map_rect[3] - map_rect[1]
    target_aspect = (map_w / map_h) if map_h > 0 else 1.0
    layout = compute_layout(nodes, routes, root_id, target_aspect=target_aspect)

    _draw_map(img, draw, nodes, routes, root_id, layout, map_rect)

    # La referencia va abajo a la izquierda, en la franja que queda libre
    # entre el fondo del mapa y el borde inferior del marco (a la izquierda
    # del cajetin, con el que nunca se solapa gracias a max_x).
    tb_x0 = inner[2] - _mm_to_px(TITLE_BLOCK_W_MM)
    _draw_legend(
        draw,
        inner[0] + _mm_to_px(MAP_PAD_MM),
        inner[3] - _mm_to_px(MAP_PAD_MM),
        tb_x0 - _mm_to_px(TITLE_BLOCK_GAP_MM),
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
