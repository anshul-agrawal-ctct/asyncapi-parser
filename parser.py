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

# ---------------- FlatBuffers Parser ----------------
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

        # Strip comments
        content = re.sub(r"//.*", "", content)
        content = re.sub(r"/\*.*?\*/", "", content, flags=re.S)

        # Namespace
        if match := re.search(r"namespace\s+([\w\.]+)\s*;", content):
            self.data["namespace"] = match.group(1)

        # Attributes
        self.data["attributes"] = re.findall(r'attribute\s+"([^"]+)"\s*;', content)

        # Enums
        for m in re.finditer(r"enum\s+(\w+)\s*:\s*(\w+)\s*{([^}]*)}", content, re.S):
            name, base_type, body = m.groups()
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
            self.data["enums"][name] = {"base_type": base_type, "values": values}

        # Unions
        for m in re.finditer(r"union\s+(\w+)\s*{([^}]*)}", content, re.S):
            name, body = m.groups()
            types = [t.strip() for t in body.split(",") if t.strip()]
            self.data["unions"][name] = types

        # Structs
        for m in re.finditer(r"struct\s+(\w+)\s*{([^}]*)}", content, re.S):
            name, body = m.groups()
            self.data["structs"][name] = self._parse_fields(body)

        # Tables
        for m in re.finditer(r"table\s+(\w+)\s*{([^}]*)}", content, re.S):
            name, body = m.groups()
            self.data["tables"][name] = self._parse_fields(body)

        # Root type
        if match := re.search(r"root_type\s+(\w+)\s*;", content):
            self.data["root_type"] = match.group(1)

    def _parse_fields(self, body: str) -> Dict[str, Dict[str, Any]]:
        """Parse fields inside a struct or table"""
        fields = {}
        for line in body.split(";"):
            line = line.strip()
            if not line:
                continue
            if match := re.match(r"(\w+)\s*:\s*([\w\[\]]+)(\s*=\s*[^()]+)?(\s*\([^)]*\))?", line):
                name, ftype, default, meta = match.groups()
                fields[name] = {
                    "type": ftype.strip(),
                    "default": default.strip(" =") if default else None,
                    "metadata": meta.strip("()") if meta else None,
                }
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
            messages = []

            if channel_ref:
                channel_obj = self.resolve_ref(channel_ref)
                channel_name = channel_ref.split("/")[-1]
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
        template = env.from_string("""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{{ info.title }} - API Docs</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="p-4">
    <h1>{{ info.title }} <small class="text-muted">v{{ info.version }}</small></h1>
    <p>{{ info.description }}</p>

    <h2>Servers</h2>
    <ul>
    {% for name, srv in servers.items() %}
        <li><b>{{ srv.title }}</b>: {{ srv.host }} ({{ srv.protocol }} v{{ srv.protocolVersion }})</li>
    {% endfor %}
    </ul>

    <h2>Operations</h2>
    {% for op in operations %}
        <div class="card mb-3 shadow-sm">
            <div class="card-body">
                <h5 class="card-title">{{ op.operationId }}</h5>
                <p><b>Action:</b> {{ op.action }} | <b>Channel:</b> {{ op.channel }}</p>

                <h6>Messages</h6>
                <ul>
                {% for m in op.messages %}
                    <li>
                        <b>{{ m.name }}</b> ({{ m.schemaFormat }}) → {{ m.schema }}
                        {% if m.fbs_def %}
                            <div class="ms-3 mt-2">
                                {% if m.fbs_def.structs %}
                                    <h6>Structs</h6>
                                    {% for sname, sfields in m.fbs_def.structs.items() %}
                                        <b>{{ sname }}</b>
                                        <ul>
                                            {% for fname, fdata in sfields.items() %}
                                                <li>{{ fname }} : {{ fdata.type }} (default={{ fdata.default }}, meta={{ fdata.metadata }})</li>
                                            {% endfor %}
                                        </ul>
                                    {% endfor %}
                                {% endif %}

                                {% if m.fbs_def.tables %}
                                    <h6>Tables</h6>
                                    {% for tname, tfields in m.fbs_def.tables.items() %}
                                        <b>{{ tname }}</b>
                                        <ul>
                                            {% for fname, fdata in tfields.items() %}
                                                <li>{{ fname }} : {{ fdata.type }} (default={{ fdata.default }}, meta={{ fdata.metadata }})</li>
                                            {% endfor %}
                                        </ul>
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
        f.write("""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>API Documentation Index</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="p-4">
    <div class="container">
        <h1 class="mb-4">API Documentation Index</h1>
        <div class="list-group">
""")
        for name, filename in generated_files:
            f.write(f'            <a href="{filename}" class="list-group-item list-group-item-action">{name}</a>\n')
        f.write("""        </div>
    </div>
</body>
</html>""")
    logging.info(f"📑 Index generated: {index_path}")


# ---------------- Entry Point ----------------
if __name__ == "__main__":
    generate_all(asyncapi_path="./api", fbs_dir="./flatbuffers", output_dir="./docs")