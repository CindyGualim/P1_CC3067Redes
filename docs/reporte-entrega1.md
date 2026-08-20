# Proyecto 1 — Uso de un protocolo existente
## Reporte de la primera entrega

**Curso:** CC3067 Redes · Universidad del Valle de Guatemala
**Caso de uso:** cadena de farmacias — inventario por síntoma, verificación de
recetas médicas y generación de órdenes de compra
**Repositorio:** `P1_CC3067Redes`

Este reporte cubre los incisos **8** (especificación del servidor MCP
desarrollado) y **10** (conclusiones), referidos a los servidores que se
ejecutan de forma **local**. El inciso 9 y la parte remota de los incisos 8 y 10
corresponden a la segunda entrega.

---

## 1. Qué se implementó

El proyecto es un chatbot de terminal que actúa como **anfitrión (host)** de MCP.
Coordina tres servidores simultáneos y un modelo de lenguaje:

```
                    ┌──────────────────────────────┐
                    │  Anfitrión (chatbot, TUI)    │
                    │  ┌────────────────────────┐  │
   Gemini API  ◄────┼──┤ Agente: bucle de tools │  │
                    │  └───────────┬────────────┘  │
                    │              │               │
                    │  ┌───────────▼────────────┐  │
                    │  │ Registro de servidores │  │
                    │  └──┬─────────┬────────┬──┘  │
                    └─────┼─────────┼────────┼─────┘
                       cliente   cliente  cliente     JSON-RPC 2.0 sobre stdio
                          │         │        │
                    ┌─────▼───┐ ┌───▼────┐ ┌─▼──────────┐
                    │ farmacia│ │filesys.│ │    git     │
                    │ (propio)│ │(oficial)│ │ (oficial) │
                    └─────────┘ └────────┘ └────────────┘
```

**La implementación del protocolo es manual.** No se usa FastMCP ni el SDK `mcp`
en ningún punto del código propio: los mensajes JSON-RPC se construyen, enmarcan
y validan en `src/core/`. El paquete `mcp` sí queda instalado porque es
dependencia del servidor Git oficial, que es un proceso externo; la prueba
`tests/test_no_mcp_sdk.py` recorre el AST de todos los módulos de `src/` y falla
si alguno lo importa.

### Correspondencia con los requisitos

| # | Requisito | Dónde está |
| --- | --- | --- |
| 1 | Conexión con un LLM a nivel de API | `src/host/llm/gemini.py` |
| 2 | Mantener contexto en la sesión | `src/host/conversation.py` |
| 3 | Mantener y mostrar el log de interacciones MCP | `src/core/mcp/protocol_log.py`, pestaña «Actividad MCP» |
| 4 | Servidores oficiales Filesystem y Git | `config/servers.json`, `scripts/demo_official_servers.py` |
| 5 | Servidor MCP propio, local | `src/servers/pharmacy/` |
| Extra | Interfaz de usuario | `src/tui/` |

---

## 2. Inciso 8 — Especificación del servidor MCP desarrollado

La especificación completa, con todos los parámetros y ejemplos, está en
[pharmacy-mcp-server.md](pharmacy-mcp-server.md). Esta sección resume lo esencial.

### 2.1 Identidad y transporte

| Campo | Valor |
| --- | --- |
| `serverInfo.name` | `pharmacy-mcp-server` |
| `serverInfo.version` | `1.0.0` |
| Protocolo | JSON-RPC 2.0 sobre MCP, versión `2025-11-25` |
| Versiones aceptadas | `2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05` |
| Transporte | **stdio**: un objeto JSON por línea, UTF-8, delimitado por `\n` |
| Capacidades | `{"tools": {"listChanged": false}}` |

### 2.2 Sobre los «endpoints»

En esta entrega el servidor **no tiene endpoints de red**. Con transporte stdio
el anfitrión lanza el servidor como proceso hijo y el canal es el par
stdin/stdout de ese proceso; no hay socket, ni puerto, ni URL, y por eso tampoco
hay nada que capturar con Wireshark todavía. Lo que cumple el papel de endpoint
es el **método** del mensaje JSON-RPC:

| Método | Tipo | Función |
| --- | --- | --- |
| `initialize` | petición | Negociación de versión e intercambio de capacidades |
| `notifications/initialized` | notificación | El cliente confirma el handshake; no se responde |
| `ping` | petición | Verificación de vida, atendida en cualquier momento |
| `tools/list` | petición | Devuelve las siete herramientas con su JSON Schema |
| `tools/call` | petición | Ejecuta una herramienta |

Cualquier otro método se responde con `-32601 Method not found`.

En la segunda entrega el mismo servidor se expone sobre Streamable HTTP en Cloud
Run, y ahí sí aparece un endpoint real (`POST /mcp`). La arquitectura ya está
preparada: `MCPServer.handle_message(payload) -> payload` no conoce el
transporte, por lo que el paso de `stdin → handle_message → stdout` a
`body HTTP → handle_message → body HTTP` no toca la lógica de las herramientas.

### 2.3 Herramientas y parámetros

| Herramienta | Obligatorios | Opcionales | Modifica datos |
| --- | --- | --- | --- |
| `list_branches` | — | — | no |
| `search_medicines` | uno de `query` / `symptom` | `prescription_filter`, `limit` | no |
| `get_medicine_details` | `sku` | — | no |
| `check_inventory` | — | `sku`, `branch_id` | no |
| `verify_prescription` | `folio` | `patient_id` | no |
| `create_purchase_order` | `branch_id`, `customer_name`, `items` | `customer_id`, `prescription_folio` | **sí** |
| `get_order` | `order_id` | — | no |

`items` es un arreglo de objetos `{sku: string, quantity: integer ≥ 1}`.

Cada herramienta declara además sus `annotations`. La que importa es
`readOnlyHint`: el anfitrión la lee para decidir qué operaciones requieren
confirmación del usuario, sin ninguna lista de nombres codificada.

### 2.4 Reglas de negocio que aplica el servidor

`create_purchase_order` valida **todo antes de escribir nada**, de modo que una
orden rechazada no deja rastro:

1. La sucursal existe y todos los SKU existen.
2. Hay existencias suficientes en esa sucursal; si otra sí las tiene, el mensaje
   de error la nombra.
3. Todo medicamento con `requires_prescription` exige un folio de receta.
4. La receta debe estar vigente, no anulada y con cantidades pendientes
   suficientes.
5. Los totales se calculan con IVA del 12 %, redondeando a centavos.

Si todo pasa, en **una sola transacción SQLite** se inserta la orden con sus
líneas, se descuenta el inventario, se aumentan las cantidades despachadas de la
receta y la receta se cierra cuando ya no queda nada pendiente.

### 2.5 Modelo de errores

El servidor distingue dos canales de falla, y la distinción es deliberada:

**Errores de protocolo** — objeto `error` de JSON-RPC. La llamada estaba mal
formada.

| Código | Significado |
| --- | --- |
| `-32700` | La línea no era JSON válido |
| `-32600` | No es un mensaje JSON-RPC 2.0 |
| `-32601` | Método inexistente |
| `-32602` | Herramienta desconocida o argumento faltante o de tipo incorrecto |
| `-32603` | Error interno |
| `-32002` | Se llamó algo antes de `initialize` (definido por esta aplicación) |

**Errores de herramienta** — respuesta **exitosa** cuyo resultado trae
`isError: true` y un mensaje en español. Son resultados de negocio («la receta
venció», «no hay existencias») que el modelo debe leer y usar para reconducir la
conversación. Convertirlos en errores de protocolo rompería el diálogo en vez de
guiarlo.

Antes de ejecutar cualquier handler, los argumentos se validan contra el
`inputSchema` de la herramienta: claves obligatorias, tipos, enumeraciones,
mínimos y elementos de arreglos anidados.

### 2.6 Ejemplo de intercambio

```json
--> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"pharmacy-mcp-host","version":"0.1.0"}}}
<-- {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"pharmacy-mcp-server","version":"1.0.0"}}}
--> {"jsonrpc":"2.0","method":"notifications/initialized"}
--> {"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"create_purchase_order","arguments":{"branch_id":"SUC-01","customer_name":"Ana Lucia Morales","items":[{"sku":"MED-005","quantity":1}]}}}
<-- {"jsonrpc":"2.0","id":5,"result":{"content":[{"type":"text","text":"Los siguientes medicamentos requieren receta medica: Amoxicilina 500 mg..."}],"isError":true}}
```

### 2.7 Modelo de datos

SQLite con nueve tablas, construida en el primer arranque desde
`data/pharmacy_seed.json`: sucursales, medicamentos, síntomas por medicamento,
inventario por sucursal, recetas, ítems de receta, órdenes e ítems de orden.

El conjunto de datos tiene 3 sucursales, 16 medicamentos (6 con receta, 2 de
ellos controlados), 48 filas de inventario y 5 recetas que cubren los casos
interesantes: vigente, vencida, ya despachada, parcialmente despachada y
controlada. Algunas existencias están en cero a propósito, para poder demostrar
la ruta de «no hay en esta sucursal, pero sí en aquella».

---

## 3. La interfaz

![Conversación](img/tui-conversacion.svg)

La pantalla tiene dos regiones. La conversación ocupa la columna izquierda y más
ancha porque es la tarea principal; el log del protocolo, los servidores y las
herramientas viven en pestañas a la derecha, donde se consultan sin interrumpir
el chat.

Decisiones de color, del curso de HCI:

- **Verde azulado** para el asistente y los estados sanos. El verde se lee como
  seguridad y cuidado en un contexto de salud, y el matiz azulado lo separa del
  verde de «éxito».
- **Azul** para el usuario. Azul y verde azulado están lo bastante lejos en tono
  para distinguir a los dos interlocutores de un vistazo, y además difieren en
  luminosidad, así que la distinción sobrevive a las formas comunes de daltonismo.
- **Ámbar reservado a un único significado**: una acción que va a modificar
  datos y necesita confirmación. Nada más en la interfaz es ámbar, de modo que el
  color por sí solo transmite la advertencia.
- **Rojo solo para errores**, y en tono apagado: el rojo puro se lee como alarma
  y cansa sobre fondo oscuro.
- Los paneles técnicos son deliberadamente de bajo contraste: son información
  secundaria y no deben competir con la conversación.

La actividad de las herramientas se muestra **dentro del chat**, atenuada. El
usuario ve qué está haciendo el asistente mientras espera, que es la diferencia
entre una pausa explicada y una aplicación que parece colgada.

![Confirmación](img/tui-confirmacion.svg)

El diálogo de confirmación aparece antes de cualquier herramienta que escriba.
Sigue tres reglas: muestra **qué** va a pasar y no solo **que** va a pasar
(lista los argumentos completos, así se confirma una orden concreta); la opción
reversible es la que tiene el foco al abrirse y Escape cancela; y el título dice
la consecuencia, tomada del `title` y la `description` que publica el propio
servidor.

---

## 4. Dificultades y cómo se resolvieron

**El servidor Git oficial no puede crear repositorios.** El escenario que sugiere
el enunciado empieza con «cree un repositorio», pero `mcp-server-git` expone doce
herramientas y `git_init` no está entre ellas en ninguna versión publicada; se
revisaron 0.6.2, 2025.9.25, 2026.1.14 y la actual, además del código fuente
instalado. La solución fue que el anfitrión prepare los repositorios vacíos
dentro de su área de trabajo, automáticamente al arrancar o con el comando
`/workspace`, y que **todas** las operaciones de git reales sigan pasando por el
servidor oficial.

**`npx` no se puede lanzar por nombre en Windows.** `create_subprocess_exec` no
consulta `PATHEXT` como lo hace una shell, así que arrancar el servidor
Filesystem fallaba con `WinError 2`. Se resuelve resolviendo el programa por PATH
antes de lanzarlo, lo que en Windows devuelve `npx.CMD`.

**Distinguir un error de negocio de un error de protocolo.** La primera versión
devolvía «se requiere receta» como error JSON-RPC, y eso rompía el turno del
modelo. Separar los dos canales —`isError: true` dentro de una respuesta exitosa
para lo primero, objeto `error` para lo segundo— fue el cambio que hizo que el
chatbot pueda pedir el folio en vez de fallar.

**Marcado de Rich contra los datos.** Los argumentos de las herramientas están
llenos de corchetes (`[MED-001]`, arreglos JSON) y Rich los interpretaba como
etiquetas, dejando escapar un `[/dim]` literal en pantalla. Se corrigió armando
esas líneas con objetos `Text` en vez de con marcado.

**Despachar contra una receta que también incluye medicamentos de venta libre.**
Al escribir las pruebas apareció el caso: si la receta lista ambroxol y el
cliente compra tres frascos, no debe fallar por «exceder la receta», pero el
frasco recetado sí debe marcarse como despachado. Obligó a separar «validar
contra la receta» de «descontar de la receta».

---

## 5. Inciso 10 — Conclusiones

**MCP resuelve un problema de integración, no de inteligencia.** Lo que aporta el
protocolo es que la herramienta se describe una sola vez, en JSON Schema, y sirve
para cualquier modelo. En este proyecto eso se ve en un archivo concreto,
`src/host/llm/schema.py`: el servidor de farmacia no sabe que existe Gemini, y
cambiar de proveedor significa reescribir ese archivo y ningún otro. Es
exactamente la interoperabilidad que no existía cuando cada empresa definía su
propio formato de funciones.

**Implementar el protocolo a mano enseña más que usar el SDK.** Escribir la
correlación de identificadores, el handshake y el manejo de errores obliga a
entender por qué JSON-RPC 2.0 separa peticiones de notificaciones: una
notificación no tiene `id` precisamente porque nadie va a responderla, y
confundirlas deja al cliente esperando para siempre. También obliga a entender
que las respuestas se emparejan por `id` y no por orden de llegada, que es lo que
permite tener varias llamadas en vuelo al mismo tiempo.

**La prueba de que la implementación es correcta fue conectarse a servidores
ajenos.** Mientras el cliente solo hablaba con nuestro propio servidor, ambos
podían compartir el mismo malentendido del protocolo. Al conectar el cliente
escrito a mano contra el Filesystem y el Git de Anthropic, sin cambiar una línea,
quedó demostrado que lo implementado sigue la especificación y no una convención
propia.

**El protocolo trae información de diseño que conviene aprovechar.** La
anotación `readOnlyHint` permitió que la compuerta de confirmación funcione con
cualquier servidor sin una lista de nombres peligrosos: cuando se agregaron los
dos servidores oficiales, `write_file`, `edit_file`, `git_commit` y `git_reset`
quedaron protegidos automáticamente porque ellos mismos se declaran así. Lo mismo
ocurre con el campo `instructions` del `initialize`, que deja que el servidor le
enseñe al modelo cómo usarlo sin tocar el código del anfitrión.

**Separar el transporte desde el principio ahorró trabajo.** La decisión de poner
una interfaz `Transport` entre el cliente y stdio se tomó pensando en la segunda
entrega, y ya pagó: el servidor de farmacia, sus herramientas y sus reglas de
negocio no van a cambiar cuando el mismo servidor se publique sobre HTTP en Cloud
Run; solo cambia la clase que mueve los bytes.

**Comentario sobre el proyecto.** El enunciado prohíbe usar SDKs de MCP, y esa
restricción es la parte más valiosa del ejercicio: es lo que convierte un tema de
moda en un problema de redes con capas, formatos y estados. La parte que quedó
pendiente de verificar —la llamada real a la API de Gemini— está aislada detrás
de una interfaz y probada con un modelo simulado, de modo que el resto del
sistema ya está demostrado de punta a punta.

---

## 6. Estado y pendientes

Lo que está verificado con pruebas automáticas (134 pruebas): el formato JSON-RPC,
el ciclo de vida de la sesión, el servidor de farmacia y sus reglas, el bucle de
herramientas del agente, las sesiones reales contra los dos servidores oficiales
y la interfaz.

Lo único no verificado en vivo es la llamada a la API de Gemini, porque al cierre
de esta entrega todavía no se contaba con la llave. El camino está cubierto por
pruebas con un modelo simulado, y `scripts/check_gemini.py` lo comprueba de un
solo golpe en cuanto la llave esté disponible.

Para la segunda entrega quedan: publicar el mismo servidor sobre Streamable HTTP
en Google Cloud Run (inciso 6), capturar la comunicación con Wireshark y
clasificar los mensajes JSON-RPC (inciso 7), el análisis por capas de enlace,
red, transporte y aplicación (inciso 9) y la ampliación de los incisos 8 y 10 con
lo remoto.
