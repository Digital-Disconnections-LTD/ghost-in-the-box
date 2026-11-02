"""What every Ghost in the Box MCP server shares: transport, protocol, schemas and catalogs.

The transport is standard MCP stdio (newline-delimited JSON), plus Content-Length
frames for framed clients such as Batbox, answered in kind. A server module
supplies its tools, its instructions and a function that turns one tool call
into a structured result; this module does the rest.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import json
import os
import re
import sys

from farm import config
from farm.cli_output import error_object

# Newest first. Batching is only part of 2025-03-26, so it is accepted only there.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
BATCH_PROTOCOL_VERSIONS = frozenset({"2025-03-26"})
MAX_MESSAGE_BYTES = 1024 * 1024
DOCTOR_HINT = "run ghost-in-the-box doctor"
WITHHELD = "the tool failed; details are withheld so session data and provider responses cannot leak"


class ToolInputError(ValueError):
    """An explicitly safe local validation message with a stable code and next step."""

    def __init__(self, message, *, code="invalid_input", hint=None):
        super().__init__(message)
        self.code = code
        self.hint = hint


class AccountSelectionError(Exception):
    """Why a server cannot pick an account, and what to run instead."""

    def __init__(self, message, hint):
        super().__init__(message)
        self.hint = hint


def negotiate_protocol(requested):
    """Echo a supported client version; otherwise offer our newest one."""
    return requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION


def server_version():
    return config.package_version()


# --- account selection -------------------------------------------------------

def select_account(explicit, *, platform, label, env_var, environ, registry_factory, valid_handle,
                   invalid_message, invalid_hint, placeholders, login_hint):
    """Pick the account to serve: explicit, then env_var, then the only registered one.

    valid_handle(text) -> bool decides the platform's handle shape. A documentation
    placeholder is refused unless that exact name is really registered.
    """
    environ = os.environ if environ is None else environ
    candidate = (explicit or environ.get(env_var) or "").strip()
    if candidate and not valid_handle(candidate):
        raise AccountSelectionError(invalid_message, invalid_hint)
    try:
        registry = registry_factory()
        if candidate:
            if candidate.casefold() in placeholders and registry.get(platform, candidate) is None:
                raise AccountSelectionError(
                    f"'{candidate}' is the documentation placeholder, not a registered {label} account",
                    "pass your own handle with --account; to register it, " + login_hint("YOUR_HANDLE"))
            return candidate
        handles = sorted(entry["account"] for entry in registry.list() if entry["platform"] == platform)
    except AccountSelectionError:
        raise
    except (OSError, ValueError) as error:
        raise AccountSelectionError(
            f"the account registry could not be read ({type(error).__name__})",
            "run ghost-in-the-box doctor and ghost-in-the-box accounts list, or pass --account HANDLE") from None
    if len(handles) == 1:
        return handles[0]
    if not handles:
        raise AccountSelectionError(f"no {label} account is registered and --account was not given",
                                    login_hint("YOUR_HANDLE") + ", or pass --account HANDLE")
    raise AccountSelectionError(
        f"{len(handles)} {label} accounts are registered ({', '.join(handles)}); choose one",
        f"pass --account HANDLE (or set {env_var})")


# --- input schemas -----------------------------------------------------------

def string(description, *, minimum=1, maximum=4096, pattern=None, default=None):
    result = {"type": "string", "description": description,
              "minLength": minimum, "maxLength": maximum}
    if pattern:
        result["pattern"] = pattern
    if default is not None:
        result["default"] = default
    return result


def integer(description, default, low, high):
    return {"type": "integer", "description": description, "default": default,
            "minimum": low, "maximum": high}


def boolean(description, default=False):
    return {"type": "boolean", "description": description, "default": default}


def choice(description, values, default=None):
    result = {"type": "string", "description": description, "enum": values}
    if default is not None:
        result["default"] = default
    return result


def make_tool(prefix, name, title, description, properties=None, required=(), *, read_only=False,
              external=False, destructive=False):
    # title is the 2025-06-18 field; annotations.title serves 2025-03-26 clients.
    return {"name": f"{prefix}_{name}", "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": list(required), "additionalProperties": False},
            "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": destructive,
                            "openWorldHint": external}}


def validate(value, schema, field="arguments"):
    """Validate the small JSON Schema subset the servers advertise before side effects."""
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "string": isinstance(value, str),
             "array": isinstance(value, list), "integer": type(value) is int,
             "boolean": type(value) is bool}.get(kind, False)
    if not valid:
        raise ToolInputError(f"{field} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolInputError(f"{field} has an unsupported value")
    if kind == "object":
        properties = schema["properties"]
        if any(key not in properties for key in value):
            raise ToolInputError("arguments contains an unknown field")
        for required in schema.get("required", []):
            if required not in value:
                raise ToolInputError(f"{required} is required")
        for key, item in value.items():
            validate(item, properties[key], key)
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", MAX_MESSAGE_BYTES):
            raise ToolInputError(f"{field} has an invalid length")
        if schema.get("minLength", 0) and not value.strip():
            raise ToolInputError(f"{field} must not be blank")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            raise ToolInputError(f"{field} has an invalid format")
    elif kind == "integer":
        if not schema["minimum"] <= value <= schema["maximum"]:
            raise ToolInputError(f"{field} is outside its allowed bounds")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise ToolInputError(f"{field} has an invalid number of items")
        for item in value:
            validate(item, schema["items"], field)


def prepare_call(tools_by_name, name, arguments, *, unknown):
    """Validate one call against its schema; return the arguments with defaults filled in."""
    if name not in tools_by_name:
        raise ToolInputError(unknown, code="unknown_tool", hint="call tools/list for the available names")
    schema = tools_by_name[name]["inputSchema"]
    try:
        validate(arguments, schema)
    except ToolInputError as error:
        raise ToolInputError(str(error), code="invalid_arguments",
                             hint=f"match the inputSchema of {name} from tools/list") from None
    args = {key: prop["default"] for key, prop in schema["properties"].items() if "default" in prop}
    args.update(arguments)
    return args


# --- tool results ------------------------------------------------------------

def tool_error(code, message, hint, *, fallback_hint=DOCTOR_HINT):
    return error_object(code, message, hint, fallback_hint=fallback_hint)


def quiet_call(function, *args):
    """Run a tool with stdout and stderr silenced: diagnostics must never corrupt framing."""
    with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
        return function(*args)


def tool_content(result):
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": not result.get("ok", False)}


# --- JSON-RPC ----------------------------------------------------------------

def invalid(request_id=None, message="Invalid request"):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": message}}


class Server:
    """One MCP server: its identity, tools, and the function that answers a tool call."""

    def __init__(self, *, name, title, instructions, tools, call, unknown_tool):
        self.name = name
        self.title = title
        self.instructions = instructions
        self.tools = tools
        self.tool_names = frozenset(item["name"] for item in tools)
        self.call = call
        self.unknown_tool = unknown_tool

    def dispatch(self, service, message, session=None):
        """Answer one JSON-RPC message; session remembers the negotiated protocol version."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return invalid()
        if "id" not in message:
            return None  # Notifications never execute tools and never receive replies.
        request_id = message["id"]
        method = message.get("method")
        params = message.get("params", {})
        response = {"jsonrpc": "2.0", "id": request_id}
        if not isinstance(params, dict) or not isinstance(method, str):
            return invalid(request_id)
        elif method == "initialize":
            version = negotiate_protocol(params.get("protocolVersion"))
            if session is not None:
                session["protocol"] = version
            response["result"] = {"protocolVersion": version, "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "title": self.title, "version": server_version()},
                "instructions": self.instructions}
        elif method == "ping":
            response["result"] = {}
        elif method == "tools/list":
            response["result"] = {"tools": self.tools}
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in self.tool_names:
                response["error"] = {"code": -32602, "message": self.unknown_tool}
            else:
                response["result"] = self.call(service, name, arguments)
        elif method in ("resources/list", "resources/templates/list", "prompts/list"):
            key = {"resources/list": "resources", "resources/templates/list": "resourceTemplates",
                   "prompts/list": "prompts"}[method]
            response["result"] = {key: []}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        return response

    def handle(self, service, message, session):
        """Answer one message, or a JSON-RPC batch when 2025-03-26 was negotiated."""
        if not isinstance(message, list):
            return self.dispatch(service, message, session)
        if session.get("protocol") not in BATCH_PROTOCOL_VERSIONS or not message:
            return invalid()
        replies = []
        for item in message:
            if isinstance(item, dict) and item.get("method") == "initialize":
                reply = invalid(item.get("id"), "initialize cannot be batched") if "id" in item else None
            else:
                reply = self.dispatch(service, item, session)
            if reply is not None:
                replies.append(reply)
        return replies or None

    def serve(self, service, stdin=None, stdout=None):
        stdin = stdin or sys.stdin.buffer
        stdout = stdout or sys.stdout.buffer
        session = {}
        framing = "framed"
        while True:
            try:
                message, framing = read_message(stdin)
            except (ValueError, UnicodeError):
                # Invalid framing cannot safely be resynchronized. No raw payloads.
                write_message(stdout, {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Invalid JSON-RPC message"}}, framing)
                return 2
            if message is None:
                return 0
            if isinstance(message, dict) and message.get("method") == "notifications/exit" and "id" not in message:
                return 0
            response = self.handle(service, message, session)
            if response is not None:
                write_message(stdout, response, framing)


def read_message(stream):
    """Accept MCP newline JSON and Content-Length frames; respond in the caller's format."""
    line = stream.readline(8193)
    while line and not line.strip():
        line = stream.readline(8193)
    if not line:
        return None, "framed"
    if line.lstrip().startswith((b"{", b"[")):
        if not line.endswith(b"\n"):
            tail = stream.readline(MAX_MESSAGE_BYTES - len(line) + 1)
            line += tail
        if len(line) > MAX_MESSAGE_BYTES:
            raise ValueError("message too large")
        return json.loads(line), "lines"
    total = len(line)
    length = None
    while line not in (b"\r\n", b"\n"):
        if not line or total > 8192:
            raise ValueError("invalid frame headers")
        key, separator, value = line.partition(b":")
        if not separator:
            raise ValueError("invalid frame header")
        if key.strip().lower() == b"content-length":
            if length is not None:
                raise ValueError("duplicate length")
            length = int(value.strip())
        line = stream.readline(8193)
        total += len(line)
    if length is None or not 0 < length <= MAX_MESSAGE_BYTES:
        raise ValueError("invalid frame length")
    chunks = bytearray()
    while len(chunks) < length:
        data = stream.read(length - len(chunks))
        if not data:
            raise ValueError("incomplete frame")
        chunks.extend(data)
    return json.loads(chunks), "framed"


def write_message(stream, message, framing="framed"):
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write((f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
                 if framing == "framed" else body + b"\n")
    stream.flush()


# --- catalogs ----------------------------------------------------------------

def first_sentence(text):
    match = re.match(r"(.+?[.!?])(?:\s|$)", " ".join(text.split()))
    return match.group(1) if match else " ".join(text.split())


def tool_catalog(tools):
    """One-line summaries of tool definitions, for docs and quick inspection."""
    entries = []
    for item in tools:
        schema = item["inputSchema"]
        required = list(schema.get("required", []))
        entries.append({"name": item["name"], "title": item["title"],
                        "description": first_sentence(item["description"]),
                        "required": required,
                        "optional": [key for key in schema["properties"] if key not in required],
                        "read_only": item["annotations"]["readOnlyHint"],
                        "contacts_provider": item["annotations"]["openWorldHint"]})
    return entries


def catalog_document(generated_by, servers):
    return {"generated_by": generated_by, "version": server_version(),
            "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS), "servers": servers}


def _cell(value):
    return " ".join(str(value).split()).replace("|", "\\|")


def _access(entry):
    if entry["read_only"] is None:
        return ""
    if entry["read_only"]:
        return "reads the provider" if entry["contacts_provider"] else "local read"
    return "writes; contacts the provider" if entry["contacts_provider"] else "local write"


def catalog_markdown(data, *, default_generator="ghost-in-the-box instagram-mcp --list-tools"):
    generator = data.get("generated_by") or default_generator
    lines = ["# MCP tool catalog", "",
             f"Generated by `{generator} --format md` from Ghost in the Box "
             f"{data['version']}. Supported MCP protocol versions: {', '.join(data['protocol_versions'])}.", ""]
    for server in data["servers"]:
        lines += [f"## {server['server']}", "", f"Start: `{server['command']}`", ""]
        if server.get("note"):
            lines += [server["note"], ""]
        lines += ["| Tool | Title | What it does | Required | Optional | Access |",
                  "|---|---|---|---|---|---|"]
        for entry in server["tools"]:
            lines.append("| " + " | ".join([
                f"`{entry['name']}`", _cell(entry["title"]), _cell(entry["description"]),
                ", ".join(f"`{name}`" for name in entry["required"]),
                ", ".join(f"`{name}`" for name in entry["optional"]), _access(entry)]) + " |")
        lines.append("")
    return "\n".join(lines).rstrip("\n")
