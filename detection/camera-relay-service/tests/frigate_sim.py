"""Stands in for a node's Frigate on 8971: TLS with a self-signed certificate. It answers with the
headers it received and, on purpose, the secret, which the API must strip."""

import json
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        secret = self.headers.get("X-Proxy-Secret", "")
        body = json.dumps(
            {
                "path": self.path,
                "secret_sha256": __import__("hashlib").sha256(secret.encode()).hexdigest(),
                "secret_echo": secret,
                "header_names": sorted(name.lower() for name in self.headers),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Proxy-Secret", secret)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = HTTPServer(("0.0.0.0", 8971), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/sim/cert.pem", "/sim/key.pem")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
