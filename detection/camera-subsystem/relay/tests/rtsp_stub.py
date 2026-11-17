"""Stand-ins for the redirect attack, speaking just enough RTSP.

- ``redirect ROUTES``: a host on the site. Answers each DESCRIBE with a 302, to the Location
  routes.json holds for the requested path.
- ``capture``: a listener off the site, on 8554. Challenges for a login, and logs each connection
  and whether a login arrived. It never prints the login.
"""

import json
import socketserver
import sys
from urllib.parse import urlsplit


def record(line):
    # One write. print() writes the newline separately, so threads could join two lines into one,
    # and the smoke test counts lines.
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def respond(status, cseq, extra=""):
    return f"RTSP/1.0 {status}\r\nCSeq: {cseq}\r\n{extra}\r\n".encode()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = self.client_address[0]
        if MODE == "capture":
            record(f"CONNECT from {peer}")
        while True:
            request = self.rfile.readline().decode("latin-1").split()
            if not request:
                return
            headers = {}
            while (line := self.rfile.readline().decode("latin-1").strip()):
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            method, url = request[0], request[1]
            cseq = headers.get("cseq", "0")
            if method == "OPTIONS":
                self.wfile.write(respond("200 OK", cseq, "Public: DESCRIBE\r\n"))
            elif MODE == "redirect":
                location = ROUTES.get(urlsplit(url).path, "rtsp://192.0.2.1:8554/x")
                record(f"REDIRECT from {peer}")
                self.wfile.write(respond("302 Found", cseq, f"Location: {location}\r\n"))
            elif "authorization" in headers:
                record(f"LOGIN from {peer}")
                self.wfile.write(respond("404 Not Found", cseq))
            else:
                challenge = 'WWW-Authenticate: Basic realm="x"\r\n'
                self.wfile.write(respond("401 Unauthorized", cseq, challenge))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


MODE = sys.argv[1]
ROUTES = json.load(open(sys.argv[2])) if MODE == "redirect" else {}
Server(("0.0.0.0", 8554), Handler).serve_forever()
