"""The host plugin and adapter link never leaves this machine."""
from http.client import HTTPConnection, HTTPSConnection
from http.server import ThreadingHTTPServer
import json
import os
import socket
from socketserver import ThreadingMixIn
import ssl
import stat

from .storage import require


def private_directory(path):
    info = path.lstat()
    require(path.is_absolute() and path.is_dir() and not path.is_symlink()
            and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
            403, "private_runtime_required")
    if os.name == "nt":
        from .windows_acl import private_directory as private_acl
        private = private_acl(path)
    else:
        private = info.st_uid == os.getuid() and not info.st_mode & 0o077
    require(private, 403, "private_runtime_required")
    return path


def private_file(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
            403, "private_dsh_link_required")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if os.name == "nt":
            from .windows_acl import private_file as private_acl
            private = private_acl(stream.fileno())
        else:
            private = opened.st_uid == os.getuid() and not opened.st_mode & 0o077
        require(private and os.path.samestat(info, opened), 403, "private_dsh_link_required")
    return path


class UnixConnection(HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=8)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class LocalLink:
    def __init__(self, directory, *, use_tls=False):
        self.directory = private_directory(directory)
        require(os.name != "nt" or use_tls, 422, "dsh_local_tls_required")
        self.ports = None
        if not use_tls:
            return
        cfg = json.loads(private_file(directory / "dsh-link.json").read_text(encoding="utf-8"))
        require(isinstance(cfg, dict) and set(cfg) == {"host_port", "worker_port"}
                and all(type(port) is int and 1024 <= port <= 65535 for port in cfg.values())
                and cfg["host_port"] != cfg["worker_port"], 422, "invalid_dsh_local_ports")
        cert = private_file(directory / "dsh-link.crt")
        key = private_file(directory / "dsh-link.key")
        self.client = ssl.create_default_context(cafile=cert)
        self.server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server.verify_mode = ssl.CERT_REQUIRED
        self.server.load_verify_locations(cafile=cert)
        for context in (self.client, self.server):
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
        self.ports = cfg

    def connect_host(self):
        private_directory(self.directory)
        if self.ports:
            return HTTPSConnection("127.0.0.1", self.ports["host_port"], timeout=8, context=self.client)
        return UnixConnection(self.directory / "dsh.sock")

    def listen_for_tools(self, handler):
        endpoint = private_directory(self.directory) / "worker.sock"
        if self.ports:
            return TlsTools(("127.0.0.1", self.ports["worker_port"]), handler, self.server), None

        from socketserver import UnixStreamServer

        class Server(ThreadingMixIn, UnixStreamServer):
            daemon_threads = True

        server = Server(str(endpoint), handler)
        return server, endpoint


class TlsTools(ThreadingHTTPServer):
    allow_reuse_address = False
    allow_reuse_port = False

    def __init__(self, address, handler, context):
        self.context = context
        super().__init__(address, handler)

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request_thread(self, request, client_address):
        # Handshake off the accept loop so an unauthenticated client cannot stop other callbacks.
        try:
            request.settimeout(8)
            with self.context.wrap_socket(request, server_side=True) as session:
                self.finish_request(session, client_address)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)
