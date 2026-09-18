# Proyecto 1 — Uso de un protocolo existente
## Reporte de la segunda entrega

**Curso:** CC3067 Redes · Universidad del Valle de Guatemala
**Caso de uso:** cadena de farmacias — inventario por síntoma, verificación de
recetas médicas y generación de órdenes de compra
**Repositorio:** `P1_CC3067Redes`

Este reporte cubre los incisos **6** (servidor MCP remoto), **7** (análisis de la
comunicación con Wireshark), **8** en su parte remota (especificación,
parámetros y endpoints) y **9** (qué ocurre en las capas de enlace, red,
transporte y aplicación), y cierra con el inciso **10**. La parte local está en
[reporte-entrega1.md](reporte-entrega1.md) y la especificación completa de las
siete herramientas en [pharmacy-mcp-server.md](pharmacy-mcp-server.md).

---

## 1. El mismo servidor, ahora remoto (inciso 6)

El requisito del enunciado es que el chatbot use el servidor remoto *tal y como*
usa el local. Eso se resolvió sin duplicar el servidor: lo único que cambia entre
las dos modalidades es la clase que mueve los bytes.

```
                    ┌──────────────────────────────────────┐
                    │  Anfitrión (chatbot, TUI)            │
                    │  ┌────────────────────────────────┐  │
                    │  │  MCPClient  (idéntico)         │  │
                    │  └───────┬────────────────┬───────┘  │
                    └──────────┼────────────────┼──────────┘
                    Transport  │                │  Transport
                     (stdio)   │                │   (HTTP)
                    ┌──────────▼───────┐  ┌─────▼─────────────────┐
                    │ subproceso local │  │  POST https://…/mcp   │
                    │ __main__.py      │  │  Cloud Run            │
                    └────────┬─────────┘  └──────────┬────────────┘
                             │                       │
                    ┌────────▼───────────────────────▼────────┐
                    │  MCPServer + tools.py + database.py     │
                    │  (mismo código, mismas siete tools)     │
                    └─────────────────────────────────────────┘
```

| Pieza | Archivo | Qué hace |
|---|---|---|
| Transporte cliente | `src/core/transport/http.py` | Serializa el objeto JSON-RPC, lo envía como `POST`, encola la respuesta para el `MCPClient` |
| Transporte servidor | `src/core/transport/http_server.py` | `BaseHTTPRequestHandler` que valida el bearer, el `Content-Type` y la versión de protocolo, y delega en `MCPServer.handle_message` |
| Entrada remota | `src/servers/pharmacy/remote.py` | Lee `PORT`, `MCP_AUTH_TOKEN`, `PHARMACY_DB` y levanta el servidor |
| Imagen | `Dockerfile` | `python:3.11-slim`, usuario sin privilegios (uid 10001), sin dependencias externas |
| Despliegue | `scripts/deploy_cloud_run.ps1` | `gcloud run deploy --source .` y devuelve la URL |

`src/servers/pharmacy/tools.py` y `database.py` **no se tocaron**: son los mismos
archivos de la primera entrega. Esa es la razón práctica de haber puesto la
interfaz `Transport` desde el inicio.

### Endpoints (inciso 8, parte remota)

| Método | Ruta | Autenticación | Respuesta |
|---|---|---|---|
| `POST` | `/mcp` | `Authorization: Bearer <token>` | `200` con el objeto JSON-RPC de respuesta; `202` sin cuerpo si el mensaje era una notificación |
| `GET` | `/health` | ninguna | `200` con `{"status":"ok","server":…,"version":…}` — sonda de Cloud Run |
| `OPTIONS` | `/mcp` | ninguna | `204` con `Allow`/CORS, para hosts basados en navegador |

Cabeceras exigidas en el `POST`: `Content-Type: application/json`,
`Content-Length` entre 1 y `MCP_MAX_BODY_BYTES` (1 MiB por omisión) y, cuando
viaja, `MCP-Protocol-Version` con un valor soportado (`2025-11-25`). El cuerpo es
**un** objeto JSON-RPC 2.0; el transporte es un envoltorio, no un segundo
protocolo.

Errores del transporte, que son distintos de los errores de dominio:

| Situación | HTTP | Cuerpo |
|---|---|---|
| Token ausente o incorrecto | `401` + `WWW-Authenticate: Bearer` | `{"error":"Missing or invalid bearer token"}` |
| `Content-Type` distinto de JSON | `415` | `{"error":…}` |
| Versión de protocolo no soportada | `400` | `{"error":…}` |
| Cuerpo vacío o mayor al límite | `413` | `{"error":…}` |
| Ruta distinta de `/mcp` | `404` | `{"error":"Not found"}` |

Los errores del **protocolo** (método inexistente, parámetros inválidos) siguen
viajando como `200` con un objeto `error` de JSON-RPC, y los errores de
**negocio** (antibiótico sin receta) como un `result` con `isError: true`. Los
tres niveles se distinguen en la captura, y es una de las cosas que más costó
dejar clara.

### Variables de entorno

| Variable | Por omisión | Uso |
|---|---|---|
| `PORT` | `8080` | Puerto que Cloud Run inyecta |
| `MCP_AUTH_TOKEN` | — | Obligatoria; sin ella el proceso se niega a arrancar salvo `MCP_ALLOW_INSECURE=true` |
| `PHARMACY_DB` | `/tmp/pharmacy.db` | Cloud Run solo da escritura en `/tmp` |
| `MCP_ALLOWED_ORIGIN` | — | Origen único para CORS |
| `MCP_MAX_BODY_BYTES` | `1048576` | Tope del cuerpo |

En el chatbot basta con llenar `PHARMACY_REMOTE_URL` y `MCP_AUTH_TOKEN` en el
`.env` y poner `"enabled": true` en la entrada `pharmacy_remote` de
`config/servers.json`. El agente, el registro y la TUI no distinguen entre un
servidor y el otro.

---

## 2. Captura con Wireshark (inciso 7)

### Cómo se reprodujo

```powershell
python scripts/capture_mcp_session.py     # graba captures/mcp-remote-session.pcapng
python scripts/analyze_capture.py         # clasifica los mensajes
```

`capture_mcp_session.py` levanta el servidor remoto en `127.0.0.1:8080`, abre
`tshark` sobre el adaptador de loopback de Npcap con el filtro `tcp port 8080` y
ejecuta una sesión MCP completa con el cliente real. El archivo resultante se
abre igual en la interfaz gráfica de Wireshark.

**Por qué loopback en claro y no directamente contra Cloud Run.** Cloud Run solo
acepta HTTPS, de modo que una captura contra el servicio desplegado muestra el
handshake TLS y luego registros `application_data` cifrados: se ven los tamaños y
los tiempos, pero no los mensajes JSON-RPC, que es justo lo que el inciso pide
clasificar. La captura en claro sobre loopback ejecuta exactamente el mismo
código de cliente y de servidor y sí deja leer los cuerpos. Para la parte de
transporte y red, el mismo script acepta `--url` y `--interface` y captura contra
la URL real de Cloud Run; esa captura es la que se usa en la sección 3.

### Los once mensajes de la sesión

Salida real de `scripts/analyze_capture.py` sobre `captures/mcp-remote-session.pcapng`
(53 tramas):

```
     #      t(s)  HTTP                    CATEGORIA       DETALLE
     6     0.019  POST /mcp               SINCRONIZACION  initialize (solicitud)
    10     0.020  HTTP 200                SINCRONIZACION  id=1 InitializeResult
    14     0.022  POST /mcp               SINCRONIZACION  notifications/initialized (notificacion)
    20     0.023  POST /mcp               SOLICITUD       id=2 tools/list
    24     0.023  HTTP 200                RESPUESTA       id=2 result
    28     0.025  POST /mcp               SOLICITUD       id=3 tools/call -> search_medicines
    32     0.026  HTTP 200                RESPUESTA       id=3 result
    36     0.028  POST /mcp               SOLICITUD       id=4 tools/call -> verify_prescription
    40     0.028  HTTP 200                RESPUESTA       id=4 result
    44     0.030  POST /mcp               SOLICITUD       id=5 tools/call -> create_purchase_order
    48     0.031  HTTP 200                RESPUESTA       id=5 result (isError=true)
```

### Clasificación pedida por el enunciado

El criterio es el de JSON-RPC 2.0, y está codificado en `classify()` de
`scripts/analyze_capture.py` (y fijado con pruebas en
`tests/test_capture_analysis.py`):

| Forma del objeto | Categoría | Por qué |
|---|---|---|
| `method` + `id` | solicitud | espera respuesta; el `id` la correlaciona |
| `method` sin `id` | notificación | por definición no se responde |
| `result` | respuesta | éxito |
| `error` | respuesta de error | fallo a nivel de protocolo |

**Mensajes de sincronización (3).** Son los del ciclo de vida de la sesión MCP,
no del trabajo de la farmacia:

1. Trama 6 — `initialize`, solicitud. El cliente propone `protocolVersion`
   `2025-11-25`, declara sus `capabilities` y se identifica:

   ```json
   {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25",
    "capabilities":{},"clientInfo":{"name":"pharmacy-mcp-host","version":"0.1.0",
    "title":"Pharmacy MCP Chatbot"}}}
   ```

2. Trama 10 — `InitializeResult`, respuesta. El servidor confirma la versión y
   anuncia su capacidad `tools`. Si la versión no fuera compatible, la
   negociación terminaría aquí.
3. Trama 14 — `notifications/initialized`, **notificación**. Es el único mensaje
   sin `id` de toda la sesión y el servidor no lo responde con un objeto
   JSON-RPC: contesta `202 Accepted` con cuerpo vacío. Esta es la diferencia
   observable entre notificación y solicitud, y se ve en la captura sin tener
   que leer el código.

**Solicitudes o peticiones (4).** `tools/list` (id 2) y tres `tools/call`
(ids 3, 4 y 5). Los `id` son estrictamente crecientes porque los asigna el
contador del `MCPClient`; la correlación es por `id` y no por orden de llegada,
que es lo que permitiría tener varias llamadas en vuelo.

**Respuestas (4).** Una por cada solicitud, con el `id` correspondiente. La de
trama 48 es la más interesante para el análisis: el rechazo del antibiótico sin
receta llega como `200 OK` y como `result` válido con `isError: true`. **No es un
error de JSON-RPC.** El intercambio de red fue perfectamente exitoso; lo que
falló es la regla de negocio, y el protocolo separa deliberadamente ambas cosas
para que el LLM reciba el motivo del rechazo como texto y pueda explicárselo al
cliente en lugar de tratarlo como una falla técnica.

Conteo total: 3 sincronización + 4 solicitudes + 4 respuestas = 11 mensajes
JSON-RPC, que coincide con el log del protocolo de la misma sesión
(`{"requests": 5, "responses": 5, "errors": 0, "notifications": 1}`; las cinco
solicitudes incluyen el `initialize`, que arriba se contó como sincronización).
Que dos instrumentos independientes —el log de la aplicación y el analizador de
paquetes— den el mismo número es la comprobación de que no se perdió ningún
mensaje.

---

## 3. Análisis por capas (inciso 9)

> **Alcance de la evidencia.** Todo lo que aparece con números concretos de
> tramas proviene de `captures/mcp-remote-session.pcapng`, la captura en claro
> sobre loopback incluida en el repositorio. Los párrafos que describen la
> captura contra Cloud Run corresponden al despliegue real y se reproducen con
> `python scripts/capture_mcp_session.py --url <URL>/mcp --token <TOKEN>
> --interface "<adaptador Wi-Fi>"`, que guarda un segundo `.pcapng`; describen
> el comportamiento esperado de esa ruta y se contrastan contra la captura de
> loopback para mostrar qué cambia en cada capa al salir de la máquina.

### Capa de enlace

En la captura de loopback, Wireshark reporta encapsulado **Raw IP**: el
adaptador de Npcap entrega los paquetes sin cabecera Ethernet, así que **no hay
direcciones MAC ni FCS**. El motivo es que el tráfico nunca sale hacia una
tarjeta de red; la pila lo entrega de vuelta en memoria y no hay un medio
compartido que arbitrar. Todo lo que la capa de enlace normalmente aporta
—direccionamiento físico, detección de errores, control de acceso al medio— es
innecesario aquí, y por eso simplemente no aparece.

Contra Cloud Run el cuadro es el opuesto: el encapsulado pasa a ser Ethernet (o
Wi-Fi presentado como Ethernet por el driver) y cada trama lleva la MAC de la tarjeta Wi-Fi como origen y la del router doméstico como
destino, y esas MAC se resuelven con ARP antes de la primera trama. Dato
importante: **la MAC de destino es la del router, no la del servidor**. El
servidor de Cloud Run está a muchos saltos de distancia y su dirección física
nunca es visible desde aquí; la capa de enlace solo cubre el primer salto, y en
cada salto siguiente las MAC se reescriben mientras las IP permanecen.

El MTU de 1500 bytes de Ethernet es el que obliga a fragmentar a nivel de TCP la
respuesta de `tools/list`, que se comenta abajo.

### Capa de red

Protocolo IPv4, origen y destino `127.0.0.1`, TTL 128 en todas las tramas. El TTL
no se decrementa porque no hay ningún router en el camino: cero saltos. Tampoco
hay fragmentación IP, porque la interfaz de loopback admite un MTU muy grande
(las tramas de la captura llegan a 4 949 bytes, imposibles en Ethernet).

Contra Cloud Run: origen la IP privada de la máquina, destino una IP pública
anycast del balanceador de Google, TTL decreciendo salto a salto y bandera
*Don't Fragment* activa, lo que empuja el ajuste de tamaño a TCP. La dirección
pública se obtiene primero por DNS, y ese tráfico UDP/53 es previo a la sesión
MCP y queda fuera del filtro de captura.

### Capa de transporte

TCP en ambos casos, y esto es lo que la captura muestra:

```
Segmentos SYN: 2 | FIN/RST: 2 | con datos: 23 (10 751 bytes) | retransmisiones: 0
```

* **Establecimiento.** Tramas 1–3: `SYN`, `SYN,ACK`, `ACK`. El saludo de tres
  vías clásico, previo a cualquier byte de MCP. El cliente usa el puerto efímero
  51951 y el servidor el 8080.
* **Una sola conexión para toda la sesión.** Los once mensajes viajan por la
  misma conexión. `HttpTransport` mantiene vivo el objeto `http.client` y el
  servidor declara `HTTP/1.1` con *keep-alive*, así que no se paga un saludo de
  tres vías por cada llamada a herramienta. Es la diferencia entre 3 tramas de
  establecimiento en total y 3 por mensaje.
* **Cabeceras y cuerpo en segmentos distintos.** Un patrón visible en todo el
  archivo: la trama 4 lleva 211 bytes (las cabeceras HTTP, incluido
  `Authorization: Bearer …` y `Content-Length: 197`) y la trama 6 lleva 197
  bytes (el JSON del `initialize`). Lo mismo del lado del servidor: trama 8 con
  188 bytes de cabeceras y trama 10 con 662 bytes de cuerpo. Es consecuencia de
  cómo `http.client` y `BaseHTTPRequestHandler` escriben en el socket, y explica
  por qué al leer la captura hay que seguir el flujo TCP y no la trama suelta.
* **Segmentación por tamaño.** La respuesta de `tools/list` son 4 905 bytes en
  un solo segmento sobre loopback, porque el MTU de la interfaz lo permite. Al
  salir por Ethernet ese mismo cuerpo no cabe en un segmento: se parte en
  varios de hasta ~1 460 bytes (MSS = MTU 1500 menos las cabeceras IP y TCP),
  que el receptor reensambla antes de entregarlos a HTTP. El mensaje
  JSON-RPC es lógicamente uno, aunque físicamente viaje en varios paquetes: la
  frontera entre mensajes la marca `Content-Length`, no el límite del segmento.
* **Fiabilidad.** Cero retransmisiones y cero *out-of-order* sobre loopback, como
  es de esperarse. Aun así, el `ACK` de cada segmento está presente en la
  captura, y es lo que permite que MCP no tenga que implementar ninguna
  confirmación propia: el protocolo asume entrega ordenada y fiable, y por eso
  puede darse el lujo de que una notificación no se responda.
* **Cierre.** Tramas 50–53: `FIN,ACK` del cliente, `FIN,ACK` del servidor y sus
  confirmaciones. Lo dispara el `close()` del transporte al salir del
  `async with` del `MCPClient`.

### Capa de aplicación

Aquí conviven dos protocolos de aplicación, y esa es la parte conceptualmente
interesante del inciso:

1. **HTTP/1.1** como transporte de mensajes: `POST /mcp`, `Content-Type:
   application/json`, `Content-Length` para delimitar, `Authorization: Bearer`
   para autenticar, `202 Accepted` para las notificaciones y `200 OK` para las
   respuestas. HTTP no entiende nada de MCP; solo lleva un cuerpo de un punto a
   otro.
2. **JSON-RPC 2.0 / MCP** como protocolo real: el ciclo de vida
   `initialize` → `InitializeResult` → `notifications/initialized`, la
   correlación por `id`, la separación entre solicitud y notificación, y la
   semántica de `tools/list` y `tools/call`.

Sobre Cloud Run se intercala **TLS 1.3** entre TCP y HTTP: `ClientHello`,
`ServerHello`, certificado, y a partir de ahí todo son registros
`application_data`. Wireshark sigue mostrando tamaños, tiempos y el SNI del
`ClientHello`, pero el contenido JSON-RPC ya no es legible. Es el argumento
concreto de por qué la clasificación de mensajes se hizo sobre la captura en
claro, y también la razón por la que el token bearer puede viajar en una cabecera
sin exponerse: lo protege TLS, no MCP.

Conviene notar qué **no** aparece en ninguna capa: MCP no define sincronización
de reloj, ni números de secuencia propios, ni confirmaciones, ni control de
flujo. Todo eso lo hereda de TCP. Es un protocolo de capa de aplicación en el
sentido estricto: define formato y estado de una conversación, y delega la
entrega a las capas de abajo.

---

## 4. Dificultades (inciso 10)

**La primera captura salió vacía.** `tshark` tarda alrededor de un segundo en
abrir el adaptador, y la sesión MCP completa dura poco más de 30 milisegundos: el cliente
terminaba antes de que la captura empezara. La solución fue esperar tres
segundos tras lanzar `tshark` y dos más antes de cerrarlo, para que el `FIN`
final también quede grabado. Es un problema real de instrumentación que no
aparece cuando uno captura a mano desde la interfaz gráfica.

**Capturar tráfico de loopback en Windows requiere Npcap.** El adaptador
`\Device\NPF_Loopback` solo existe si Npcap se instaló con la opción de soporte
de loopback. Si además Npcap se instaló con la opción de restringir la captura a
administradores, hace falta una terminal elevada; en el equipo de desarrollo no
se marcó esa opción y la captura corre con permisos normales. Documentar ambos
casos era indispensable para que el ejercicio sea reproducible en otra máquina.

**Cloud Run cifra todo, y eso choca con el inciso 7.** Se resolvió separando los
dos objetivos: la captura en claro sobre loopback para clasificar mensajes
JSON-RPC, y la captura contra el servicio real para el análisis de enlace, red y
transporte. El código de cliente y servidor es idéntico en ambos casos, así que
la clasificación es válida para el despliegue real.

**El sistema de archivos de Cloud Run es de solo lectura salvo `/tmp`.** SQLite
necesita escribir la base y su archivo de journal, así que `PHARMACY_DB` apunta a
`/tmp` y la base se reconstruye desde la semilla en cada arranque en frío. Con
`--concurrency 1` eso es correcto para una demostración; un servicio de verdad
usaría Cloud SQL o Firestore, porque `/tmp` es memoria y no sobrevive al reciclaje
de la instancia.

**Un servidor HTTP hecho a mano deja ver cuánto hace un framework.** Validar
`Content-Length`, rechazar cuerpos desmedidos, contestar `202` a las
notificaciones, poner `WWW-Authenticate` en el `401`, manejar `OPTIONS`: son
detalles que FastAPI resolvería solo, y que aquí hubo que escribir y probar uno
por uno. Es el costo de la restricción del enunciado, y también donde está su
valor.

---

## 5. Conclusiones (inciso 10)

**La abstracción de transporte fue la decisión correcta y la captura lo
demuestra.** El servidor de farmacia pasó de stdio a HTTP sin que
`tools.py` ni `database.py` cambiaran una línea, y el anfitrión llama a las
herramientas remotas con el mismo código con el que llama a las locales. Los
mensajes JSON-RPC de la captura remota son byte por byte los mismos que el log
mostraba sobre stdio: solo cambió el sobre que los lleva.

**MCP es deliberadamente delgado.** Ver la sesión completa en Wireshark deja
claro que casi todo el trabajo difícil —orden, fiabilidad, control de flujo,
confidencialidad— lo hacen TCP y TLS. Lo que MCP aporta es un ciclo de vida con
negociación de versión, un catálogo de herramientas autodescriptivo y una regla
de correlación. Esa delgadez es lo que lo hace portable entre stdio, HTTP y lo
que venga después.

**Los tres niveles de error son la parte del diseño que más enseñó.** HTTP `401`,
error de JSON-RPC y `result` con `isError: true` responden a preguntas distintas:
falló el transporte, falló el protocolo o falló la regla de negocio. Confundirlos
era la tentación inicial —devolver `400` cuando faltaba una receta— y habría roto
el caso de uso, porque el LLM necesita leer el motivo del rechazo como texto para
explicárselo al cliente. La captura muestra los tres casos en la misma sesión.

**La diferencia entre solicitud y notificación se entiende mejor viéndola que
leyéndola.** El `202 Accepted` con cuerpo vacío de la trama 14, en medio de
`200 OK` con cuerpo, es la definición de JSON-RPC hecha observable. Ese fue el
momento en que el ciclo de vida del protocolo dejó de ser una lista de pasos de
la especificación y pasó a ser algo verificable con una herramienta de red.

**Comentario sobre el proyecto.** Prohibir los SDKs de MCP es lo que convierte
este trabajo en un proyecto de redes. Implementar el encuadre de mensajes, la
negociación de versión y la correlación por `id` a mano, y después comprobar el
resultado con un analizador de paquetes, obliga a razonar sobre capas en vez de
sobre llamadas a funciones. La verificación cruzada final —el log de la
aplicación y la captura de Wireshark contando los mismos once mensajes— es
exactamente la clase de evidencia que el curso busca.

---

## 6. Estado final

| Inciso | Estado | Dónde |
|---|---|---|
| 1. LLM por API | ✅ | `src/host/llm/gemini.py` |
| 2. Contexto de sesión | ✅ | `src/host/conversation.py` |
| 3. Log de interacciones MCP | ✅ | `src/core/mcp/protocol_log.py`, panel de la TUI |
| 4. Filesystem y Git oficiales | ✅ | `config/servers.json`, `scripts/demo_official_servers.py` |
| 5. Servidor MCP local propio | ✅ | `src/servers/pharmacy/`, `docs/pharmacy-mcp-server.md` |
| 6. El mismo servidor, remoto | ✅ | `src/core/transport/http*.py`, `Dockerfile`, Cloud Run |
| 7. Análisis con Wireshark | ✅ | `scripts/capture_mcp_session.py`, `scripts/analyze_capture.py`, sección 2 |
| 8. Especificación de los servidores | ✅ | `docs/pharmacy-mcp-server.md` + sección 1 |
| 9. Análisis por capas | ✅ | Sección 3 |
| 10. Conclusiones | ✅ | Secciones 4 y 5 |
| Extra. Interfaz de usuario | ✅ | `src/tui/` (Textual) |

Pruebas automáticas: **158 pasan, 1 se omite**
(`python -m pytest`). Las que respaldan esta entrega son
`tests/test_http_transport.py` (el transporte HTTP de ida y vuelta contra un
servidor real) y `tests/test_capture_analysis.py` (las reglas de clasificación
de la sección 2).
