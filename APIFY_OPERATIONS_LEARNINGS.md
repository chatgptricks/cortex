# Aprendizajes de operación — Cortex / Sentient Dash

Documento de reglas duras aprendidas **rompiendo cosas y pagando por errores** en este
proyecto. Cada regla existe porque algo falló de verdad, con el costo real anotado.
Leer antes de tocar Apify, el scheduler, la base de datos o de desplegar.

---

## 1. Nunca usar `run-sync-get-dataset-items` para scrapes grandes

**Costo del error: ~$30 perdidos, y lo repetí una segunda vez ($0.21).**

Apify tiene dos formas de correr un actor:

| Método | Qué hace | Cuándo falla |
|---|---|---|
| `run-sync-get-dataset-items` | Mantiene **una conexión HTTP abierta** durante todo el scrape | Se corta en scrapes largos y devuelve 408 |
| Start + poll + fetch dataset | Arranca el run, consulta estado con llamadas cortas, baja el dataset al final | No se corta: ninguna conexión dura más de ~30s |

**El modo de falla es silencioso y caro:** el run **completa y se cobra** del lado de
Apify, pero nosotros recibimos un timeout y no guardamos nada. Se paga el trabajo
completo y se pierde el resultado.

Cómo se detectó: en la auditoría de gasto aparecieron **cargos idénticos repetidos**
($1.84 × 7, $0.98 × 7, $2.61 × 3...). Cargos iguales = el mismo scrape corrido varias
veces porque los resultados nunca llegaron. 57% del gasto del ciclo era esto.

> **Regla:** todo scrape que pueda pasar de ~1 minuto usa `_run_apify_actor_and_fetch()`.
> `_fetch_apify_items()` solo para llamadas chicas y garantizadamente rápidas (preview de
> perfil, lote de pocos posts).

**Lo repetí después de arreglarlo.** Escribí `scrape_missing_enrichment()` con la función
frágil por inercia, teniendo la resiliente ya escrita. Al escribir código nuevo que
llame a Apify, verificar explícitamente qué función se está usando.

---

## 2. Antes de re-scrapear, revisar si el dato ya está pagado

**Ahorro concreto: ~$4.60 en un caso, $0.21 en otro.**

Apify **retiene el dataset de cada run** (en este proyecto ninguno había expirado ni
después de 381 runs). Si un scrape completó pero los datos no llegaron a la base, se
recuperan **gratis** bajando ese dataset.

Herramientas dejadas para esto:
- `GET /api/admin/apify/runs` — lista runs con estado y costo
- `POST /api/admin/apify/import-run/{handle}` — importa posts de un run terminado
- `POST /api/admin/apify/enrich-from-run/{run_id}` — enriquece desde un run específico
- `POST /api/admin/apify/abort-run/{run_id}` — corta un run en vuelo para dejar de gastar

> **Regla:** ante datos faltantes, el primer paso es **mirar el historial de runs**, no
> lanzar un scrape. Si hay un run `SUCCEEDED` que cubre esos datos, es gratis.

Esta regla salió de una observación del usuario, no mía: mi reflejo fue relanzar el
backfill (o sea pagar de nuevo). Ya llevaba $1.85 gastados cuando me lo señaló.

---

## 3. Elegir el método de scrape según la forma del faltante

**Costo del error: 3-4 horas de proceso en lugar de ~15 minutos.**

| Faltante | Método correcto | Ritmo medido |
|---|---|---|
| Muchos posts de **una** cuenta | Scrape de perfil (`resultsType=posts`) | **1.9 posts/s** |
| Pocos posts **dispersos** entre cuentas | Por URL (`resultsType=details`) | **0.17 posts/s** |

El scrape de perfil pagina el feed (~12 posts por request). El de URL **navega a cada
post por separado**. Diferencia: **~11x**.

Usé el de URL para 1,953 posts de una sola cuenta porque venía de resolver 156 dispersos.
Herramienta correcta para el problema anterior, equivocada para el nuevo.

> **Regla:** contar el faltante **por cuenta** antes de elegir. Concentrado en una cuenta
> → perfil. Disperso → URL.

---

## 4. Nunca desplegar con trabajo en vuelo

**Costo del error: un backfill de 1,700 posts perdido, ~$4.60 (recuperado después).**

Render **reinicia el proceso en cada deploy**, matando cualquier hilo en segundo plano:
backfills, barridos de OCR, enriquecimientos. Hice ~6 deploys en una hora y maté el
backfill de `therundownai` sin darme cuenta.

> **Regla:** antes de `git push`, verificar que no haya nada corriendo
> (`/ocr/status`, `/accounts/backfill-status`, `/apify/enrich-status`).
> Si hay, esperar. Si es urgente, avisar que se va a interrumpir.

Corolario: los trabajos largos deben **guardar progreso en la base**, no en memoria, para
poder retomar. Ver `ocr_checked`, `enriched_at`, `scheduler_state`.

---

## 5. Estado del scheduler en la base, nunca en memoria

**Costo del error: el ciclo diario completo re-corriendo en cada deploy.**

Los marcadores de "última corrida" estaban en variables de módulo. Cada reinicio los
borraba, así que el job **se disparaba de nuevo al arrancar** — con ~15 deploys en un día,
eso es gasto de Apify repetido en silencio.

Solución: tabla `scheduler_state`. Verificado que tras un deploy el bucket y la fecha
sobreviven y el job **no** vuelve a correr.

> **Regla:** cualquier "esto ya se hizo" debe persistirse. Reclamar el turno **antes** de
> ejecutar, para que un crash a mitad no lo deje sin reclamar y se repita.

---

## 6. Verificar el resultado, no el código

Errores que **compilaron y desplegaron bien**, y solo aparecieron al mirar datos reales:

- **`Path` sin importar** en `apify_sync.py` → el barrido de OCR murió a los 200 covers
  con `name 'Path' is not defined`. Habría roto también el job horario del scheduler, en
  silencio, para siempre.
- **`sum()` con `None`** → HTTP 500 en todo el dashboard al pasar los likes a NULL.
- **Columna faltante en el SELECT** → el OCR se guardaba bien pero `ocrText` llegaba
  vacío a la API. El dato existía; la consulta no lo pedía.
- **Match por `ownerUsername`** → 92 items bajados, 0 aplicados. Las cuentas se
  repostean, así que el payload trae al autor original, no la cuenta nuestra.

> **Regla:** después de desplegar, consultar el endpoint real y **mirar los valores**.
> "Compila" y "desplegó" no son verificación.

---

## 7. Medir antes de optimizar

Dos casos donde mi intuición estaba mal y los datos lo mostraron:

**Compresión de covers.** Asumí que re-comprimir JPEG ahorraría. Medido: **~8%** a q75, y
a q80+ los archivos **crecían** (los JPEG de Instagram ya están optimizados). WebP a q82
dio **31%** a la misma resolución. Sin medir, habría enviado algo inútil.

**Paralelismo del OCR.** Lancé 3 hilos esperando 3x. Resultado: **peor** (13 → 7
covers/min). El worker de Modal tiene `MAX_CONTAINERS = 1`, así que serializa las
peticiones y el paralelismo solo agregó cola.

> **Regla:** medir sobre datos reales antes y después. Si empeora, decirlo y revertir.

También: verificar el tamaño real de renderizado antes de bajar resolución. El preview
del sidebar renderiza a 531×843 CSS px → **~1062px en retina**, así que reducir a 810px
lo habría degradado visiblemente.

---

## 8. No inventar datos para llenar huecos

Cuando Instagram oculta los likes, el código viejo guardaba **500** fijo. Eso:
- hacía ver posts sin dato como si tuvieran engagement real
- ensuciaba totales y promedios
- **marcaba posts como HOT falsamente**: 500 likes / 1h = 500/hr, cruzando el umbral de
  cualquier cuenta con threshold ≤500. Y el flag HOT es de una sola vez, o sea permanente.

Ahora se guarda `NULL` y la UI muestra "—".

> **Regla:** dato desconocido = `NULL`, y la UI lo dice. Nunca un placeholder que se
> confunda con un valor real. Y **excluirlo de promedios**, no contarlo como 0.

Los 152 posts con el 500 histórico eran indistinguibles de un 500 real. Se limpiaron
razonando sobre la distribución (el resto de los likes está disperso; 152 exactamente en
500 no es casualidad), y dejando claro el criterio.

---

## 9. Claves únicas: las cuentas se repostean

**21 shortcodes existen bajo dos cuentas** (`costarica` repostea `traselveloreal`,
`trends` repostea `ainterestingupdate`).

Esto rompió dos cosas:
- **Keys de React duplicadas** → el reordenamiento queda indefinido y esas tarjetas
  quedaban clavadas arriba en *cualquier* orden. Se veía como "el filtro de fechas no
  funciona".
- **Selección** → clic en un post podía abrir el de la otra cuenta.

> **Regla:** la identidad de un post es `cuenta + shortcode`, nunca el shortcode solo.
> Aplica a keys de React, selección, dedupe y nombres de archivo.

**Excepción deliberada:** al *enriquecer* se matchea por shortcode solo, porque el payload
describe un post real de Instagram y ambas filas merecen esos datos. Al *insertar* sí se
valida la cuenta, para no meter posts ajenos bajo el handle equivocado.

---

## 10. Guardar todo lo que ya se pagó

Apify devuelve ~36 campos por post; se guardaban **8**. Se tiraban views y plays de reels,
duración, cantidad de slides del carrusel, hashtags, audio, colaboradores, alt text.

Con 2,158 videos en la base, se juzgaban reels **solo por likes**. El dato mostró por qué
importa: un reel con 234,991 views / 133,700 likes convierte a **1.8x**, y otro con 41,566
views / 1,109 likes está en **37.5x**. Diagnósticos opuestos, indistinguibles sin views.

Solución en dos capas: `raw_json` con el payload completo (nada se pierde nunca más) +
columnas propias para lo que necesita filtrarse/ordenarse.

> **Regla:** si ya se pagó por el dato, **guardarlo entero**, aunque no se use hoy.
> Re-scrapear después cuesta plata; una columna TEXT no.

---

## 11. Preservar el trabajo ya hecho al migrar

Al mover `chatgptricks` a `dashboard_posts` se preservaron explícitamente: el OCR
(`hook_text` + `ocr_checked=1`, para no re-procesar y re-pagar), estado HOT, covers,
fechas originales.

También: **dry-run primero**. El primer filtro tomaba solo `section='single'` (47 posts) y
habría dejado 2,361 históricos fuera del dashboard. Se detectó en el dry-run, antes de
ejecutar.

> **Regla:** toda migración arranca con `dry_run=True` y se revisan los números. Y es
> **copia**, no movimiento, salvo confirmación explícita.

---

## 12. Aislar fallos por ítem en trabajos por lotes

Al batchear el ciclo horario en una sola llamada de Apify, introduje una regresión: una
cuenta con config mala o error de base **mataba la detección HOT de todas** las cuentas de
ese ciclo. El bucle anterior aislaba cada cuenta.

> **Regla:** en un lote, un ítem que falla no puede tumbar el resto. Envolver el proceso
> por ítem. Solo el recurso compartido (la llamada a la API) puede propagar.

También: liberar los reclamos si el lote falla, para que las filas no queden trabadas en
"en proceso" para siempre (ver `reset_stuck_ocr_claims()`).

---

## Checklist antes de tocar producción

1. ¿Hay algo corriendo? → `/ocr/status`, `/accounts/backfill-status`, `/apify/enrich-status`
2. ¿Necesito scrapear, o el dato ya está en un run pagado? → `/apify/runs`
3. Si scrapeo: ¿el faltante está concentrado en una cuenta (perfil) o disperso (URL)?
4. ¿Uso `_run_apify_actor_and_fetch`, no `_fetch_apify_items`?
5. ¿Es una migración? → `dry_run=True` primero, revisar números
6. Tras desplegar: consultar el endpoint real y **mirar los valores**, no solo el commit
7. ¿Medí antes y después? Si empeoró, decirlo y revertir

## Límites conocidos del entorno

- **Deploy en Render:** ~2 minutos; devuelve 502 durante el cambio. Esperar y consultar
  `/api/health` por el commit.
- **Modal OCR:** `MAX_CONTAINERS = 1` → serializa. Paralelizar del lado del backend no
  sirve; hay que subirlo y redesplegar el worker de Modal (requiere la cuenta del usuario).
- **Disco de Render:** 2GB. Los covers son el mayor consumidor. WebP q82 ahorra ~31%.
- **Apify:** máximo 100 archivos por request al worker de OCR; `onlyPostsNewerThan` es
  cota inferior (no hay cota superior nativa, el filtro de fecha superior va en Python).
