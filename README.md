# 💸 Sincronización de Gastos: Factorial → Holded

Esta herramienta copia automáticamente los gastos que están **aprobados en Factorial** y los crea como **facturas de compra en Holded**, incluyendo la foto o PDF de la factura adjunta.

> Funciona en tu propio ordenador. No necesita nada más allá de conexión a internet para hablar con Factorial y Holded.

---

## ¿Qué hace exactamente?

Por cada gasto aprobado en Factorial que todavía no esté en Holded, la herramienta:

1. Lee el gasto desde Factorial (importe, fecha, proveedor, IVA, descripción)
2. Si el proveedor no existe en Holded, lo crea automáticamente (buscando primero por NIF)
3. Crea una factura de compra en Holded con todos los datos
4. Adjunta la foto o PDF de la factura
5. Anota que ese gasto ya fue sincronizado (para no duplicarlo la próxima vez)

---

## 📦 Instalación (solo la primera vez)

### Paso 1 — Instalar Python

1. Ir a: https://www.python.org/downloads/
2. Descargar la versión más reciente (botón amarillo grande)
3. Ejecutar el instalador
4. **IMPORTANTE:** marcar la casilla **"Add Python to PATH"** antes de hacer clic en Install

### Paso 2 — Abrir la carpeta del proyecto en terminal

1. Descomprimir/abrir la carpeta del proyecto
2. En la barra de direcciones del Explorador de Windows, escribir `cmd` y pulsar Enter
   - Se abrirá una ventana negra (símbolo del sistema) ya posicionada en esa carpeta

### Paso 3 — Instalar las dependencias

Copiar y pegar esto en la ventana negra, luego pulsar Enter:

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Verás que se instalan varios paquetes. Esperar hasta que termine (puede tardar 1-2 minutos).

> ⚠️ Cada vez que abras una ventana nueva tendrás que ejecutar `venv\Scripts\activate` antes de usar la herramienta.

### Paso 4 — Configurar las claves de acceso

Las claves son como contraseñas que permiten a la herramienta conectarse a Factorial y Holded en tu nombre.

1. En la carpeta del proyecto, buscar el archivo `.env.example`
2. Hacer una **copia** de ese archivo y renombrarla exactamente como `.env` (sin `.example`)
3. Abrir `.env` con el Bloc de Notas
4. Rellenar las dos claves:

**Clave de Factorial:**
- Entrar en Factorial → Configuración → Integraciones → API
- Copiar la API Key y pegarla después del `=`

**Clave de Holded:**
- Entrar en Holded → Configuración → API Keys
- Crear una nueva key (o copiar la existente) y pegarla después del `=`

El archivo `.env` debe quedar así (con tus claves reales, sin espacios):
```
FACTORIAL_API_KEY=abc123tuclavedefactorial
HOLDED_API_KEY=xyz789tuclaveholded
FACTORIAL_EMPLOYEE_ID=
```

> 🔒 **Seguridad:** Este archivo `.env` contiene tus claves de acceso. **No lo compartas, no lo adjuntes en emails, y no lo subas a ningún sitio en internet.**

### Paso 5 — Obtener tu ID de empleado en Factorial (opcional)

Si quieres sincronizar solo los gastos de un empleado concreto (en lugar de todos):

Con el entorno virtual activado, ejecutar:
```
python scripts/explore_factorial.py
```

Esto mostrará una lista con todos los empleados y sus IDs. Copia el número ID del empleado que necesitas y pégalo en `.env` después de `FACTORIAL_EMPLOYEE_ID=`.

Si lo dejas vacío, se sincronizan los gastos de **todos** los empleados.

---

## 🖥️ Uso diario

### Abrir la herramienta

1. Abrir la carpeta del proyecto
2. En la barra de direcciones del Explorador, escribir `cmd` y pulsar Enter
3. Ejecutar estos dos comandos (uno por uno):
```
venv\Scripts\activate
streamlit run app.py
```
4. Se abrirá automáticamente el navegador en `http://localhost:8501`

### Pantalla principal — Pestaña "📤 Sincronizar"

Al entrar verás tres números:
- **Total aprobados en Factorial** → todos los gastos aprobados que existen
- **Pendientes de sincronizar** → los que todavía no están en Holded
- **Ya en Holded** → los que ya se copiaron anteriormente

Debajo aparece una tabla con los gastos pendientes (fecha, concepto, proveedor, importe, IVA).

**Para sincronizar:**
- Puedes activar **"🔮 Modo prueba"** si quieres ver qué se haría sin enviar nada a Holded (recomendado la primera vez)
- Cuando estés lista, pulsar el botón azul **"🚀 Sincronizar todo → Holded"**
- Verás el progreso en tiempo real. Al terminar aparecerá un resumen de éxitos y fallos

### Pestaña "📊 Historial"

Muestra cuántos gastos se han sincronizado en total y la fecha de la última sincronización.

### Columna izquierda (sidebar)

Muestra el estado de conexión con las APIs. Si aparece ✅ verde en ambas, todo está bien configurado.

---

## ❓ Preguntas frecuentes

**¿Puedo sincronizar dos veces sin que se dupliquen los gastos en Holded?**
Sí. La herramienta lleva un registro interno y nunca vuelve a enviar un gasto que ya envió.

**¿Qué pasa si un gasto falla?**
Aparece en rojo con el motivo del error. Los demás siguen procesándose. En la próxima sincronización volverá a intentar los que fallaron.

**¿Se sincronizan gastos que no están aprobados?**
No. Solo los gastos con estado "aprobado" en Factorial se copian a Holded.

**¿Qué pasa si el proveedor ya existe en Holded?**
La herramienta lo busca primero por NIF. Si ya existe, lo usa; si no, lo crea. Nunca duplica proveedores.

**Cerré la ventana negra, ¿se pierden los datos?**
No. Los datos sincronizados se guardan automáticamente en el archivo `data/synced_expenses.json`.

---

## 🆘 Solución de problemas

| Problema | Solución |
|---|---|
| `'python' no se reconoce como comando` | Reinstalar Python marcando "Add Python to PATH" |
| `venv\Scripts\activate` da error | Ejecutar primero `python -m venv venv` |
| ❌ API Factorial / ❌ API Holded en la barra lateral | Revisar que `.env` tenga las claves correctas, sin espacios ni comillas |
| La página no carga en el navegador | Esperar 5 segundos y refrescar. Si no, cerrar y volver a ejecutar `streamlit run app.py` |
| Error HTTP 401 o 403 | La clave API no tiene permisos o es incorrecta. Generar una nueva en Factorial/Holded |
| Error HTTP 429 | Demasiadas peticiones a la API. Esperar 1 minuto y volver a intentarlo |

---

## ⚠️ Notas importantes

- Cada gasto se sincroniza **una sola vez** (registro en `data/synced_expenses.json`)
- Solo se sincronizan gastos con estado **aprobado** en Factorial
- No hay servidor ni cloud: todo corre en tu equipo local
- El archivo `.env` contiene credenciales: **no compartirlo ni subirlo a ningún sitio**

---

## 📁 Qué contiene cada archivo

```
factorial-holded-expenses/
├── app.py                    → La interfaz visual (lo que ves en el navegador)
├── .env                      → Tus claves de acceso (NO compartir)
├── .env.example              → Plantilla vacía del archivo anterior
├── requirements.txt          → Lista de librerías necesarias
├── scripts/
│   ├── explore_factorial.py  → Ver empleados e IDs de Factorial
│   ├── explore_holded.py     → Ver facturas y proveedores de Holded
│   └── sync_expenses.py      → Versión de línea de comandos (alternativa a la web)
└── data/
    ├── synced_expenses.json  → Registro de gastos ya sincronizados
    └── attachments/          → Facturas descargadas temporalmente
```

---

> Herramienta local · Sin servidor · Sin suscripción · v1.0
