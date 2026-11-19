# J.A.R.V.I.S.

Asistente de voz para Windows: se activa con «Jarvis» o una tecla, entiende español, responde con voz de ElevenLabs y actúa sobre el PC.

| Pieza | Tecnología |
|---|---|
| Palabra de activación | «Jarvis» (reconocida con Whisper) o «Hey Jarvis» (openWakeWord), todo en local |
| Oído | faster-whisper `small` en CPU, español |
| Cerebro | Claude Haiku 5.5 con herramientas |
| Voz | ElevenLabs en streaming, frase a frase |

## Puesta en marcha

0. Instala el comando `jarvis` para poder usarlo desde cualquier carpeta:

   ```powershell
   uv tool install --python 3.11 --editable C:\J.A.R.V.I.S
   ```

   El comando sigue usando esta carpeta para el código, `config.toml`, `.env`, `memory/` y `data/`, estés donde estés. Los cambios en el código se aplican solos; si cambian las dependencias de `pyproject.toml`, repite el comando con `--reinstall`. Para quitarlo: `uv tool uninstall jarvis`. Sin instalarlo, todo funciona igual con `uv run jarvis` desde esta carpeta.

1. Copia `.env.example` como `.env` y pega tus claves de [Anthropic](https://console.anthropic.com/settings/keys) y [ElevenLabs](https://elevenlabs.io/app/settings/api-keys). Si restringes los permisos de la clave de ElevenLabs, necesita al menos texto a voz y lectura de voces (`voices_read`).
2. La búsqueda web viene activada de serie en la API de Anthropic; no hay que hacer nada. Si alguien la desactivó en tu organización, se reactiva en [Settings → Capabilities](https://platform.claude.com/settings/capabilities). Cada búsqueda cuesta 0,01 USD (10 USD por 1.000), aparte de los tokens. Si está desactivada, Jarvis sigue funcionando sin acceso a internet.
3. Elige voz: `jarvis voices` lista las de tu cuenta, con las españolas primero. Copia el ID en `config.toml` (`[voice] voice_id`). Si lo dejas vacío se usa la primera voz en español de la cuenta. `jarvis voices --library` enseña voces nativas que puedes añadir desde la web de ElevenLabs.
4. Pon tu ciudad en `config.toml` (`[user] city`) para el tiempo y el briefing.
5. Comprueba que todo funciona: `jarvis selftest`.
6. Arranca: `jarvis`.

## Uso

| Comando | Qué hace |
|---|---|
| `jarvis` | Asistente de voz con su ventana (HUD). Da el resumen del día en el primer arranque de cada día. |
| `jarvis --no-ui` | Igual, sin ventana: solo voz y consola. |
| `jarvis run --no-briefing` | Igual, sin resumen. |
| `jarvis briefing` | Arranca dando el resumen del día. |
| `jarvis text` | Conversación por teclado, sin audio. Mismo cerebro y herramientas. |
| `jarvis selftest` | Comprueba micrófono, cerebro, voz, palabra de activación y Whisper. |
| `jarvis selftest --offline` | Solo el oído, con las voces de Windows. No gasta créditos ni necesita claves. |
| `jarvis selftest --listen` | Prueba real del micrófono: dices una frase y muestra lo que ha entendido. |
| `jarvis selftest --wake` | Prueba real de la palabra de activación: dices «Jarvis» varias veces y muestra qué ha entendido cada vez y si se habría activado. |
| `jarvis voices` | Lista las voces de ElevenLabs. |
| `jarvis calendar setup` | Conecta tu Google Calendar paso a paso (ver [más abajo](#google-calendar-y-avisos-en-el-móvil)). |
| `jarvis calendar sync` | Comprueba la conexión con Google Calendar y envía ahora los cambios pendientes. |

Añade `-v` antes del comando (`jarvis -v text`) para ver las llamadas a herramientas y el consumo de tokens.

Di **«Jarvis»** o pulsa **Ctrl+Alt+J**. Un pitido ascendente indica que escucha; uno descendente, que ha terminado de oírte. Si Jarvis acaba con una pregunta, el micrófono se abre solo. Para interrumpirle mientras habla, pulsa la tecla o di «Jarvis».

Hay dos formas de llamarle por voz:

- **«Jarvis»**, a secas y con tu pronunciación normal. Dilo solo y espera al pitido, que tarda alrededor de un segundo: Jarvis transcribe con Whisper las palabras sueltas que oye y se activa si es su nombre. Si lo dices de corrido con una orden larga («Jarvis, pon música de Estopa») no lo reconoce. Solo funciona mientras está en espera.
- **«Hey Jarvis»**. Responde al instante y también sirve para interrumpirle mientras habla.

### La ventana (HUD)

`jarvis` abre una ventana propia que muestra lo que está haciendo. Cerrarla cierra Jarvis.

- **Centro:** el orbe cambia de color y ritmo según el estado (en espera, escuchando, pensando, hablando) y debajo aparece lo que te ha entendido y lo que responde. Pulsar el orbe equivale a decir «Jarvis»; si está hablando, le interrumpe.
- **Sistema y servicios:** CPU, memoria y red del PC, y si el micrófono, el modelo de lenguaje, la voz y Google Calendar están funcionando. Calendario aparece como «sin configurar» hasta que lo conectes.
- **Sonando:** carátula, título y artista de lo que esté sonando, en Spotify o en cualquier otro reproductor. Solo aparece mientras suena algo; al pausar desaparece.
- **Hoy:** tus tareas de hoy y las atrasadas, leídas de `memory/tasks.md`.
- **Consumo:** lo gastado y lo que queda en los dos servicios de pago (ver [Consumo](#consumo)).
- **Actividad:** las últimas acciones (`[ok]` hecha, `[..]` en curso, `[!!]` fallida).
- **Escribe un comando…:** lo que escribas se lo dices a Jarvis sin usar la voz; responde hablando. Con barra son comandos de consola (`/breve`, `/estado`).
- **Próximamente:** el panel Dispositivos está en el diseño pero aún no hace nada.

La ventana es Edge (o Chrome) en modo aplicación, con un perfil propio en `data/hud-profile`; recuerda el tamaño y la posición que le des. La página la sirve Jarvis solo para tu PC (`127.0.0.1`) y exige una clave aleatoria por sesión, de modo que otras páginas web no pueden enviarle órdenes. Para desactivarla siempre: `[ui] enabled = false` en `config.toml`.

### Cuánto habla (y cuánto gasta)

Con Jarvis en marcha, escribe en la consola una de estas palabras y pulsa Intro:

| Comando | Qué hace | Gasto medido frente a `normal` |
|---|---|---|
| `breve` | Lo imprescindible, en una frase. | Aproximadamente la mitad |
| `normal` | Una o dos frases. | — |
| `detallado` | Explicaciones completas. | Unas tres veces más |

También valen `escueto`, `corto`, `extendido` y `extenso`. `estado` muestra el modo actual, `consumo` lo gastado y `ayuda` la lista. El cambio se aplica desde la siguiente respuesta y se recuerda al reiniciar; el valor inicial está en `config.toml` (`[brain] verbosity`). En `jarvis text` los comandos llevan barra: `/breve`.

El gasto son sobre todo caracteres de ElevenLabs, que es lo que más pesa, y en menor medida tokens de salida de Claude.

### Consumo

El panel **Consumo** de la ventana se actualiza después de cada respuesta:

- **Voz · ElevenLabs:** caracteres gastados y límite de tu plan en este periodo, cuántos quedan y cuándo se renueva. Son los datos de tu cuenta, así que incluyen lo que gastes fuera de Jarvis. Si la clave tiene los permisos restringidos, necesita el de lectura de usuario (`user_read`).
- **Modelo · Claude:** lo que Jarvis ha gastado este mes, en dólares y en tokens. Anthropic no deja consultar el saldo con una clave normal, así que Jarvis cuenta los tokens de cada respuesta y les aplica el precio de tarifa del modelo. Para ver cuánto queda, mira tu saldo en la [consola de Anthropic](https://console.anthropic.com/settings/billing) y anótalo con `saldo 12,50` en la consola (o `/saldo 12,50` en la ventana): desde ese momento Jarvis le va restando lo que gasta.

El saldo de Claude es una estimación: no cuenta lo que gastes con la misma clave fuera de Jarvis. Vuelve a anotarlo cuando recargues o si ves que se desvía. `consumo` muestra el mismo resumen en la consola. Los totales se guardan en `data/usage.json`.

### Qué puedes pedirle

- **PC:** «abre Chrome», «cierra Spotify», «sube el volumen al cuarenta», «¿por qué va lento el ordenador?»
- **Archivos:** «busca el PDF de la factura de la luz de este mes y muévelo a Impuestos».
- **Pantalla:** «¿qué significa este error?», «¿dónde está la opción de exportar?»
- **Memoria:** «apunta que el wifi del estudio va mal», «recuérdame mañana a las diez revisar el presupuesto», «¿qué tengo pendiente?»
- **Música:** «pon Thunderstruck de AC/DC», «pon algo de Estopa», «pon mi lista Bandas Sonoras», «añade Bohemian Rhapsody a la cola», «¿qué está sonando?», «pausa la música». Usa la aplicación de Spotify del PC con tu sesión (la abre si está cerrada); no necesita claves. Requiere la versión de Microsoft Store, que trae la herramienta `spotify_cli.exe`.
- **Modos:** «vamos a crear un modo trabajo» (Jarvis te pregunta qué abrir y cerrar), «activa el modo trabajo», «añade Notion al modo trabajo». Un modo puede abrir y cerrar aplicaciones, abrir webs, fijar el volumen, poner música en Spotify, pausar o pasar de canción y decir una frase.
- **Web:** «busca qué gráficas rondan los cuatrocientos euros y déjame una tabla en el escritorio».
- **Tiempo:** «¿va a llover mañana?»

## Google Calendar y avisos en el móvil

Jarvis puede copiar las tareas con fecha a un calendario llamado «Jarvis» dentro de tu Google Calendar. Así el teléfono avisa a la hora aunque el PC esté apagado. Es opcional y se conecta una sola vez:

```powershell
jarvis calendar setup
```

El comando abre el editor de Google Apps Script, te deja en el portapapeles un pequeño script para pegar y te guía hasta obtener una dirección web que pegas de vuelta en la terminal. Ese script vive en tu cuenta de Google y es el único que toca tu calendario; no hace falta Google Cloud ni caduca. Al terminar, comprueba en la app de Google Calendar del móvil que el calendario «Jarvis» está marcado.

| Tarea | En Google Calendar |
|---|---|
| Con día y hora («recuérdame mañana a las diez…») | Evento a esa hora, con aviso en el móvil. |
| Solo con día | Evento de todo el día. Para que avise, elige en los ajustes del calendario «Jarvis» la notificación de los eventos de todo el día (por ejemplo, el mismo día a las 9:00). |
| Sin fecha | No se envía: se queda en `memory/tasks.md`. |

- Al marcar una tarea como hecha, su evento se borra. Si cambias la fecha a mano en `memory/tasks.md`, el evento se mueve.
- Sin conexión, la tarea se guarda igual y se envía cuando vuelva internet. `jarvis calendar sync` lo fuerza y dice si la conexión funciona.
- Solo va de Jarvis al calendario: si mueves o borras un evento en el móvil, la tarea de Jarvis no cambia.
- La antelación del aviso y la duración del evento están en `config.toml` (`[calendar]`). Con `enabled = false` se desactiva.
- Para desconectarlo del todo, borra las dos líneas `JARVIS_CALENDAR_…` de `.env` y, en [script.google.com](https://script.google.com), el proyecto del script.

## Seguridad

- Cerrar aplicaciones, mover o renombrar archivos, sobrescribir un archivo y borrar un modo piden confirmación por voz. La confirmación la exige el código, no depende de que el modelo decida preguntar. Solo un «sí» claro cuenta.
- Los pasos de un modo se ejecutan sin preguntar uno a uno: los aprobaste al definirlo.
- Al cerrar una aplicación primero se pide a sus ventanas que se cierren. Si queda una ventana abierta (normalmente «¿guardar cambios?») no se fuerza.
- Las herramientas de archivos solo tocan tu carpeta de usuario (configurable en `[files] allowed_roots`) y nunca `AppData`, `.ssh`, `.env` ni similares. Mover nunca sobrescribe.
- No hay herramienta para borrar archivos ni para ejecutar comandos arbitrarios.
- Lo que Jarvis lee (archivos, capturas de pantalla, tu voz transcrita) se envía a la API de Anthropic; lo que dice, a ElevenLabs. El audio del micrófono se transcribe en local y no sale del PC.
- Si conectas Google Calendar, el texto y la fecha de las tareas con fecha se envían a tu cuenta de Google. La dirección del script y su clave están en `.env`: quien tuviera las dos podría crear o borrar eventos en el calendario «Jarvis», y en ningún otro.

## Tus datos

- `memory/notes/AAAA-MM-DD.md`: notas del día.
- `memory/tasks.md`: tareas, en formato `- [ ] texto ⏰ 2026-11-15 10:00`. Puedes editarlo a mano o abrir `memory/` como bóveda de Obsidian.
- `memory/profile.md`: lo que Jarvis recuerda de ti. Se carga al empezar cada conversación.
- `data/modes.yaml`: modos de trabajo.
- `data/usage.json`: tokens de Claude gastados por mes y el saldo que hayas anotado.
- `data/calendar_sync.json`: qué tareas están ya en Google Calendar. Si se borra, se reconstruye sin duplicar eventos.
- `data/logs/jarvis.log`: registro.

`memory/`, `data/` y `.env` no se suben a git.

## Ajustes frecuentes (`config.toml`)

| Síntoma | Ajuste |
|---|---|
| Se activa solo con ruidos | Sube `[ears] wake_threshold` (0.6–0.7). |
| No reacciona a «Jarvis» | Dilo solo, con una pausa antes y después. `jarvis selftest --wake` enseña qué entiende cada vez. |
| No reacciona a «Hey Jarvis» | Baja `[ears] wake_threshold` (0.3–0.4) o usa la tecla. |
| Se activa cuando alguien dice «Jarvis» en una conversación, o gasta CPU con la tele puesta | `[ears] wake_by_name = false`: vuelve a activarse solo con «Hey Jarvis». |
| Te corta antes de acabar la frase | Sube `[ears] silence_ms` (1100–1400). |
| Tarda en notar que has terminado | Baja `[ears] silence_ms` (700). Mira en la consola si pone «cortado por duración máxima»: entonces hay un ruido de fondo tan alto como tu voz. |
| Transcribe mal | `[ears] whisper_model = "medium"` (más lento) . |
| Responde lento | `[ears] whisper_model = "base"`, o `[voice] model = "eleven_flash_v2_5"`. |
| Quieres respuestas más meditadas | `[brain] effort = "medium"`. |

## Arranque automático

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1          # instalar
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove  # quitar
```

## Desarrollo

```powershell
uv run pytest        # lógica de herramientas, memoria, modos y bucle del agente
uv run ruff check .
```

La estructura está en `jarvis/`: `audio/` (micrófono, activación, grabación, STT, TTS), `brain/` (bucle del agente, prompt y herramientas), `app.py` (máquina de estados de la voz) y `calendar_sync.py` con `calendar_script.gs` (copia de las tareas a Google Calendar).

## Fuera de esta versión

Domótica (Home Assistant), correo, leer los eventos de tu calendario, borrado de archivos, silenciar las notificaciones de Windows y voz de respaldo sin conexión.
