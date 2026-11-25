# Fixes de ocmonitor: tools en `session_part`, live histórico y columna de id

Documento de referencia para diagnosticar, aplicar y volver a aplicar los
parches en futuras versiones de
[`Shlomob/ocmonitor-share`](https://github.com/Shlomob/ocmonitor-share).

| Dato | Valor |
|---|---|
| Fecha del diagnóstico | 2026-11-24 |
| ocmonitor afectado | 1.0.5 (PyPI, instalado con pipx) |
| OpenCode | 2.4.1 |
| Esquema de BD | `session_v2` + `session_message` + `session_part` |
| Rama local de referencia | `feat/live-pinned-any-session` |
| Commits locales | `5e89110` (tools), `8e45c4f` (live), `c99dc62` (columna id), `2ddb1f7` (ancho adaptable), `3c7c140` (prefijos), `fa2eda3` (detalle tools/chat) y `f5b5dd5` (selector de sesiones) |
| Archivos modificados | `ocmonitor/utils/sqlite_utils.py`, `ocmonitor/services/live_monitor.py`, `ocmonitor/services/report_generator.py`, `ocmonitor/ui/tables.py` |
| Tests añadidos | `test_sqlite_v2_schema.py`, `test_sqlite_active_workflows.py`, `test_live_monitor.py` |
| Resultado | `ocmonitor model deepseek-v4.1-flash`: 195 → 848 tool calls; `live --session-id` acepta cualquier sesión y prefijos únicos; `ocmonitor sessions` muestra el id completo y se adapta al ancho |

---

## 1. Síntoma

`ocmonitor` mostraba el uso de tokens y coste correctamente, pero:

- Muchas sesiones aparecían con **0 tool calls** aunque hubieran usado tools.
- Otras aparecían con el **uso muy infravalorado**.
- Tools nuevas (`webfetch`, `question`, `skill`) no aparecían nunca.

Ejemplo real antes del parche:

```text
Tool      Calls  Success  Failed
edit         90       89       1
shell        49       49       0
read         34       34       0
...
Total tool calls: 195
```

Después del parche:

```text
Tool      Calls  Success  Failed
shell       359      359       0
edit        251      249       2
read        149      149       0
webfetch     22       20       2
...
Total tool calls: 848
```

---

## 2. Causa raíz

OpenCode 2.4 movió las *parts* de los mensajes a una tabla propia,
`session_part`, y **dejó de copiarlas** en
`session_message.data.content`.

Según cómo se escribió cada sesión, sus tool calls están en un sitio o en el
otro, y en la BD analizada **nunca en ambos**:

| Fuente | Tool calls |
|---|---|
| `session_part` (columna `type='tool'`, datos en `data`) | 640 |
| `session_message.data.content` (`type='tool'`) | 365 |
| Unión (0 ids repetidos) | 1.005 |
| Unión con estado terminal (`completed`/`error`) | 1.004 |

El código v1.0.5 solo consulta la segunda fuente:

```python
_V2_TOOL_FROM = (
    "session_message m, json_each("
    "CASE WHEN json_valid(m.data) THEN m.data ELSE '{}' END, '$.content') p"
)
```

Por eso las sesiones escritas por versiones nuevas (con `session_part`)
devuelven cero. En la BD del diagnóstico, 10 sesiones con 109, 103, 76, 70...
tools reales mostraban 0.

El repositorio upstream no menciona `session_part` en ningún sitio, así que su
detección de esquema no lo contempla.

### Consulta de comprobación

Sirve para verificar si el problema sigue presente en el futuro:

```bash
python3 - <<'EOF'
import sqlite3
db = sqlite3.connect("file:$HOME/.local/share/opencode/opencode.db?mode=ro", uri=True)
c = db.cursor()
q = """
WITH content_tools AS (
  SELECT m.session_id, json_extract(c.value,'$.id') cid
  FROM session_message m,
       json_each(CASE WHEN json_valid(m.data) THEN m.data ELSE '{}' END,'$.content') c
  WHERE json_extract(c.value,'$.type') = 'tool'
)
SELECT
  (SELECT count(*) FROM session_part WHERE type='tool') AS parts,
  (SELECT count(*) FROM content_tools) AS inline,
  (SELECT count(*) FROM content_tools
    WHERE cid IN (SELECT call_id FROM session_part)) AS overlap
"""
print(c.execute(q).fetchone())
EOF
```

Si `parts` es mayor que 0 y `overlap` es 0, este parche es necesario.

---

## 3. Solución

Se añade un tercer esquema, `_SCHEMA_V2_WITH_PARTS`, que:

1. Detecta la tabla `session_part` al elegir el esquema.
2. Combina con `UNION ALL` las tools de `session_part` y las que siguen
   inline en `session_message.data.content`.
3. Deduplica por id de llamada (`session_part.call_id` frente al `id` de la
   part inline), por si alguna versión escribe la misma llamada en ambos
   sitios.
4. Mantiene intacta la lógica anterior (v1, v2 y v2 con tablas legacy) cuando
   `session_part` no existe.

### 3.1. Parche de `ocmonitor/utils/sqlite_utils.py`

```diff
diff --git a/ocmonitor/utils/sqlite_utils.py b/ocmonitor/utils/sqlite_utils.py
--- a/ocmonitor/utils/sqlite_utils.py
+++ b/ocmonitor/utils/sqlite_utils.py
@@ -46,6 +46,23 @@ _V2_TOOL_FROM = (
     "CASE WHEN json_valid(m.data) THEN m.data ELSE '{}' END, '$.content') p"
 )
 
+# OpenCode 2.4+ moved message parts into their own ``session_part`` table and
+# stopped mirroring them into ``session_message.data.content``. Depending on
+# how a session was written, its tool calls live in one place or the other
+# (they never appear in both), so both sources have to be combined.
+_V2_TOOL_FROM_PARTS = (
+    "(SELECT session_id, message_id, data FROM session_part"
+    " UNION ALL"
+    " SELECT sm.session_id, sm.id, c.value"
+    " FROM session_message sm, json_each("
+    "CASE WHEN json_valid(sm.data) THEN sm.data ELSE '{}' END, '$.content') c"
+    " WHERE json_extract(c.value, '$.type') = 'tool'"
+    " AND NOT EXISTS ("
+    "SELECT 1 FROM session_part sp"
+    " WHERE sp.call_id = json_extract(c.value, '$.id'))"
+    ") p"
+)
+
 _SCHEMA_V2: Dict[str, str] = {
     "session": "session_v2",
     "message": "session_message",
@@ -63,6 +80,17 @@ _SCHEMA_V2: Dict[str, str] = {
     "tool_name_path": "$.name",
 }
 
+_SCHEMA_V2_WITH_PARTS: Dict[str, str] = {
+    **_SCHEMA_V2,
+    "tool_from": _V2_TOOL_FROM_PARTS,
+    "tool_from_with_message": (
+        f"{_V2_TOOL_FROM_PARTS} JOIN session_message m ON p.message_id = m.id"
+    ),
+    "tool_session": "p.session_id",
+    "tool_data": "p.data",
+    "tool_name_path": "$.name",
+}
+
 # Upgraded databases keep the frozen v1 tables next to the v2 ones. The v2
 # migration skips some older sessions' messages and rewrites time.completed
 # on the ones it copies, so v1 rows win and v2 only adds messages v1 lacks.
@@ -189,11 +217,13 @@ class SQLiteProcessor:
             row[0]
             for row in conn.execute(
                 "SELECT name FROM sqlite_master WHERE type = 'table' "
-                "AND name IN ('session_v2', 'message', 'part')"
+                "AND name IN ('session_v2', 'session_part', 'message', 'part')"
             )
         }
         if "session_v2" not in tables:
             return _SCHEMA_V1
+        if "session_part" in tables:
+            return _SCHEMA_V2_WITH_PARTS
         if {"message", "part"} <= tables:
             return _SCHEMA_V2_WITH_LEGACY
         return _SCHEMA_V2
```

### 3.2. Test de regresión

Se añade al final de `tests/unit/test_sqlite_v2_schema.py`. Cubre tools solo en
`session_part`, solo inline, una llamada duplicada en ambos sitios, estados no
terminales y agregación por modelo.

```diff
diff --git a/tests/unit/test_sqlite_v2_schema.py b/tests/unit/test_sqlite_v2_schema.py
--- a/tests/unit/test_sqlite_v2_schema.py
+++ b/tests/unit/test_sqlite_v2_schema.py
@@ -302,3 +302,77 @@ def test_upgraded_db_model_queries_and_stats(upgraded_db: Path):
     db_stats = SQLiteProcessor.get_database_stats(upgraded_db)
     assert db_stats["session_count"] == 2
     assert db_stats["message_count"] == 3
+
+
+def _tool(name, status, call_id):
+    return {"type": "tool", "id": call_id, "name": name,
+            "state": {"status": status}}
+
+
+@pytest.fixture
+def v2_parts_db(tmp_path: Path) -> Path:
+    """OpenCode 2.4+ database: parts live in ``session_part``.
+
+    Some sessions keep their tool calls inline in the message content and
+    others only in ``session_part``; a call mirrored in both must count once.
+    """
+    db_path = tmp_path / "opencode.db"
+    conn = sqlite3.connect(db_path)
+    conn.executescript("""
+        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT);
+        CREATE TABLE session_v2 (
+            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
+            directory TEXT NOT NULL, title TEXT,
+            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
+        );
+        CREATE TABLE session_message (
+            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
+            seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
+            time_updated INTEGER NOT NULL, data TEXT NOT NULL
+        );
+        CREATE TABLE session_part (
+            message_id TEXT NOT NULL, session_id TEXT NOT NULL,
+            ordinal INTEGER NOT NULL, type TEXT NOT NULL, call_id TEXT,
+            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
+            data TEXT NOT NULL
+        );
+    """)
+    conn.execute("INSERT INTO project VALUES ('p1', '/work/myproj', 'myproj')")
+    conn.execute(
+        "INSERT INTO session_v2 VALUES ('ses_p', 'p1', NULL, '/work/myproj', 'Parts', 1000, 2000)"
+    )
+    message = _assistant(
+        "gpt-5.5", "openai", 100,
+        tools=[("read", "completed")],
+    )
+    data = json.loads(message)
+    data["content"].append(_tool("glob", "completed", "call_dup"))
+    conn.execute(
+        "INSERT INTO session_message VALUES ('m1', 'ses_p', 'assistant', 1, 1000, 1000, ?)",
+        (json.dumps(data),),
+    )
+    parts = [
+        ("call_dup", "glob", "completed"),   # also inline -> counted once
+        ("call_1", "shell", "completed"),
+        ("call_2", "edit", "error"),
+        ("call_3", "shell", "running"),      # not terminal -> ignored
+    ]
+    for ordinal, (call_id, name, status) in enumerate(parts):
+        conn.execute(
+            "INSERT INTO session_part VALUES ('m1', 'ses_p', ?, 'tool', ?, 1000, 1000, ?)",
+            (ordinal, call_id, json.dumps(_tool(name, status, call_id))),
+        )
+    conn.commit()
+    conn.close()
+    return db_path
+
+
+def test_session_part_tools_are_combined_with_inline_content(v2_parts_db: Path):
+    stats = {s.tool_name: s.total_calls for s in
+             SQLiteProcessor.load_tool_usage_for_sessions(["ses_p"], v2_parts_db)}
+
+    assert stats == {"shell": 1, "edit": 1, "read": 1, "glob": 1}
+
+    by_model = {m.model_name: m.total_calls for m in
+                SQLiteProcessor.load_tool_usage_by_model_for_sessions(["ses_p"], v2_parts_db)}
+    assert by_model == {"gpt-5.5": 4}
```

---

## 4. Cómo volver a aplicarlo

### Opción A — Sobre un clon del repositorio (recomendado)

```bash
git clone https://github.com/Shlomob/ocmonitor-share.git
cd ocmonitor-share

# Guarda el diff de las secciones 3.1 y 3.2 en fix.patch y aplica:
git apply --check fix.patch   # comprobar que encaja
git apply fix.patch

# Tests
python3 -m pip install pytest
python3 -m pytest -q tests/unit

# Instalación en modo editable
python3 -m pip install -e .
```

Si el diff no encaja porque upstream cambió el archivo, aplica los cambios a
mano: son 3 bloques (constante `_V2_TOOL_FROM_PARTS`, diccionario
`_SCHEMA_V2_WITH_PARTS` y detección en `_get_schema`).

### Opción B — Sobre una instalación pipx ya existente

Localiza el archivo dentro del venv:

```bash
SP=$(ls -d ~/.local/share/pipx/venvs/opencode-monitor/lib/python*/site-packages)
echo "$SP"
```

Haz copia de seguridad y aplica el parche desde ahí (las rutas del diff
empiezan por `ocmonitor/`, por eso `-p1`):

```bash
cd "$SP"
cp ocmonitor/utils/sqlite_utils.py \
   ocmonitor/utils/sqlite_utils.py.bak-$(date +%Y%m%d%H%M%S)

patch -p1 < /ruta/a/fix.patch
```

También se puede editar directamente el archivo con los tres bloques de la
sección 3.1.

> **Aviso:** `pipx upgrade opencode-monitor` (o `pip install --upgrade`)
> sobrescribe el parche. Hay que volver a aplicarlo tras cada actualización.

### Opción C — Variables de entorno, sin tocar el venv

```bash
PYTHONPATH=/ruta/ocmonitor-share ocmonitor model deepseek-v4.1-flash
```

Útil para probar el parche sin modificar la instalación.

---

## 5. Verificación

### Tests

```bash
python3 -m pytest -q tests/unit
# 349 passed
```

### Comparación real contra la BD

```bash
ocmonitor model deepseek-v4.1-flash
```

Antes del parche: 195 llamadas. Después: 848 (puede variar con sesiones
nuevas). Deben aparecer tools que antes no salían (`webfetch`, `question`,
`skill`).

Comprobación directa de que la unión cuadra:

```bash
python3 - <<'EOF'
import sqlite3
db = sqlite3.connect("file:$HOME/.local/share/opencode/opencode.db?mode=ro", uri=True)
c = db.cursor()
q = """
WITH union_tools AS (
  SELECT data FROM session_part WHERE type='tool'
  UNION
  SELECT c.value FROM session_message m,
    json_each(CASE WHEN json_valid(m.data) THEN m.data ELSE '{}' END,'$.content') c
  WHERE json_extract(c.value,'$.type')='tool'
)
SELECT count(*),
       sum(json_extract(data,'$.state.status') IN ('completed','error'))
FROM union_tools
"""
print(c.execute(q).fetchone())  # (total, terminales)
EOF
```

El total terminal debe coincidir aproximadamente con la suma de tools por
modelo que muestra `ocmonitor`.

---

## 6. Segundo parche: `ocmonitor live --session-id` con sesiones históricas

### 6.1. Síntoma y causa

`ocmonitor live --session-id <id>` existía, pero solo resolvía ids dentro de
los workflows **activos**, es decir, con actividad en los últimos 30 minutos
(`get_all_active_workflows(active_threshold_minutes=30)`). Al apuntar a una
sesión antigua o terminada:

```text
Selected session/workflow 'ses_...' is not available.
```

Y aunque se hubiera cargado, el bucle de refresco volvía a buscar solo entre
las activas y terminaba con:

```text
Selected session/workflow '...' is no longer active. Stopping monitor.
```

### 6.2. Solución

1. Nuevo método `SQLiteProcessor.get_workflow_by_session_id()` que carga una
   sesión por id **sin límite de antigüedad**. Si el id es de un sub-agente,
   devuelve el workflow de su sesión padre; si el padre no tiene interacciones
   cargables, usa el propio sub-agente.
2. Nuevo helper `LiveMonitor._resolve_pinned_sqlite_workflow()`: busca primero
   entre los workflows activos y, si no está, lo carga de la BD y lo inserta en
   la lista en memoria.
3. El arranque y el bucle de refresco usan ese helper, de modo que una sesión
   histórica se recarga por id en cada intervalo y el panel no se detiene.

Uso:

```bash
ocmonitor live --session-id ses_df5ddb4eeffe8GkB66nkhj4RIB
ocmonitor live --session-id ses_xxx --interval 1
```

Los datos de una sesión terminada son estáticos (último estado guardado); el
panel se refresca pero no aparecerán eventos nuevos.

### 6.3. Parche de `ocmonitor/utils/sqlite_utils.py`

Se inserta antes de `_build_workflow_dict`:

```diff
diff --git a/ocmonitor/utils/sqlite_utils.py b/ocmonitor/utils/sqlite_utils.py
--- a/ocmonitor/utils/sqlite_utils.py
+++ b/ocmonitor/utils/sqlite_utils.py
@@ -617,6 +617,74 @@ class SQLiteProcessor:
         finally:
             conn.close()
 
+    @classmethod
+    def get_workflow_by_session_id(
+        cls, session_id: str, db_path: Optional[Path] = None
+    ) -> Optional[Dict[str, Any]]:
+        """Load the workflow that contains a session, active or finished.
+
+        Unlike :meth:`get_all_active_workflows`, this does not apply the
+        recency threshold, so historical sessions can be inspected. If the
+        given ID belongs to a sub-agent, its parent workflow is returned.
+
+        Args:
+            session_id: Main session, sub-agent session or workflow ID
+            db_path: Path to database (uses default if not provided)
+
+        Returns:
+            Workflow dictionary compatible with SessionWorkflow, or None if
+            the session does not exist or has no interactions.
+        """
+        if db_path is None:
+            db_path = cls.find_database_path()
+
+        if not db_path or not db_path.exists():
+            return None
+
+        conn = cls._get_connection(db_path)
+        try:
+            schema = cls._get_schema(conn)
+
+            def load_row(row: sqlite3.Row) -> Optional[SessionData]:
+                return cls.load_session_data(conn, row, include_zero_token=True)
+
+            row = conn.execute(
+                f"""
+                SELECT s.*, p.worktree as project_path, p.name as project_name
+                FROM {schema['session']} s
+                LEFT JOIN project p ON s.project_id = p.id
+                WHERE s.id = ?
+                """,
+                (session_id,),
+            ).fetchone()
+            if not row:
+                return None
+
+            # A sub-agent ID resolves to its parent workflow; fall back to the
+            # sub-agent itself when the parent has no loadable interactions.
+            candidates = [row]
+            if row["parent_id"]:
+                parent_row = conn.execute(
+                    f"""
+                    SELECT s.*, p.worktree as project_path, p.name as project_name
+                    FROM {schema['session']} s
+                    LEFT JOIN project p ON s.project_id = p.id
+                    WHERE s.id = ?
+                    """,
+                    (row["parent_id"],),
+                ).fetchone()
+                if parent_row:
+                    candidates.insert(0, parent_row)
+
+            for candidate in candidates:
+                session = load_row(candidate)
+                if session and session.files:
+                    return cls._build_workflow_dict(conn, session)
+
+            return None
+        finally:
+            conn.close()
+
     @classmethod
     def _build_workflow_dict(
         cls, conn: sqlite3.Connection, main_session: SessionData
```

### 6.4. Parche de `ocmonitor/services/live_monitor.py`

```diff
diff --git a/ocmonitor/services/live_monitor.py b/ocmonitor/services/live_monitor.py
--- a/ocmonitor/services/live_monitor.py
+++ b/ocmonitor/services/live_monitor.py
@@ -292,6 +292,32 @@ class LiveMonitor:
                 return workflow
         return None
 
+    def _resolve_pinned_sqlite_workflow(
+        self, workflows: List[Dict[str, Any]], selected_session_id: str
+    ) -> Optional[Dict[str, Any]]:
+        """Resolve a pinned ID, loading finished sessions from the database.
+
+        Active workflows are matched first; if the ID belongs to a session
+        that is no longer active (or very old), it is loaded directly and
+        added to the in-memory workflow list so the refresh loop can keep
+        displaying it.
+        """
+        workflow = self._resolve_selected_sqlite_workflow(
+            workflows, selected_session_id
+        )
+        if workflow:
+            return workflow
+
+        workflow = SQLiteProcessor.get_workflow_by_session_id(selected_session_id)
+        if not workflow:
+            return None
+
+        if not any(
+            wf.get("workflow_id") == workflow["workflow_id"] for wf in workflows
+        ):
+            workflows.insert(0, workflow)
+        return workflow
+
     def _resolve_selected_file_workflow(
         self, workflows: List[SessionWorkflow], selected_session_id: str
     ) -> Optional[SessionWorkflow]:
@@ -1503,14 +1529,11 @@ class LiveMonitor:
             active_workflows = self._get_sqlite_active_workflows(
                 allow_fallback=not bool(selected_session_id)
             )
-            if not active_workflows:
-                self.console.print(
-                    "[status.error]No sessions found in database.[/status.error]"
-                )
-                return
 
             if selected_session_id:
-                current_workflow = self._resolve_selected_sqlite_workflow(
+                # Pinned IDs may point at finished/old sessions, which are not
+                # in the active-workflow window. Load them directly instead.
+                current_workflow = self._resolve_pinned_sqlite_workflow(
                     active_workflows, selected_session_id
                 )
                 if not current_workflow:
@@ -1519,6 +1542,11 @@ class LiveMonitor:
                     )
                     return
             else:
+                if not active_workflows:
+                    self.console.print(
+                        "[status.error]No sessions found in database.[/status.error]"
+                    )
+                    return
                 current_workflow = self._select_most_recent_workflow(active_workflows)
 
             current_workflow_id = current_workflow["workflow_id"]
@@ -1689,28 +1717,30 @@ class LiveMonitor:
                     active_workflows = self._get_sqlite_active_workflows(
                         allow_fallback=not bool(selected_session_id)
                     )
-                    descriptors = self._describe_sqlite_workflows(active_workflows)
-
-                    if not active_workflows:
-                        self.console.print(
-                            "[status.warning]No workflows available to monitor.[/status.warning]"
-                        )
-                        break
 
                     if selected_session_id:
-                        new_current = self._resolve_selected_sqlite_workflow(
+                        # Finished/old sessions are reloaded by ID so pinned
+                        # mode keeps working after they leave the active window.
+                        new_current = self._resolve_pinned_sqlite_workflow(
                             active_workflows, selected_session_id
                         )
                         if not new_current:
                             self.console.print(
-                                f"[status.warning]Selected session/workflow '{selected_session_id}' is no longer active. Stopping monitor.[/status.warning]"
+                                f"[status.warning]Selected session/workflow '{selected_session_id}' is no longer available. Stopping monitor.[/status.warning]"
                             )
                             break
                     else:
+                        if not active_workflows:
+                            self.console.print(
+                                "[status.warning]No workflows available to monitor.[/status.warning]"
+                            )
+                            break
                         new_current = self._select_most_recent_workflow(
                             active_workflows
                         )
 
+                    descriptors = self._describe_sqlite_workflows(active_workflows)
+
                     if new_current["workflow_id"] != current_workflow_id:
                         current_workflow_id = new_current["workflow_id"]
                         self.prev_tracked = set()
```

### 6.5. Tests de regresión

Se añaden dos bloques: uno prueba la carga de una sesión terminada desde la
BD (incluido el acceso por id de sub-agente) y otro el helper de pinned mode.

```diff
diff --git a/tests/unit/test_sqlite_active_workflows.py b/tests/unit/test_sqlite_active_workflows.py
--- a/tests/unit/test_sqlite_active_workflows.py
+++ b/tests/unit/test_sqlite_active_workflows.py
@@ -115,3 +115,49 @@ def test_get_all_active_workflows_orders_by_latest_parent_activity(tmp_path: Pat
     )
 
     assert [w["workflow_id"] for w in workflows[:2]] == ["older-parent", "newer-parent"]
+
+
+def test_get_workflow_by_session_id_loads_finished_session(tmp_path: Path):
+    db_path = tmp_path / "opencode.db"
+    conn = sqlite3.connect(db_path)
+    conn.row_factory = sqlite3.Row
+    _create_base_schema(conn)
+
+    old_ms = int(time.time() * 1000) - 3 * 24 * 60 * 60 * 1000
+    conn.execute(
+        "INSERT INTO project (id, worktree, name) VALUES (?, ?, ?)",
+        ("proj", "/tmp/project", "project"),
+    )
+    conn.execute(
+        "INSERT INTO session (id, project_id, parent_id, title, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?)",
+        ("old-parent", "proj", None, "Old Parent", old_ms, old_ms),
+    )
+    conn.execute(
+        "INSERT INTO session (id, project_id, parent_id, title, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?)",
+        ("old-sub", "proj", "old-parent", "Old Sub", old_ms + 1000, old_ms + 1000),
+    )
+    conn.execute(
+        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
+        ("msg-parent", "old-parent", old_ms, old_ms, _assistant_message()),
+    )
+    conn.execute(
+        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
+        ("msg-sub", "old-sub", old_ms + 1000, old_ms + 1000, _assistant_message()),
+    )
+    conn.commit()
+    conn.close()
+
+    # The finished session is outside the active window...
+    assert SQLiteProcessor.get_all_active_workflows(db_path) == []
+
+    # ...but can still be loaded by ID, alone or through its sub-agent.
+    workflow = SQLiteProcessor.get_workflow_by_session_id("old-parent", db_path)
+    assert workflow is not None
+    assert workflow["workflow_id"] == "old-parent"
+    assert workflow["sub_agent_count"] == 1
+
+    via_sub = SQLiteProcessor.get_workflow_by_session_id("old-sub", db_path)
+    assert via_sub is not None
+    assert via_sub["workflow_id"] == "old-parent"
+
+    assert SQLiteProcessor.get_workflow_by_session_id("missing", db_path) is None
```

```diff
diff --git a/tests/unit/test_live_monitor.py b/tests/unit/test_live_monitor.py
--- a/tests/unit/test_live_monitor.py
+++ b/tests/unit/test_live_monitor.py
@@ -1409,3 +1409,46 @@ class TestLiveMonitorProviderAwareRegressions:
 
         assert "claude-sonnet-4.5" in result
         assert result["claude-sonnet-4.5"]["context_window"] == 100000
+
+
+class TestPinnedSqliteWorkflow:
+    """Pinned mode can display sessions that are no longer active."""
+
+    def test_resolve_pinned_loads_finished_session(self, monkeypatch):
+        from ocmonitor.services.live_monitor import LiveMonitor
+
+        monitor = LiveMonitor(pricing_data={}, init_from_db=False)
+        finished = {"workflow_id": "wf-old", "all_sessions": []}
+        monkeypatch.setattr(
+            "ocmonitor.services.live_monitor.SQLiteProcessor.get_workflow_by_session_id",
+            lambda session_id: finished if session_id == "ses-old" else None,
+        )
+
+        workflows = []
+        result = monitor._resolve_pinned_sqlite_workflow(workflows, "ses-old")
+
+        assert result is finished
+        assert workflows == [finished]
+
+    def test_resolve_pinned_prefers_active_match(self, monkeypatch):
+        from ocmonitor.services.live_monitor import LiveMonitor
+
+        monitor = LiveMonitor(pricing_data={}, init_from_db=False)
+        monkeypatch.setattr(
+            "ocmonitor.services.live_monitor.SQLiteProcessor.get_workflow_by_session_id",
+            lambda session_id: pytest.fail("should not load from DB"),
+        )
+
+        active = {"workflow_id": "wf-active", "all_sessions": []}
+        assert monitor._resolve_pinned_sqlite_workflow([active], "wf-active") is active
+
+    def test_resolve_pinned_returns_none_for_unknown_id(self, monkeypatch):
+        from ocmonitor.services.live_monitor import LiveMonitor
+
+        monitor = LiveMonitor(pricing_data={}, init_from_db=False)
+        monkeypatch.setattr(
+            "ocmonitor.services.live_monitor.SQLiteProcessor.get_workflow_by_session_id",
+            lambda session_id: None,
+        )
+
+        assert monitor._resolve_pinned_sqlite_workflow([], "missing") is None
```

### 6.6. Verificación

```bash
# Tests
python3 -m pytest -q tests/unit
# 353 passed

# Sesión histórica real (terminada hace días)
ocmonitor live --session-id ses_df5ddb4eeffe8GkB66nkhj4RIB --interval 1
# Starting live monitoring of workflow: ses_...
# Pinned mode: tracking selected ID ...
# (el panel se mantiene; Ctrl+C para salir)

# Id inexistente
ocmonitor live --session-id ses_no_existe
# Selected session/workflow 'ses_no_existe' is not available.
```

### 6.7. Reaplicación

Funciona igual que en la sección 4, pero afecta a dos archivos:

```bash
git apply live.patch
python3 -m pytest -q tests/unit
```

En una instalación pipx, haz copia de seguridad y parchea desde
`site-packages`:

```bash
cd "$(ls -d ~/.local/share/pipx/venvs/opencode-monitor/lib/python*/site-packages)"
cp ocmonitor/services/live_monitor.py \
   ocmonitor/services/live_monitor.py.bak-$(date +%Y%m%d%H%M%S)
cp ocmonitor/utils/sqlite_utils.py \
   ocmonitor/utils/sqlite_utils.py.bak-$(date +%Y%m%d%H%M%S)
patch -p1 < /ruta/a/live.patch
```

Este parche es independiente del de `session_part`: puede aplicarse solo,
aunque el orden recomendado es primero el de la sección 3.

### 6.8. Aceptar prefijos de id (`3c7c140`)

Para no tener que escribir los ~30 caracteres del id, `--session-id` admite
un prefijo:

- Si coincide con un id exacto, se usa ese.
- Si exactamente una sesión empieza por el prefijo, se expande y se informa.
- Si hay varias, se listan y el comando se detiene (no adivina).
- La comparación usa `substr`, no `LIKE`, para que los guiones bajos de
  `ses_df...` se traten literalmente.

```bash
ocmonitor live --session-id ses_df0c22
# Resolved session prefix ses_df0c22 -> ses_df0c225d0ffemklRasfJmRgx2x
```

```text
ocmonitor live --session-id ses_df0
# Session ID prefix 'ses_df0' is ambiguous. 2 sessions match:
#   ses_df0c225d0ffemklRasfJmRgx2x
#   ses_df0d046acffeocqooxTldWv92E
# Provide more characters to disambiguate.
```

Cambios:

- `SQLiteProcessor.resolve_session_id(partial_id, db_path)` devuelve
  `(full_id, candidates)` consultando la tabla de sesiones.
- `LiveMonitor.start_sqlite_workflow_monitoring()` resuelve el prefijo antes
  de cargar workflows; si es ambiguo, lista los candidatos y retorna.
- Texto de ayuda de `--session-id` actualizado.
- Test en `test_sqlite_active_workflows.py` (coincidencia exacta, prefijo
  único, ambiguo, inexistente y guion bajo literal).

### 6.9. Detalle de tools y chat en el panel live (`fa2eda3`)

Requiere `--interactive-switch` (o `--pick`) para capturar teclas:

```bash
ocmonitor live --interactive-switch
```

Novedades:

- **Cabecera:** muestra el `ID` de la sesión y el total de
  `Iterations` (suma del workflow si hay sub-agentes). La cabecera pasa a dos
  líneas y su altura de layout de 3 a 5.
- **`t` — Tools:** agrupa las llamadas por herramienta con `#`, total, OK y
  fallidas. `1..9` abre las llamadas de ese grupo (hora, sesión `main`/`subN`,
  estado y comando/ruta/patrón/URL). `n`/`p` paginan (8 por página).
- **`m` — Messages:** transcripción de chat de todas las sesiones del
  workflow (texto de usuario/asistente y resumen de tools), paginada de 3 en 3
  mensajes.
- **`b`** vuelve un nivel (de llamadas a grupos, de grupos al dashboard);
  **`q`** sale.
- Los textos de chat se recortan a 600 caracteres por mensaje y los inputs de
  tools a 240/180 caracteres para que la pantalla sea navegable.

Datos: nuevos `SQLiteProcessor.load_tool_calls_for_sessions()` y
`load_chat_messages_for_session()`. Combinan `session_part`, `part` y el
contenido inline de `session_message`, deduplicando tools por id.

El diff completo de este parche está en
`/home/alumno/borrar/ocmonitor-live-detalle.patch` (solo `ocmonitor/`, listo
para `patch -p1` desde `site-packages`).

### 6.10. `l` lista todas las sesiones (`f5b5dd5`)

El selector de `l` recibía solo la lista de workflows activos (últimos 30
minutos), así que no aparecían las sesiones antiguas y el único elemento no
activo solía ser la sesión fijada.

Cambios:

- Nuevo `SQLiteProcessor.get_all_workflows(limit=100)`: todas las sesiones
  padre con interacciones, sin ventana temporal, con sus sub-agentes.
- Al pulsar `l` se cargan todas, se construyen descriptores propios y la
  selección se resuelve contra esa lista completa.
- La tabla del selector muestra el **id completo** (se pliega en varias líneas
  en vez de recortar con `…`).
- Tras elegir una sesión, `n`/`p` y los saltos numéricos resuelven también
  sesiones históricas mediante el cargador de pinned mode.
- Test añadido en `test_sqlite_active_workflows.py`: una sesión de hace 3 días
  no está en `get_all_active_workflows()` pero sí en `get_all_workflows()`.

---

## 7. Tercer parche: columna `Session ID` en `ocmonitor sessions`

### 7.1. Motivación

La tabla de `ocmonitor sessions` no mostraba el id de la sesión, así que para
usarlo con `ocmonitor live --session-id` había que recurrir al JSON
(`--format json`) o a consultar SQLite. Se añade una columna `Session ID` con
el id completo, sin truncar, para poder copiarlo directamente.

Afecta a las dos variantes de la tabla:

- Agrupada por workflows (por defecto).
- Sin agrupar (`--no-group`).

En la vista agrupada, la fila de resumen del workflow muestra el id de la
sesión principal, y cada fila de sesión (principal o sub-agente) muestra su
propio id.

### 7.2. Parche de `ocmonitor/services/report_generator.py`

```diff
diff --git a/ocmonitor/services/report_generator.py b/ocmonitor/services/report_generator.py
--- a/ocmonitor/services/report_generator.py
+++ b/ocmonitor/services/report_generator.py
@@ -834,6 +834,7 @@ class ReportGenerator:
         )
 
         table.add_column("Started", style="table.row.time", no_wrap=True)
+        table.add_column("Session ID", style="table.row.model", no_wrap=True)
         table.add_column(
             "Session / Workflow", style="table.row.main", no_wrap=False, max_width=45
         )
@@ -883,6 +884,7 @@ class ReportGenerator:
 
                 table.add_row(
                     f"[table.row.time]{start_time}[/table.row.time]",
+                    f"[table.row.model]{workflow.workflow_id}[/table.row.model]",
                     f"[table.row.main]{title}[/table.row.main]",
                     f"[table.row.project]{workflow.project_name[:15]}[/table.row.project]",
                     f"[table.row.model]{provider}[/table.row.model]",
@@ -916,6 +918,7 @@ class ReportGenerator:
 
                 table.add_row(
                     "",  # No separate start time for sub-rows
+                    f"[table.row.model]{main.session_id}[/table.row.model]",
                     f"  ├─ {main_title}",
                     "",
                     provider,
@@ -951,6 +954,7 @@ class ReportGenerator:
 
                     table.add_row(
                         "",  # No separate start time for sub-rows
+                        f"[table.row.model]{sub.session_id}[/table.row.model]",
                         f"{prefix} {sub_title}",
                         "",
                         provider,
@@ -991,6 +995,7 @@ class ReportGenerator:
 
                 table.add_row(
                     start_time,
+                    f"[table.row.model]{main.session_id}[/table.row.model]",
                     title,
                     main.project_name[:15],
                     provider,
```

### 7.3. Parche de `ocmonitor/ui/tables.py`

```diff
diff --git a/ocmonitor/ui/tables.py b/ocmonitor/ui/tables.py
--- a/ocmonitor/ui/tables.py
+++ b/ocmonitor/ui/tables.py
@@ -98,6 +98,7 @@ class TableFormatter:
         table.add_column("Started", style="table.row.time", no_wrap=True)
         table.add_column("Duration", style="table.row.time", no_wrap=True)
         table.add_column("Session", style="table.row.main", max_width=35)
+        table.add_column("Session ID", style="table.row.model", no_wrap=True)
         table.add_column("Provider", style="table.row.model", max_width=20)
         table.add_column("Model", style="table.row.model", max_width=30)
         table.add_column("Interactions", justify="right", style="status.success")
@@ -140,10 +141,12 @@ class TableFormatter:
                     # Truncate if too long for display
                     if len(session_display) > 35:
                         session_display = session_display[:32] + "..."
+                    session_id_display = session.session_id
                 else:
                     start_time = ""
                     duration = ""
                     session_display = ""
+                    session_id_display = ""
 
                 # Format model name
                 provider, model = FileProcessor.split_provider_model(display_model)
@@ -167,6 +170,7 @@ class TableFormatter:
                     start_time,
                     duration,
                     session_display,
+                    session_id_display,
                     provider_text,
                     model_text,
                     self.format_number(stats['files']),
@@ -191,6 +195,7 @@ class TableFormatter:
             Text("TOTALS", style="table.footer"),
             "",
             "",  # Empty session column
+            "",  # Empty session ID column
             "",  # Empty provider column
             Text(f"{len(sorted_sessions)} sessions", style="table.footer"),
             Text(self.format_number(total_interactions), style="status.success"),
```

No hay tests nuevos: la suite existente sigue pasando (`353 passed`) y no había
tests que validaran estas tablas.

### 7.4. Verificación y reaplicación

```bash
ocmonitor sessions --limit 3
ocmonitor sessions --limit 3 --no-group
```

Debe aparecer la columna `Session ID` junto a `Started` (vista agrupada) o
junto a `Session` (vista sin agrupar), con ids completos tipo
`ses_df0d046acffeocqooxTldWv92E`.

```bash
git apply sessions-id.patch
python3 -m pytest -q tests/unit
```

En pipx, copia de seguridad y `patch -p1` desde `site-packages`, igual que en
los parches anteriores. Afecta a `services/report_generator.py` y
`ui/tables.py`.

---

## 8. Notas para el futuro

- **Qué comprobar primero:** que exista la tabla `session_part` y que
  `session_message.data.content` ya no contenga las tools. La consulta de la
  sección 2 lo resuelve.
- **Detección de esquema:** el orden actual es
  `session_part` → legacy (`message` + `part`) → v2 simple. Si upstream añade
  su propia detección, comparar antes de reaplicar para no duplicar lógica.
- **BDs migradas con `part` + `session_part` a la vez:** el parche prioriza
  `session_part` y las tools inline, e ignora la tabla legacy `part`. En la BD
  del diagnóstico no existía `part`, así que no se pudo probar ese caso. Si
  tras una actualización desaparecen tools de sesiones antiguas, ampliar la
  unión para incluir también `part` y deduplicar por id de llamada.
- **Deduplicación:** se apoya en que el id inline (`$.id`) coincide con
  `session_part.call_id`. Si una versión futura cambia esa correspondencia,
  revisar la cláusula `NOT EXISTS`.
- **Formato del estado:** solo se cuentan `completed` y `error`; `running` se
  ignora a propósito para no contar llamadas a medias.
- **`live --session-id` solo cubre SQLite:** el modo legacy de ficheros
  (`--source files`) sigue limitándose a workflows activos. Si algún día hace
  falta, aplicar la misma idea cargando el directorio de la sesión por id.
- **Sesiones históricas en vivo:** el panel se refresca, pero una sesión
  terminada no genera eventos nuevos; muestra su último estado guardado.
- **Ramas y commits de referencia:** `fix/session-part-tool-usage` /
  `5e89110` (tools), `feat/live-pinned-any-session` / `8e45c4f` (live) y
  `c99dc62` (columna id) en el clon local
  `/tmp/opencode/ocmonitor-share`. Ese directorio es temporal; conserva este
  documento y los diffs, no el clon.
- **Tablas y columnas:** si una versión futura reordena las tablas de
  `report_generator.py` o `tables.py`, añadir la columna `Session ID` y un
  valor por fila (id del workflow, de la sesión principal o del sub-agente)
  suele ser suficiente; el resto es maquetación de Rich.


---

## Anexo A — Tabla de `ocmonitor sessions` adaptable al ancho

Commit de referencia: `2ddb1f7`.

### A.1. Problema

La tabla de `ocmonitor sessions` necesita mucho ancho: 10 columnas en la vista
agrupada y 12 en `--no-group`. En terminales estrechas Rich colapsaba columnas
hasta dejarlas vacías y recortaba valores con `…` (el id de sesión, el modelo,
el proyecto y los títulos). El resultado no permitía ver ni copiar los valores
completos.

### A.2. Solución

- Umbrales de ancho: **220** columnas para la vista agrupada y **245** para
  `--no-group`.
- Por debajo del umbral se usa una tabla compacta de **2 columnas**:
  - `Session`: fecha/hora, título completo y `ID: ses_...` en líneas separadas.
  - `Details`: proyecto, modelo(s), agente y métricas (interacciones, tokens,
    coste; duración en `--no-group`), también en varias líneas.
- Las columnas usan `overflow="fold"` para partir palabras largas en vez de
  recortarlas con `…`; ningún valor se pierde, ni siquiera a 60 columnas.
- En terminales anchas se mantiene la tabla detallada, pero se han eliminado
  los truncados manuales (`title[:37]`, `project[:15]`, etc.) y los
  `max_width` que recortaban modelo/proveedor.
- La fecha se integra en la celda `Session` para dedicar más ancho al id; a
  80 columnas el id completo cabe en una sola línea.

### A.3. Verificación

```bash
COLUMNS=80  ocmonitor sessions --limit 2
COLUMNS=120 ocmonitor sessions --limit 2 --no-group
COLUMNS=250 ocmonitor sessions --limit 2
```

A 80 columnas debe verse:

```text
┃ Session                            ┃ Details                                 ┃
│ 2026-11-24 21:27                   │ borrar ·                                │
│ Sistema operativo y versión        │ opencode-go/deepseek-v4.1-flash · build │
│ ID: ses_df0c225d0ffemklRasfJmRgx2x │ 3 interacciones · 18,556 tokens · $0.00 │
```

Tests: `353 passed`.

### A.4. Diff completo

```diff
diff --git a/ocmonitor/services/report_generator.py b/ocmonitor/services/report_generator.py
index 663ff3a..2b4035b 100644
--- a/ocmonitor/services/report_generator.py
+++ b/ocmonitor/services/report_generator.py
@@ -5,6 +5,8 @@ from typing import Any, Dict, List, Optional
 
 from rich.console import Console
 from rich.panel import Panel
+from rich.table import Table
+from rich.text import Text
 
 from ..models.analytics import (
     DailyUsage,
@@ -20,6 +22,12 @@ from ..ui.tables import TableFormatter
 from ..utils.file_utils import FileProcessor
 
 
+# Below these widths the full multi-column tables cannot show every value
+# without collapsing columns, so a compact three-column layout is used.
+_WORKFLOW_TABLE_MIN_WIDTH = 220
+_SESSIONS_TABLE_MIN_WIDTH = 245
+
+
 class ReportGenerator:
     """Service for generating various types of reports."""
 
@@ -47,6 +55,186 @@ class ReportGenerator:
             return self.currency_converter.format(amount)
         return f"${amount:.2f}"
 
+    def _compact_session_cell(
+        self, session: SessionData, prefix: str = "", started: Optional[str] = None
+    ) -> Text:
+        """Optional start time, title and full id, stacked for narrow terminals."""
+        text = Text()
+        if started:
+            text.append(started, style="table.row.time")
+            text.append("\n")
+        text.append(f"{prefix}{session.display_title}", style="table.row.main")
+        text.append(f"\n{' ' * len(prefix)}ID: ", style="table.row.time")
+        text.append(session.session_id, style="table.row.model")
+        return text
+
+    def _compact_session_details(
+        self,
+        session: SessionData,
+        force_recalculate: bool = False,
+        include_project: bool = False,
+        include_duration: bool = False,
+    ) -> Text:
+        """Model/agent and usage, stacked to keep every value complete."""
+        cost = session.calculate_total_cost(
+            self.analyzer.pricing_data, force_recalculate
+        )
+        models = ", ".join(session.models_used) or "-"
+
+        first_line = []
+        if include_project and session.project_name:
+            first_line.append(session.project_name)
+        first_line.append(models)
+        first_line.append(session.agent or "-")
+
+        text = Text(" · ".join(first_line), style="table.row.model")
+        text.append("\n")
+
+        second_line = []
+        if include_duration:
+            second_line.append(
+                self.table_formatter._format_duration(session.duration_ms)
+                if session.duration_ms
+                else "N/A"
+            )
+        second_line.extend(
+            [
+                f"{session.interaction_count} interacciones",
+                f"{session.total_tokens.total:,} tokens",
+                self._fmt_cost(cost),
+            ]
+        )
+        text.append(" · ".join(second_line), style="table.row.tokens")
+        return text
+
+    def _display_workflow_sessions_compact_table(
+        self, workflows, force_recalculate: bool = False
+    ):
+        """Two-column layout for narrow terminals; folds instead of cropping."""
+        table = Table(
+            title="Session Workflows",
+            show_header=True,
+            header_style="table.header",
+            title_style="table.title",
+        )
+        table.add_column("Session", style="table.row.main", ratio=3, overflow="fold")
+        table.add_column("Details", style="table.row.model", ratio=2, overflow="fold")
+
+        for workflow in reversed(workflows):
+            workflow_cost = workflow.calculate_total_cost(
+                self.analyzer.pricing_data, force_recalculate
+            )
+            start_time = (
+                workflow.start_time.strftime("%Y-%m-%d %H:%M")
+                if workflow.start_time
+                else "N/A"
+            )
+
+            if workflow.has_sub_agents:
+                header = Text()
+                header.append(start_time, style="table.row.time")
+                header.append("\n")
+                header.append(workflow.display_title, style="table.row.main")
+                header.append("\nID: ", style="table.row.time")
+                header.append(workflow.workflow_id, style="table.row.model")
+
+                sub_label = (
+                    "sub-agente"
+                    if workflow.sub_agent_count == 1
+                    else "sub-agentes"
+                )
+                details = Text(
+                    f"{workflow.project_name} · {workflow.session_count} sesiones "
+                    f"({workflow.sub_agent_count} {sub_label})",
+                    style="table.row.model",
+                )
+                details.append("\n")
+                details.append(
+                    f"{sum(s.interaction_count for s in workflow.all_sessions)} "
+                    f"interacciones · {workflow.total_tokens.total:,} tokens · "
+                    f"{self._fmt_cost(workflow_cost)}",
+                    style="table.row.tokens",
+                )
+                table.add_row(header, details, style="table.row.main")
+
+                main = workflow.main_session
+                table.add_row(
+                    self._compact_session_cell(main, "  ├─ "),
+                    self._compact_session_details(main, force_recalculate),
+                    style="table.row.dim",
+                )
+                for i, sub in enumerate(workflow.sub_agent_sessions):
+                    is_last = i == len(workflow.sub_agent_sessions) - 1
+                    prefix = "  └─ " if is_last else "  ├─ "
+                    table.add_row(
+                        self._compact_session_cell(sub, prefix),
+                        self._compact_session_details(sub, force_recalculate),
+                        style="table.row.dim",
+                    )
+            else:
+                main = workflow.main_session
+                table.add_row(
+                    self._compact_session_cell(main, started=start_time),
+                    self._compact_session_details(
+                        main, force_recalculate, include_project=True
+                    ),
+                )
+
+        self.console.print(table)
+
+    def _display_sessions_compact_table(
+        self, sessions: List[SessionData], force_recalculate: bool = False
+    ):
+        """Three-column summary for narrow terminals (--no-group)."""
+        table = Table(
+            title="OpenCode Sessions Summary",
+            show_header=True,
+            header_style="table.header",
+            title_style="table.title",
+        )
+        table.add_column("Session", style="table.row.main", ratio=3, overflow="fold")
+        table.add_column("Details", style="table.row.model", ratio=2, overflow="fold")
+
+        sorted_sessions = sorted(sessions, key=lambda s: s.start_time or s.session_id)
+        for session in sorted_sessions:
+            start_time = (
+                session.start_time.strftime("%Y-%m-%d %H:%M:%S")
+                if session.start_time
+                else "N/A"
+            )
+            table.add_row(
+                self._compact_session_cell(session, started=start_time),
+                self._compact_session_details(
+                    session,
+                    force_recalculate,
+                    include_project=True,
+                    include_duration=True,
+                ),
+            )
+
+        self.console.print(table)
+
+    def _print_workflow_summary(
+        self, sessions: List[SessionData], workflows, force_recalculate: bool = False
+    ):
+        """Print the summary panel and workflow count for the sessions table."""
+        summary_panel = self.table_formatter.create_summary_panel(
+            sessions, self.analyzer.pricing_data, force_recalculate
+        )
+        self.console.print(summary_panel)
+
+        total_workflows = len(workflows)
+        total_with_subs = sum(1 for w in workflows if w.has_sub_agents)
+        if total_with_subs > 0:
+            workflow_info = (
+                f"{total_workflows} workflows ({total_with_subs} with sub-agents)"
+            )
+            self.console.print(
+                Panel(
+                    workflow_info, title="Workflow Summary", border_style="table.header"
+                )
+            )
+
     def _get_model_breakdown_for_sessions(
         self, sessions: List[SessionData], force_recalculate: bool = False
     ) -> List[Dict[str, Any]]:
@@ -803,10 +991,13 @@ class ReportGenerator:
         force_recalculate: bool = False,
     ):
         """Display sessions summary as table."""
-        table = self.table_formatter.create_sessions_table(
-            sessions, self.analyzer.pricing_data, force_recalculate
-        )
-        self.console.print(table)
+        if self.console.width < _SESSIONS_TABLE_MIN_WIDTH:
+            self._display_sessions_compact_table(sessions, force_recalculate)
+        else:
+            table = self.table_formatter.create_sessions_table(
+                sessions, self.analyzer.pricing_data, force_recalculate
+            )
+            self.console.print(table)
 
         summary_panel = self.table_formatter.create_summary_panel(
             sessions, self.analyzer.pricing_data, force_recalculate
@@ -826,6 +1017,15 @@ class ReportGenerator:
         grouper = SessionGrouper()
         workflows = grouper.group_sessions(sessions)
 
+        if self.console.width < _WORKFLOW_TABLE_MIN_WIDTH:
+            self._display_workflow_sessions_compact_table(
+                workflows, force_recalculate
+            )
+            self._print_workflow_summary(
+                sessions, workflows, force_recalculate
+            )
+            return
+
         table = Table(
             title="Session Workflows",
             show_header=True,
@@ -836,15 +1036,20 @@ class ReportGenerator:
         table.add_column("Started", style="table.row.time", no_wrap=True)
         table.add_column("Session ID", style="table.row.model", no_wrap=True)
         table.add_column(
-            "Session / Workflow", style="table.row.main", no_wrap=False, max_width=45
+            "Session / Workflow", style="table.row.main", no_wrap=False, overflow="fold"
+        )
+        table.add_column(
+            "Project", style="table.row.project", no_wrap=False, overflow="fold"
+        )
+        table.add_column(
+            "Provider", style="table.row.model", no_wrap=False, overflow="fold"
         )
         table.add_column(
-            "Project", style="table.row.project", no_wrap=True, max_width=15
+            "Model", style="table.row.model", no_wrap=False, overflow="fold"
         )
-        table.add_column("Provider", style="table.row.model", no_wrap=True, max_width=15)
-        table.add_column("Model", style="table.row.model", no_wrap=True, max_width=25)
         table.add_column(
-            "Agent", justify="center", style="table.row.model", no_wrap=True
+            "Agent", justify="center", style="table.row.model", no_wrap=False,
+            overflow="fold",
         )
         table.add_column("Interactions", justify="right", style="status.success")
         table.add_column("Tokens", justify="right", style="table.row.tokens")
@@ -860,8 +1065,6 @@ class ReportGenerator:
             if workflow.has_sub_agents:
                 # Show workflow summary row
                 title = workflow.display_title
-                if len(title) > 40:
-                    title = title[:37] + "..."
 
                 # Get start time for workflow
                 start_time = (
@@ -886,7 +1089,7 @@ class ReportGenerator:
                     f"[table.row.time]{start_time}[/table.row.time]",
                     f"[table.row.model]{workflow.workflow_id}[/table.row.model]",
                     f"[table.row.main]{title}[/table.row.main]",
-                    f"[table.row.project]{workflow.project_name[:15]}[/table.row.project]",
+                    f"[table.row.project]{workflow.project_name}[/table.row.project]",
                     f"[table.row.model]{provider}[/table.row.model]",
                     f"[table.row.model]{model}[/table.row.model]",
                     f"[table.row.model]+{workflow.sub_agent_count}[/table.row.model]",
@@ -902,8 +1105,6 @@ class ReportGenerator:
                     self.analyzer.pricing_data, force_recalculate
                 )
                 main_title = main.display_title
-                if len(main_title) > 38:
-                    main_title = main_title[:35] + "..."
 
                 # Get model for main session
                 main_models = main.models_used
@@ -938,8 +1139,6 @@ class ReportGenerator:
                         self.analyzer.pricing_data, force_recalculate
                     )
                     sub_title = sub.display_title
-                    if len(sub_title) > 38:
-                        sub_title = sub_title[:35] + "..."
 
                     # Get model for sub-agent session
                     sub_models = sub.models_used
@@ -972,8 +1171,6 @@ class ReportGenerator:
                     self.analyzer.pricing_data, force_recalculate
                 )
                 title = main.display_title
-                if len(title) > 42:
-                    title = title[:39] + "..."
 
                 # Get start time for single session
                 start_time = (
@@ -997,7 +1194,7 @@ class ReportGenerator:
                     start_time,
                     f"[table.row.model]{main.session_id}[/table.row.model]",
                     title,
-                    main.project_name[:15],
+                    main.project_name,
                     provider,
                     model,
                     main.agent or "-",
@@ -1008,26 +1205,7 @@ class ReportGenerator:
 
         self.console.print(table)
 
-        # Summary panel
-        total_workflows = len(workflows)
-        total_with_subs = sum(1 for w in workflows if w.has_sub_agents)
-        summary_panel = self.table_formatter.create_summary_panel(
-            sessions, self.analyzer.pricing_data, force_recalculate
-        )
-        self.console.print(summary_panel)
-
-        # Additional workflow info
-        if total_with_subs > 0:
-            from rich.panel import Panel
-
-            workflow_info = (
-                f"{total_workflows} workflows ({total_with_subs} with sub-agents)"
-            )
-            self.console.print(
-                Panel(
-                    workflow_info, title="Workflow Summary", border_style="table.header"
-                )
-            )
+        self._print_workflow_summary(sessions, workflows, force_recalculate)
 
     def _display_daily_breakdown_table(
         self,
diff --git a/ocmonitor/ui/tables.py b/ocmonitor/ui/tables.py
index 38fb522..8c297f2 100644
--- a/ocmonitor/ui/tables.py
+++ b/ocmonitor/ui/tables.py
@@ -97,10 +97,10 @@ class TableFormatter:
         # Add columns
         table.add_column("Started", style="table.row.time", no_wrap=True)
         table.add_column("Duration", style="table.row.time", no_wrap=True)
-        table.add_column("Session", style="table.row.main", max_width=35)
+        table.add_column("Session", style="table.row.main", overflow="fold")
         table.add_column("Session ID", style="table.row.model", no_wrap=True)
-        table.add_column("Provider", style="table.row.model", max_width=20)
-        table.add_column("Model", style="table.row.model", max_width=30)
+        table.add_column("Provider", style="table.row.model", overflow="fold")
+        table.add_column("Model", style="table.row.model", overflow="fold")
         table.add_column("Interactions", justify="right", style="status.success")
         table.add_column("Input Tokens", justify="right", style="table.row.tokens")
         table.add_column("Output Tokens", justify="right", style="table.row.tokens")
@@ -138,9 +138,6 @@ class TableFormatter:
                     start_time = session.start_time.strftime('%Y-%m-%d %H:%M:%S') if session.start_time else 'N/A'
                     duration = self._format_duration(session.duration_ms) if session.duration_ms else 'N/A'
                     session_display = session.display_title
-                    # Truncate if too long for display
-                    if len(session_display) > 35:
-                        session_display = session_display[:32] + "..."
                     session_id_display = session.session_id
                 else:
                     start_time = ""
@@ -150,8 +147,8 @@ class TableFormatter:
 
                 # Format model name
                 provider, model = FileProcessor.split_provider_model(display_model)
-                provider_text = Text(provider[:17] + "..." if len(provider) > 20 else provider)
-                model_text = Text(model[:27] + "..." if len(model) > 30 else model)
+                provider_text = Text(provider)
+                model_text = Text(model)
 
                 # Get cost color
                 cost_color = self.get_cost_color(stats['cost'])
```
