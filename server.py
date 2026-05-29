import json
import os
import tempfile
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from docling.document_converter import DocumentConverter

converter = DocumentConverter()
KNOWLEDGE_DIR = "/data/knowledge"
os.makedirs(KNOWLEDGE_DIR, exist_ok=True)

def parse_multipart(data, boundary):
    parts = data.split(b'--' + boundary)
    fields = {}
    filename = None
    filebody = None
    for part in parts:
        if b'Content-Disposition' in part:
            header, _, body = part.partition(b'\r\n\r\n')
            header_str = header.decode(errors='ignore')
            fn_match = re.search(r'filename="([^"]+)"', header_str)
            name_match = re.search(r'name="([^"]+)"', header_str)
            if fn_match:
                filename = fn_match.group(1)
                filebody = body.rstrip(b'\r\n--')
            elif name_match:
                fields[name_match.group(1)] = body.rstrip(b'\r\n--').decode(errors='ignore')
    return filename, filebody, fields

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
            return
        if self.path == '/list':
            result = []
            for dept in os.listdir(KNOWLEDGE_DIR):
                dept_path = os.path.join(KNOWLEDGE_DIR, dept)
                if os.path.isdir(dept_path):
                    for f in os.listdir(dept_path):
                        result.append({"department": dept, "file": f})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"files": result}).encode())
            return
        if self.path.startswith('/read'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            department = qs.get('department', [''])[0]
            filename = qs.get('file', [''])[0]
            md_path = os.path.join(KNOWLEDGE_DIR, department, filename)
            if not os.path.exists(md_path):
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "not found"}).encode())
                return
            with open(md_path) as mf:
                content = mf.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"department": department, "file": filename, "content": content}).encode())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        content_type = self.headers.get('Content-Type', '')
        length = int(self.headers.get('Content-Length', 0))
        data = self.rfile.read(length)

        if self.path == '/delete':
            try:
                payload = json.loads(data)
                filename = payload.get('filename', '')
                department = payload.get('department', 'geral')
                md_path = os.path.join(KNOWLEDGE_DIR, department, filename + ".md")
                if os.path.exists(md_path):
                    os.unlink(md_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if self.path == '/upload' and 'multipart/form-data' in content_type:
            boundary_match = re.search(r'boundary=([^\s;]+)', content_type)
            if not boundary_match:
                self.send_response(400); self.end_headers()
                self.wfile.write(json.dumps({"error": "no boundary"}).encode())
                return

            boundary = boundary_match.group(1).encode()
            filename, body, fields = parse_multipart(data, boundary)
            department = fields.get('department', 'geral')

            if not filename or body is None:
                self.send_response(400); self.end_headers()
                self.wfile.write(json.dumps({"error": "no file"}).encode())
                return

            dept_dir = os.path.join(KNOWLEDGE_DIR, department)
            os.makedirs(dept_dir, exist_ok=True)

            ext = os.path.splitext(filename)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(body)
                tmppath = tmp.name

            try:
                result = converter.convert(tmppath)
                markdown = result.document.export_to_markdown()
                output_path = os.path.join(dept_dir, filename + ".md")
                with open(output_path, "w") as f:
                    f.write(markdown)
                os.unlink(tmppath)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "output": output_path, "department": department, "chars": len(markdown)}).encode())
            except Exception as e:
                try: os.unlink(tmppath)
                except: pass
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        self.send_response(400); self.end_headers()
        self.wfile.write(json.dumps({"error": "invalid request"}).encode())

print("Docling service running on port 3002")
HTTPServer(("0.0.0.0", 3002), Handler).serve_forever()
