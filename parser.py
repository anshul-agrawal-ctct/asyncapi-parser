import os
import re
import glob
import json
import yaml
import logging
from typing import Any, Dict, List, Optional
from jinja2 import Environment, FileSystemLoader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

FLATBUFFERS_BUILTINS = {
    "bool", "byte", "ubyte",
    "short", "ushort", "int", "uint",
    "float", "double", "long", "ulong",
    "int8", "uint8", "int16", "uint16",
    "int32", "uint32", "int64", "uint64",
    "float32", "float64",
    "string"
}

# ---------------- FBS Parser ----------------
class FBSParser:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.data: Dict[str, Any] = {
            "namespace": None,
            "attributes": [],
            "enums": {},
            "unions": {},
            "structs": {},
            "tables": {},
            "root_type": None,
        }
        self._parse_file()

    def _parse_file(self):
        if not os.path.exists(self.file_path):
            logging.warning(f"FBS file not found: {self.file_path}")
            return

        with open(self.file_path, "r", encoding="utf-8") as f:
            content = f.read()

        lines = content.splitlines()
        current_doc: List[str] = []

        def consume_doc() -> str:
            nonlocal current_doc
            doc = "\n".join(current_doc).strip() if current_doc else None
            current_doc = []
            return doc

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            # --- Doc comments
            if line.startswith("///"):
                current_doc.append(line.lstrip("/ ").strip())
                i += 1
                continue

            # --- Block comments
            if line.startswith("/*"):
                block = [line.lstrip("/* ").rstrip("*/").strip()]
                while not lines[i].strip().endswith("*/"):
                    i += 1
                    block.append(lines[i].strip("/* ").strip("*/").strip())
                current_doc.extend([b for b in block if b])
                i += 1
                continue

            # Namespace
            if match := re.match(r"namespace\s+([\w\.]+)\s*;", line):
                self.data["namespace"] = match.group(1)
                i += 1
                continue

            # Attribute
            if match := re.match(r'attribute\s+"([^"]+)"\s*;', line):
                self.data["attributes"].append(match.group(1))
                i += 1
                continue

            # Enum
            if match := re.match(r"enum\s+(\w+)\s*:\s*(\w+)\s*{", line):
                name, base_type = match.groups()
                body, offset = self._collect_block(lines, i)
                self.data["enums"][name] = {
                    "base_type": base_type,
                    "values": self._parse_enum_values(body),
                    "doc": consume_doc(),
                }
                i = offset
                continue

            # Union
            if match := re.match(r"union\s+(\w+)\s*{", line):
                name = match.group(1)
                body, offset = self._collect_block(lines, i)
                types = [t.strip() for t in body.split(",") if t.strip()]
                self.data["unions"][name] = {"types": types, "doc": consume_doc()}
                i = offset
                continue

            # Struct
            if match := re.match(r"struct\s+(\w+)\s*{", line):
                name = match.group(1)
                body, offset = self._collect_block(lines, i)
                self.data["structs"][name] = {
                    "fields": self._parse_fields(body),
                    "doc": consume_doc(),
                }
                i = offset
                continue

            # Table
            if match := re.match(r"table\s+(\w+)\s*{", line):
                name = match.group(1)
                body, offset = self._collect_block(lines, i)
                self.data["tables"][name] = {
                    "fields": self._parse_fields(body),
                    "doc": consume_doc(),
                }
                i = offset
                continue

            # Root type
            if match := re.match(r"root_type\s+(\w+)\s*;", line):
                self.data["root_type"] = match.group(1)
                i += 1
                continue

            i += 1

    def _collect_block(self, lines: List[str], start_idx: int) -> (str, int):
        content = []
        depth = 0
        i = start_idx
        while i < len(lines):
            depth += lines[i].count("{")
            depth -= lines[i].count("}")
            content.append(lines[i])
            if depth == 0:
                break
            i += 1
        body = "\n".join(content)
        # Remove opening and closing braces
        body = re.sub(r"^[^{]*{", "", body, count=1, flags=re.S)
        body = re.sub(r"}[^}]*$", "", body, count=1, flags=re.S)
        return body.strip(), i + 1

    def _parse_enum_values(self, body: str) -> Dict[str, Any]:
        values = {}
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                k, v = item.split("=")
                values[k.strip()] = int(v.strip())
            else:
                values[item] = None
        return values

    def _parse_fields(self, body: str) -> Dict[str, Dict[str, Any]]:
        fields: Dict[str, Dict[str, Any]] = {}
        current_doc: List[str] = []

        # Collect all local types
        local_types = set(self.data["enums"].keys()) | set(self.data["structs"].keys()) | set(self.data["tables"].keys())

        for raw_line in body.splitlines():
            line = raw_line.strip()

            # Triple slash doc
            if line.startswith("///"):
                current_doc.append(line.lstrip("/ ").strip())
                continue

            # Block doc inside fields
            if line.startswith("/*"):
                block_line = line.strip("/* ").strip("*/").strip()
                if block_line:
                    current_doc.append(block_line)
                continue

            if not line or line.startswith("}"):
                continue

            if match := re.match(
                r"(\w+)\s*:\s*([\w\[\]]+)(\s*=\s*[^()]+)?(\s*\([^)]*\))?", line
            ):
                name, ftype, default, meta = match.groups()
                ftype = ftype.strip()
                fields[name] = {
                    "type": ftype,
                    "default": default.strip(" =") if default else None,
                    "metadata": meta.strip("()") if meta else None,
                    "doc": "\n".join(current_doc).strip() if current_doc else None,
                    "ref": ftype if ftype in local_types else None,
                }
                current_doc = []
        return fields


# ---------------- AsyncAPI Parser ----------------
class AsyncAPIParser:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.data = self._load_file()

    def _load_file(self) -> Dict[str, Any]:
        with open(self.file_path, "r", encoding="utf-8") as f:
            content = f.read()
            try:
                return yaml.safe_load(content)
            except yaml.YAMLError:
                return json.loads(content)

    def validate(self) -> bool:
        required_fields = ["asyncapi", "info", "channels"]
        for field in required_fields:
            if field not in self.data:
                raise ValueError(f"Missing required field: {field}")
        return True

    def resolve_ref(self, ref: str) -> Optional[Any]:
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        parts = ref.lstrip("#/").split("/")
        node = self.data
        try:
            for p in parts:
                node = node[p]
            return node
        except Exception:
            return None

    def get_operations(self, base_fbs_dir: str) -> List[Dict[str, Any]]:
        """Extract operations, parse linked FBS files"""
        ops = []
        for op_name, op in self.data.get("operations", {}).items():
            action = op.get("action")
            channel_ref = op.get("channel", {}).get("$ref")
            channel_name = None
            channel_address = None
            channel_desc = None
            messages = []

            if channel_ref:
                channel_obj = self.resolve_ref(channel_ref)
                channel_name = channel_ref.split("/")[-1]
                channel_address = channel_obj['address']
                channel_desc = channel_obj['description']
                if channel_obj and "messages" in channel_obj:
                    for msg_name, msg_obj in channel_obj["messages"].items():
                        schema_val = None
                        schema_format = None
                        fbs_file = None
                        fbs_def = None

                        if isinstance(msg_obj, dict):
                            payload = msg_obj.get("payload", {})
                            if isinstance(payload, dict):
                                schema_val = payload.get("schema")
                                schema_format = payload.get("schemaFormat")

                                if schema_val and schema_val.endswith(".fbs"):
                                    fbs_file = os.path.join(base_fbs_dir, schema_val)
                                    fbs_def = FBSParser(fbs_file).data

                        messages.append({
                            "name": msg_name,
                            "schema": schema_val,
                            "schemaFormat": schema_format,
                            "fbs_file": fbs_file,
                            "fbs_def": fbs_def,
                        })

            ops.append({
                "operationId": op_name,
                "action": action,
                "channel": channel_name,
                "channel_ref": channel_ref,
                "channel_address": channel_address,
                "channel_desc": channel_desc,
                "messages": messages,
            })
        return ops


# ---------------- HTML Generator ----------------
class HTMLDocGenerator:
    def __init__(self, asyncapi_parser: AsyncAPIParser, base_fbs_dir: str):
        self.parser = asyncapi_parser
        self.base_fbs_dir = base_fbs_dir

    def generate(self, output_file: str):
        env = Environment(loader=FileSystemLoader("."))
        template = env.from_string("""<!DOCTYPE html><html>
<head>
    <meta charset="UTF-8">
    <title>{{ info.title }} - API Docs</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="p-5">
    <h1>{{ info.title }} {{ info.version }}</h1>
    <p class="mt-4" style="color: #637383;">{{ info.description }}</p>

    <p class="fs-2 mt-5" style="font-weight: 500;">Servers</p>
    <ul class="list-group">
    {% for name, srv in servers.items() %}
        <li class="list-group-item p-4" style="background-color:#edf2f7; box-shadow: 0.5px 0.5px 5px rgba(169,169,169,0.5);">
            <span class="fs-5 text-primary-emphasis">{{ srv.protocol }}://{{ srv.host }}/</span>
            <b>
                <span class='text-white bg-success p-1 px-2 mx-2 rounded-1'>{{ srv.protocol | upper }} {{ srv.protocolVersion }}</span>
                <span class='text-white bg-primary p-1 px-2 rounded-1'>{{ name }}</span>
            </b>
        </li>
    {% endfor %}
    </ul>

    <p class="fs-2 mt-5" style="font-weight: 500;">Operations</p>
    {% for op in operations %}
        <div>
            <div class="card-body">
                <p class="mt-3">
                    <span class='text-white bg-success p-1 rounded-1 me-3'>{{ op.action | upper}}</span>
                    <span class="fs-5 text-primary-emphasis">{{ op.channel_address }}</span>
                    <div>
                        {{ op.channel_desc }}              
                    </div>
                </p>
                <div class="p-3 border rounded mb-2" style="background-color:#f7fafc">
                    <span>Operation ID</span>
                    <span class='text-white ms-3 p-1 rounded-1' style="background-color:#FF7440">{{ op.operationId }}</span>
                </div>
                <h6>Accepts the following message:</h6>
                <ul class="list-group">
                {% for m in op.messages %} 
                    <li class="list-group-item p-4" style="background-color:#edf2f7; box-shadow: 0.5px 0.5px 5px rgba(169,169,169,0.5);">
                        <div class="p-3 border rounded" style="background-color:#f7fafc">
                            <span>Message ID</span><span class='text-white ms-3 p-1 rounded-1' style="background-color:#FF7440">{{ m.schema }}</span>
                        </div>
                        {% if m.fbs_def %}
                        <div class="mt-2 pt-3">

                            {# ----------- Structs ----------- #}
                            {% if m.fbs_def.structs %}
                                {% for sname, sfields in m.fbs_def.structs.items() %}
                                    <div class="mb-4">
                                        <span>Payload </span>
                                        <span class="fw-bold" style="color:#3AB3AD">{{ sname }}</span>
                                        {% if sfields.doc %}
                                            <p class="text-muted small mb-2">{{ sfields.doc }}</p>
                                        {% endif %}

                                        <div class="p-3 border rounded" style="background-color:#f7fafc">
                                            <table class="table-borderless">
                                                <tbody>
                                                    {% for fname, fdata in sfields.fields.items() %}
                                                    <tr>
                                                        <td style="width: 250px;">{{ fname }}</td>
                                                        <td>
                                                            <i class="fw-bold" style="color:#3AB3AD">{{ fdata.type }}</i>

                                                            {# If this type is a derived reference, highlight it #}
                                                            {% if fdata.ref %}
                                                                <span class="badge bg-secondary ms-2">Ref → {{ fdata.ref }}</span>
                                                            {% endif %}

                                                            {% if fdata.doc %}
                                                                <div class="text-muted small">{{ fdata.doc }}</div>
                                                            {% endif %}
                                                        </td>
                                                    </tr>
                                                    {% endfor %}
                                                </tbody>
                                            </table>
                                        </div>
                                    </div>
                                {% endfor %}
                            {% endif %}

                            {# ----------- Tables ----------- #}
                            {% if m.fbs_def.tables %}
                                {% for tname, tfields in m.fbs_def.tables.items() %}
                                    <div class="mb-4">
                                        <span>Payload </span>
                                        <span class="fw-bold" style="color:#3AB3AD">{{ tname }}</span>
                                        {% if tfields.doc %}
                                            <p class="text-muted small mb-2">{{ tfields.doc }}</p>
                                        {% endif %}

                                        <div class="p-3 border rounded" style="background-color:#f7fafc">
                                            <table class="table-borderless">
                                                <tbody>
                                                    {% for fname, fdata in tfields.fields.items() %}
                                                    <tr>
                                                        <td style="width: 250px;">{{ fname }}</td>
                                                        <td>
                                                            <i class="fw-bold" style="color:#3AB3AD">{{ fdata.type }}</i>

                                                            {# Show derived type reference if available #}
                                                            {% if fdata.ref %}
                                                                <span class="badge bg-secondary ms-2">Ref → {{ fdata.ref }}</span>
                                                            {% endif %}

                                                            {% if fdata.doc %}
                                                                <div class="text-muted small">{{ fdata.doc }}</div>
                                                            {% endif %}
                                                        </td>
                                                    </tr>
                                                    {% endfor %}
                                                </tbody>
                                            </table>
                                        </div>
                                    </div>
                                {% endfor %}
                            {% endif %}
                        </div>
                        {% endif %}
                    </li>
                {% endfor %}
                </ul>
            </div>
        </div>
    {% endfor %}
</body>
</html>""")

        info = self.parser.data.get("info", {})
        servers = self.parser.data.get("servers", {})
        operations = self.parser.get_operations(self.base_fbs_dir)
        html = template.render(info=info, servers=servers, operations=operations)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html)

        logging.info(f"✅ Generated: {output_file}")


# ---------------- Batch Runner ----------------
def generate_all(asyncapi_path: str, fbs_dir: str, output_dir: str):
    os.makedirs(output_dir, exist_ok=True)

    asyncapi_files = []
    if os.path.isdir(asyncapi_path):
        asyncapi_files = glob.glob(os.path.join(asyncapi_path, "*.yaml")) \
                       + glob.glob(os.path.join(asyncapi_path, "*.yml")) \
                       + glob.glob(os.path.join(asyncapi_path, "*.json"))
    elif os.path.isfile(asyncapi_path):
        asyncapi_files = [asyncapi_path]

    if not asyncapi_files:
        logging.warning(f"No AsyncAPI files found in {asyncapi_path}")
        return

    generated_files = []
    for asyncapi_file in asyncapi_files:
        try:
            base_name = os.path.splitext(os.path.basename(asyncapi_file))[0]
            output_file = os.path.join(output_dir, f"{base_name}.html")

            asyncapi_parser = AsyncAPIParser(asyncapi_file)
            if asyncapi_parser.validate():
                docgen = HTMLDocGenerator(asyncapi_parser, base_fbs_dir=fbs_dir)
                docgen.generate(output_file)
                generated_files.append((base_name, os.path.basename(output_file)))
        except Exception as e:
            logging.error(f"Failed to process {asyncapi_file}: {e}")

    # index.html
    index_path = os.path.join(output_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("""<!DOCTYPE html><html lang="en"><head>
    <meta charset="UTF-8">
    <title>API Documentation</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {
            height: 100vh;
            overflow: hidden;
        }
        .sidebar {
            height: 100vh;
            border-right: 1px solid #dee2e6;
        }
        .sidebar .list-group-item {
            border: none;
            font-weight: 500;
            cursor: pointer;
        }
        iframe {
            width: 100%;
            height: 100%;
            border: none;
        }
    </style>
</head>
<body>
    <div class="container-fluid h-100">
        <div class="row h-100">

            <!-- Sidebar -->
            <div class="col-3 col-md-2 bg-light sidebar p-3">
                <h4 class="mb-4">APIs</h4>
                <!-- Search Bar -->
                <input type="text" id="searchInput" class="form-control mb-2" placeholder="Search APIs...">
                <!-- List Group -->
                <div class="list-group" id="apiList">
""")
        for name, filename in generated_files:
            f.write(f'            <button class="list-group-item list-group-item-action" onclick="loadPage(\'{filename}\')">{name}</button>\n')
        f.write("""        </div>
                <!-- No results message -->
                <div id="noResults" class="text-muted small mt-2 d-none">No APIs found.</div>
            </div>

            <!-- Main Content -->
            <div class="col-9 col-md-10 p-0">
                <iframe id="contentFrame" src="" title="API Documentation"></iframe>
            </div>
        </div>
    </div>

    <script>
        function loadPage(url) {
            document.getElementById('contentFrame').src = url;
        }
        document.addEventListener('DOMContentLoaded', function () {
            const input = document.getElementById('searchInput');
            const list = document.getElementById('apiList');
            const noResults = document.getElementById('noResults');

            if (!input || !list) {
                console.error('Search input or api list not found in DOM.');
                return;
            }

            // collect items once for static list; if your list is dynamic, re-query inside the handler
            const items = Array.from(list.querySelectorAll('.list-group-item'));
            console.log('Found API items:', items.length);

            input.addEventListener('input', function (e) {
                const q = e.target.value.trim().toLowerCase();
                let visible = 0;

                items.forEach(item => {
                    // use textContent so anchors/buttons also work
                    const text = item.textContent.trim().toLowerCase();
                    if (q === '' || text.includes(q)) {
                        item.classList.remove('d-none');
                        visible++;
                    } else {
                        item.classList.add('d-none');
                    }
                });

                // show or hide "no results"
                noResults.classList.toggle('d-none', visible > 0);
            });
        });
    </script>
</body>
</html>""")
    logging.info(f"📑 Index generated: {index_path}")


# ---------------- Entry Point ----------------
if __name__ == "__main__":
    generate_all(asyncapi_path="./api", fbs_dir="./flatbuffers", output_dir="./docs")