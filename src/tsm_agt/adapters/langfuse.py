"""Local configuration and OTLP/HTTP transport for Langfuse."""

from __future__ import annotations

import base64
import http.client
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml


class LangfuseExportError(RuntimeError):
    pass


def langfuse_config_path() -> Path:
    return Path.home() / ".tsm-agt" / "langfuse.yaml"


def attributes(values):
    return [
        {"key": key, "value": {"stringValue": value if isinstance(value, str) else json.dumps(value)}}
        for key, value in values.items()
        if value is not None
    ]


def trace_payload(service_name, spans):
    return {
        "resourceSpans": [{
            "resource": {"attributes": attributes({"service.name": service_name})},
            "scopeSpans": [{"scope": {"name": service_name}, "spans": spans}],
        }],
    }


class LangfuseClient:
    def __init__(self, base_url: str, public_key: str, secret_key: str):
        self.base_url = base_url.rstrip("/")
        try:
            self.endpoint = urlsplit(self.base_url)
            self.port = self.endpoint.port
        except ValueError:
            raise LangfuseExportError("Invalid Langfuse base URL") from None
        if (self.endpoint.scheme not in {"http", "https"} or not self.endpoint.hostname
                or self.endpoint.username is not None or self.endpoint.query or self.endpoint.fragment):
            raise LangfuseExportError("Langfuse base URL must be an HTTP(S) service URL")
        auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}

    @classmethod
    def from_config(cls, config: Path | None = None):
        path = config.expanduser() if config is not None else langfuse_config_path()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if config is None:
                return cls.from_env()
            raise LangfuseExportError(f"Langfuse configuration file not found: {path}") from None
        except (OSError, UnicodeError, yaml.YAMLError):
            # YAML parsing errors can quote the source line, including a key.
            raise LangfuseExportError(f"Could not read Langfuse YAML configuration: {path}") from None
        if not isinstance(data, dict):
            raise LangfuseExportError(f"Langfuse configuration must be a YAML mapping: {path}")
        fields = ("base_url", "public_key", "secret_key")
        invalid = [field for field in fields
                   if not isinstance(data.get(field), str) or not data[field].strip()]
        if invalid:
            raise LangfuseExportError(f"Set these Langfuse configuration fields in {path}: {', '.join(invalid)}")
        return cls(
            base_url=data["base_url"].strip(),
            public_key=data["public_key"].strip(),
            secret_key=data["secret_key"].strip(),
        )

    @classmethod
    def from_env(cls):
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
        if not public_key or not secret_key:
            raise LangfuseExportError("Create ~/.tsm-agt/langfuse.yaml or set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
        return cls(os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com", public_key, secret_key)

    def request(self, path, payload=None):
        connection_type = (http.client.HTTPSConnection if self.endpoint.scheme == "https"
                           else http.client.HTTPConnection)
        connection = connection_type(self.endpoint.hostname, self.port, timeout=15)
        try:
            connection.request(
                "GET" if payload is None else "POST", self.endpoint.path + path,
                body=None if payload is None else json.dumps(payload).encode(),
                headers={**self.headers, "x-langfuse-ingestion-version": "4"},
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise LangfuseExportError(f"Langfuse returned HTTP {response.status} for {path}")
            body = response.read()
            return json.loads(body) if body else {}
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as error:
            raise LangfuseExportError(f"Langfuse {path}: {type(error).__name__}") from None
        finally:
            connection.close()

    def send_trace(self, payload, *, description="trace"):
        result = self.request("/api/public/otel/v1/traces", payload)
        rejected = result.get("partialSuccess", {}).get("rejectedSpans")
        if rejected and int(rejected):
            raise LangfuseExportError(f"Langfuse rejected {rejected} spans for {description}")
