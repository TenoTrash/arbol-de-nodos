# arbol-de-nodos

Visualiza la topología de una malla de Meshtastic como un grafo de fuerzas, con la raíz (el nodo conectado por serial) anclada al borde izquierdo. Las rutas de **ida** y **vuelta** se resuelven y dibujan por separado, porque en Meshtastic pueden pasar por nodos distintos (ruteo asimétrico) — esto es intencional, no un bug, jajajajaj!

Proyecto hermano de [mapa-mesh](https://github.com/TenoTrash/mapa-mesh), pero totalmente independiente: hardware propio, puerto serie propio, sin dependencia entre ambos.

## Requisitos

- Python 3.11+
- Un nodo Meshtastic conectado por USB (serial)
- `cryptography`, `flask`, `flask-socketio`, `meshtastic` (ver `requirements.txt`)

## Instalación

```bash
git clone https://github.com/TenoTrash/arbol-de-nodos
cd arbol-de-nodos
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuración

Antes de arrancar, revisá estas constantes al principio de `arbol_de_nodos.py`:

- `SERIAL_PORT`: puerto donde está el nodo (`/dev/ttyUSB0`, `/dev/ttyACM0`, etc. —
  varía según el chip USB-serial de la placa). Confirmalo con
  `ls /dev/ttyUSB* /dev/ttyACM*` con el nodo enchufado.
- `BIND_HOST`: por default `127.0.0.1` — la interfaz **solo se ve desde la misma
  máquina**, a propósito. Pensado para una sola persona mirando la pantalla
  localmente (por ejemplo, durante un survey de campo). Si necesitás verlo desde
  otro dispositivo en la misma red, hay que sumar autenticación antes de exponerlo
  — no lo cambies a `0.0.0.0` sin eso.
- `TRACEROUTE_COOLDOWN_SEC` / `FIRMWARE_COOLDOWN_SEC`: cooldowns entre traceroutes,
  pensados para no saturar la malla. No los bajes sin pensarlo dos veces.

### Certificado SSL

```bash
mkdir -p ssl
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout ssl/arbol.key -out ssl/arbol.pem \
  -subj "/CN=localhost"
```

Sin esto, cae a un certificado autofirmado `adhoc` que cambia en cada arranque (el navegador va a re-advertir cada vez que reiniciés el proceso). El `.gitignore` ya excluye `ssl/` — nunca subas la clave privada a un repo!!!

### Logo (opcional)

Si querés un logo en la barra lateral, poné un `logo_mesharg.png` al lado del script. Si no está, el `<img>` simplemente se oculta.

## Correr

```bash
python3 arbol_de_nodos.py
```

Con el nodo conectado, el log debería mostrar `Conectado a Meshtastic. Raiz del arbol: !xxxxxxxx` a los pocos segundos. Después abrí:

```
https://127.0.0.1:8090
```

## Assets vendorizados (uso sin internet)

`vendor/d3.min.js` y `vendor/socket.io.min.js` ya están incluidos en el repo — el HTML los sirve localmente (`/vendor/...`), no desde un CDN. Esto es a propósito:
la app puede correr en una notebook sin salida a internet (pensado para uso de campo — terrazas, cerros, sin señal en general)

## Instalación 100% air-gapped (sin internet en el destino)

Si vas a instalar esto en una máquina que nunca va a tener internet (por ejemplo, un cyberdeck de campo), armá el wheelhouse en **otra** máquina que sí tenga red, apuntando a la arquitectura y versión de Python exactas del destino:

```bash
pip download -r requirements.txt \
  --dest wheelhouse \
  --platform manylinux2014_<ARQUITECTURA> \
  --python-version <VERSION_SIN_PUNTO> \
  --implementation cp \
  --abi cp<VERSION_SIN_PUNTO> \
  --only-binary=:all:
```

Por ejemplo, para aarch64 + Python 3.13: `--platform manylinux2014_aarch64 --python-version 313 --implementation cp --abi cp313`. Confirmá la arquitectura real del destino con `uname -m` y la versión con `python3 --version` — no lo adivines, si no coincide exactamente el wheelhouse no sirve para nada.

Después, en el destino sin internet:

```bash
python3 -m venv venv && source venv/bin/activate
pip install --no-index --find-links=wheelhouse -r requirements.txt
```

## Qué muestra la interfaz

- **Grafo**: raíz a la izquierda, resto del mesh distribuido según profundidad (saltos) real, no según cuándo llegó cada respuesta. Líneas celestes = ruta de ida, naranjas punteadas = ruta de vuelta. 'Hover' sobre un nodo (o click en la lista) resalta el camino completo ida/vuelta hacia la raíz y atenúa el resto.
- **Barra lateral**: estado de conexión, estadísticas (nodos, rutas resueltas, pendientes, SNR promedio), leyenda de colores por rol, buscador, y la lista completa de nodos conocidos (nombre largo y corto, tiempo desde el último paquete).
- **Recuadro de estadísticas** (esquina inferior izquierda): nodo más conectado, relay con menos conexiones (excluye finales de malla y `CLIENT_MUTE`,que siempre tienen grado 1), nodo del que más paquetes se reciben, y nodo más silencioso.
- **Exportar CSV**: nodos (con GPS, telemetría, rol) y rutas (ida/vuelta, con origen `traceroute` o `direct`).

## Notas de diseño

- El traceroute se dispara para **cualquier** nodo escuchado, no solo los que mandan posición GPS.
- Un traceroute confirmado nunca es pisado por una detección pasiva "direct" (heurística de 0 saltos) — evita que rutas confirmadas se corrompan por una detección más reciente pero menos confiable.
- Datos de mesh (nombres, roles) se tratan como no confiables: se escapan antes de insertarse en el DOM (XSS) y se neutralizan antes de exportarse a CSV (CSV/Formula injection).

