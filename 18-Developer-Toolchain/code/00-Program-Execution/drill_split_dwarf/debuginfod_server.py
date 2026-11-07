#!/usr/bin/env python3
import http.server
import os
import signal
import socketserver
import sys

PORT = 8765

class DebuginfodHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default noisy stderr logging

    def do_GET(self):
        # Format: /buildid/<id>/debuginfo
        parts = self.path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "buildid" and parts[2] == "debuginfo":
            bid = parts[1]
            target_file = f"symbols/{bid}.debug"
            if os.path.exists(target_file):
                with open(target_file, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                print(f"[debuginfod] SERVE /buildid/{bid[:8]}.../debuginfo (200 OK, {len(data)} bytes)", flush=True)
                return
        self.send_response(404)
        self.end_headers()

def main():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), DebuginfodHandler) as httpd:
        def handle_sig(sig, frame):
            sys.exit(0)
        signal.signal(signal.SIGTERM, handle_sig)
        signal.signal(signal.SIGINT, handle_sig)
        print(f"[debuginfod] listening on 127.0.0.1:{PORT}", flush=True)
        httpd.serve_forever()

if __name__ == "__main__":
    main()
