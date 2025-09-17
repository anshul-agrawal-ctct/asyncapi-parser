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
            action = action.capitalize()
            channel_ref = op.get("channel", {}).get("$ref")
            channel_name = None
            channel_desc = None
            messages = []

            if channel_ref:
                channel_obj = self.resolve_ref(channel_ref)
                channel_name = channel_ref.split("/")[-1]
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
    <h1>{{ info.title }} <small class="text-muted">{{ info.version }}</small></h1>
    <p class="text-secondary-emphasis">{{ info.description }}</p>

    <p class="fs-2 text-dark-emphasis mt-5">Servers</p>
    <ul class="list-group">
    {% for name, srv in servers.items() %}
        <li class="list-group-item p-4 bg-light">
            <span class="fs-5 text-primary-emphasis">{{ srv.protocol }}://{{ srv.host }}/</span>
            <b>
                <span class='text-white bg-success p-1 rounded-1'>{{ srv.protocol }} {{ srv.protocolVersion }}</span>
                <span class='text-white bg-primary p-1 rounded-1'>{{ srv.title }}</span>
            </b>
        </li>
    {% endfor %}
    </ul>

    <p class="fs-2 text-dark-emphasis mt-5">Operations</p>
    {% for op in operations %}
        <div class="card mb-3 shadow-sm bg-light">
            <div class="card-body">
                <h5 class="card-title mt-2">{{ op.operationId }}</h5>
                <p class="mt-3">
                    <span class='text-white bg-primary p-1 rounded-1 me-3'><b>{{ op.action }}</b></span>
                    {{ op.channel }}
                    <div>
                        <h6>Description: </h6>
                        {{ op.channel_desc }}              
                    </div>
                </p>
                <h6>Accepts the following message:</h6>
                <ul class="list-group">
                {% for m in op.messages %}
                    <li class="list-group-item p-4">
                        <div class="m-2">
                            <span>Schema</span><span class='text-white bg-warning ms-3 p-1 rounded-1'><b>{{ m.name }}</b></span>
                        </div>
                        <div class="m-2">
                            <span>Schema Format</span><span class='text-white bg-info ms-3 p-1 rounded-1'><b>{{ m.schemaFormat }}</b></span>
                        </div>
                        {% if m.fbs_def %}
                            <div class="ms-3 mt-2 pt-3">
                                {% if m.fbs_def.structs %}
                                    {% for sname, sfields in m.fbs_def.structs.items() %}
                                        <h6>{{ sname }}</h6>
                                        <table class="table table-borderless">
                                            <thead>
                                                <tr>
                                                <th style="width: 250px;">Field Name</th>
                                                <th>Type</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {% for fname, fdata in sfields.items() %}
                                                <tr>
                                                <td style="width: 250px;"><i>{{ fname }}</i></td>
                                                <td><span class="fw-bold text-success">{{ fdata.type }}</span></td>
                                                </tr>
                                                {% endfor %}
                                            </tbody>
                                        </table>
                                    {% endfor %}
                                {% endif %}

                                {% if m.fbs_def.tables %}
                                    {% for tname, tfields in m.fbs_def.tables.items() %}
                                        <h6>{{ tname }}</h6>
                                        <table class="table table-borderless">
                                            <thead>
                                                <tr>
                                                <th style="width: 250px;">Field Name</th>
                                                <th>Type</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {% for fname, fdata in tfields.items() %}
                                                <tr>
                                                <td style="width: 250px;"><i>{{ fname }}</i></td>
                                                <td><span class="fw-bold text-success">{{ fdata.type }}</span></td>
                                                </tr>
                                                {% endfor %}
                                            </tbody>
                                        </table>
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
        for key in servers:
            servers[key]['protocol'] = servers[key]['protocol'].upper()
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
            output_file = os.path.join(output_dir, f"files/{base_name}.html")

            asyncapi_parser = AsyncAPIParser(asyncapi_file)
            if asyncapi_parser.validate():
                docgen = HTMLDocGenerator(asyncapi_parser, base_fbs_dir=fbs_dir)
                docgen.generate(output_file)
                generated_files.append((base_name, os.path.basename(output_file)))
        except Exception as e:
            logging.error(f"Failed to process {asyncapi_file}: {e}")

    # files.json
    json_path = os.path.join(output_dir, "files.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(generated_files, f)
    logging.info(f"📑 JSON file generated: {json_path}")


# ---------------- Entry Point ----------------
if __name__ == "__main__":
    generate_all(asyncapi_path="./api", fbs_dir="./flatbuffers", output_dir="./docs")
