from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import select
import socket
import socketserver
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any


DEFAULT_PORT = 8888
CONNECT_TIMEOUT_SECONDS = 20
SOCKET_BUFFER_SIZE = 64 * 1024


class ProxyAuthRequired(PermissionError):
    pass


def normalize_domain(domain: str) -> str:
    parsed = urllib.parse.urlsplit(domain if "://" in domain else f"https://{domain}")
    host = parsed.hostname or domain
    normalized = host.lower().strip(".")
    if not normalized:
        raise ValueError("domain is empty")
    if any(char in normalized for char in "/*?#[ ]"):
        raise ValueError(f"invalid domain: {domain}")
    return normalized


def domain_matches(host: str, allowed_domains: list[str]) -> bool:
    normalized_host = normalize_domain(host)
    for raw_domain in allowed_domains:
        raw = raw_domain.strip().lower()
        wildcard = raw.startswith("*.")
        if wildcard:
            raw = raw[2:]
        domain = normalize_domain(raw)
        if normalized_host == domain:
            return True
        if wildcard and normalized_host.endswith("." + domain):
            return True
        if not wildcard and normalized_host.endswith("." + domain):
            return True
    return False


def normalize_domain_pattern(domain: str) -> str:
    raw = domain.strip().lower()
    if raw.startswith("*."):
        return "*." + normalize_domain(raw[2:])
    return normalize_domain(raw)


def is_global_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return parsed.is_global


def resolve_global_addresses(host: str, port: int) -> list[tuple[Any, ...]]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"no DNS results for {host}")
    private_or_local: list[str] = []
    public_infos: list[tuple[Any, ...]] = []
    for info in infos:
        address = str(info[4][0])
        if is_global_address(address):
            public_infos.append(info)
        else:
            private_or_local.append(address)
    if private_or_local:
        raise PermissionError(
            f"DNS resolved {host} to non-global address(es): {', '.join(private_or_local[:5])}"
        )
    if not public_infos:
        raise PermissionError(f"DNS resolved {host} to no global addresses")
    return public_infos


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign_payload(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return b64url_encode(digest)


def create_egress_token(
    *,
    allowed_domains: list[str],
    secret: str,
    purpose: str = "network-tool",
    ttl_seconds: int = 900,
) -> str:
    if not secret:
        raise ValueError("egress proxy signing secret is required")
    normalized = [normalize_domain_pattern(domain) for domain in allowed_domains if domain.strip()]
    if not normalized:
        raise ValueError("allowed_domains must not be empty")
    payload = {
        "allowed_domains": normalized,
        "purpose": purpose,
        "exp": int(time.time()) + max(1, min(int(ttl_seconds), 3600)),
    }
    payload_b64 = b64url_encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    )
    return f"{payload_b64}.{sign_payload(payload_b64, secret)}"


def verify_egress_token(token: str, secret: str) -> list[str]:
    if not secret:
        raise PermissionError("egress proxy signing secret is not configured")
    try:
        payload_b64, signature = token.rsplit(".", 1)
    except ValueError as exc:
        raise PermissionError("invalid egress token format") from exc
    expected = sign_payload(payload_b64, secret)
    if not hmac.compare_digest(signature, expected):
        raise PermissionError("invalid egress token signature")
    try:
        payload = json.loads(b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise PermissionError("invalid egress token payload") from exc
    if int(payload.get("exp") or 0) < int(time.time()):
        raise PermissionError("egress token expired")
    domains = payload.get("allowed_domains")
    if not isinstance(domains, list):
        raise PermissionError("egress token missing allowed_domains")
    return [normalize_domain_pattern(str(domain)) for domain in domains if str(domain).strip()]


def parse_domains_csv(value: str) -> list[str]:
    return [normalize_domain_pattern(item.strip()) for item in value.split(",") if item.strip()]


class EgressProxyConfig:
    def __init__(self, *, signing_secret: str, default_allowlist: list[str]) -> None:
        self.signing_secret = signing_secret
        self.default_allowlist = default_allowlist


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class EgressProxyHandler(BaseHTTPRequestHandler):
    server_version = "SandboxEgressProxy/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def proxy_config(self) -> EgressProxyConfig:
        return self.server.proxy_config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("EGRESS_PROXY_LOG", "").lower() in {"1", "true", "yes"}:
            super().log_message(fmt, *args)

    def do_CONNECT(self) -> None:
        try:
            host, port = self.parse_connect_target(self.path)
            self.validate_destination(host, port)
            upstream = self.open_upstream(host, port)
        except Exception as exc:
            self.reject(exc)
            return
        try:
            self.send_response(200, "Connection established")
            self.end_headers()
            self.relay_bidirectional(self.connection, upstream)
        finally:
            upstream.close()

    def do_GET(self) -> None:
        self.forward_http_request()

    def do_HEAD(self) -> None:
        self.forward_http_request()

    def do_POST(self) -> None:
        self.forward_http_request()

    def do_PUT(self) -> None:
        self.forward_http_request()

    def do_PATCH(self) -> None:
        self.forward_http_request()

    def do_DELETE(self) -> None:
        self.forward_http_request()

    def do_OPTIONS(self) -> None:
        self.forward_http_request()

    def parse_connect_target(self, value: str) -> tuple[str, int]:
        if ":" not in value:
            raise ValueError("CONNECT target must include host:port")
        host, port_text = value.rsplit(":", 1)
        return normalize_domain(host), int(port_text)

    def parse_http_target(self) -> tuple[str, int, str]:
        url = self.path
        if "://" not in url:
            host_header = self.headers.get("Host", "")
            if not host_header:
                raise ValueError("HTTP proxy request missing Host header")
            url = f"http://{host_header}{url}"
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("only http and https URLs are supported")
        host = normalize_domain(parsed.hostname or "")
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        return host, port, path

    def auth_allowed_domains(self) -> list[str]:
        header = (self.headers.get("Proxy-Authorization") or "").strip()
        if not header:
            return list(self.proxy_config.default_allowlist)
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
        elif scheme.lower() == "basic":
            decoded = base64.b64decode(value.strip()).decode("utf-8", errors="strict")
            token = decoded.split(":", 1)[0]
        else:
            raise PermissionError("unsupported proxy authorization scheme")
        return verify_egress_token(token, self.proxy_config.signing_secret)

    def validate_destination(self, host: str, port: int) -> None:
        if port not in {80, 443}:
            raise PermissionError(f"port {port} is not allowed")
        has_auth = bool((self.headers.get("Proxy-Authorization") or "").strip())
        if not has_auth and not domain_matches(host, self.proxy_config.default_allowlist):
            raise ProxyAuthRequired(f"proxy authentication required for {host}")
        allowed_domains = self.auth_allowed_domains()
        if not domain_matches(host, allowed_domains):
            raise PermissionError(f"host {host} is outside proxy allowlist")
        resolve_global_addresses(host, port)

    def open_upstream(self, host: str, port: int) -> socket.socket:
        errors: list[Exception] = []
        for family, socktype, proto, _canonname, sockaddr in resolve_global_addresses(host, port):
            upstream = socket.socket(family, socktype, proto)
            upstream.settimeout(CONNECT_TIMEOUT_SECONDS)
            try:
                upstream.connect(sockaddr)
                upstream.settimeout(None)
                return upstream
            except Exception as exc:
                errors.append(exc)
                upstream.close()
        if errors:
            raise errors[-1]
        raise OSError(f"could not connect to {host}:{port}")

    def forward_http_request(self) -> None:
        try:
            host, port, path = self.parse_http_target()
            self.validate_destination(host, port)
            upstream = self.open_upstream(host, port)
            content_length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(content_length) if content_length else b""
            request_head = self.build_origin_request(path)
            upstream.sendall(request_head + body)
            self.relay_to_client(upstream)
        except Exception as exc:
            self.reject(exc)
            return
        finally:
            try:
                upstream.close()  # type: ignore[has-type]
            except Exception:
                pass

    def build_origin_request(self, path: str) -> bytes:
        lines = [f"{self.command} {path} HTTP/1.1"]
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in {
                "proxy-authorization",
                "proxy-connection",
                "connection",
                "keep-alive",
                "te",
                "trailers",
                "transfer-encoding",
                "upgrade",
            }:
                continue
            lines.append(f"{key}: {value}")
        lines.append("Connection: close")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")

    def relay_to_client(self, upstream: socket.socket) -> None:
        while True:
            try:
                data = upstream.recv(SOCKET_BUFFER_SIZE)
            except (ConnectionResetError, BrokenPipeError, OSError):
                break
            if not data:
                break
            try:
                self.connection.sendall(data)
            except (ConnectionResetError, BrokenPipeError, OSError):
                break

    def relay_bidirectional(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = [client, upstream]
        while True:
            try:
                readable, _writable, exceptional = select.select(sockets, [], sockets, 120)
            except OSError:
                break
            if exceptional:
                break
            if not readable:
                break
            for current in readable:
                other = upstream if current is client else client
                try:
                    data = current.recv(SOCKET_BUFFER_SIZE)
                except (ConnectionResetError, BrokenPipeError, OSError):
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except (ConnectionResetError, BrokenPipeError, OSError):
                    return

    def reject(self, exc: Exception) -> None:
        message = f"{exc.__class__.__name__}: {exc}"
        body = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode("utf-8")
        try:
            status = 407 if isinstance(exc, ProxyAuthRequired) else 403
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            if isinstance(exc, ProxyAuthRequired):
                self.send_header("Proxy-Authenticate", 'Basic realm="sandbox-egress"')
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass


def run_proxy(host: str, port: int, config: EgressProxyConfig) -> None:
    with ThreadingHTTPServer((host, port), EgressProxyHandler) as server:
        server.proxy_config = config  # type: ignore[attr-defined]
        print(
            "egress-proxy listening "
            f"on {host}:{port}; default_allowlist={','.join(config.default_allowlist)}",
            flush=True,
        )
        server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Allowlist HTTP CONNECT/HTTP egress proxy.")
    parser.add_argument("--host", default=os.environ.get("EGRESS_PROXY_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("EGRESS_PROXY_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument(
        "--default-allowlist",
        default=os.environ.get("EGRESS_PROXY_DEFAULT_ALLOWLIST", "api.openai.com"),
    )
    args = parser.parse_args()
    secret = os.environ.get("EGRESS_PROXY_SIGNING_SECRET", "")
    config = EgressProxyConfig(
        signing_secret=secret,
        default_allowlist=parse_domains_csv(args.default_allowlist),
    )
    run_proxy(args.host, args.port, config)


if __name__ == "__main__":
    main()
