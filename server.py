import json
import os
import tempfile
import re
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from docling.document_converter import DocumentConverter

converter = DocumentConverter()
KNOWLEDGE_DIR = "/data/knowledge"
os.makedirs(KNOWLEDGE_DIR, exist_ok=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBED_MODEL = "text-embedding-3-small"
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200


# ---------------- Chunking ----------------
def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = start + size
        chunks.append(text[start:end])
        if end >= n:
            break
        start = end - overlap
        if start < 0:
            start = 0
    return [c.strip() for c in chunks if c.strip()]


# ---------------- OpenAI embeddings ----------------
def embed_texts(texts):
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_KEY}",
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=60).read().decode()
    data = json.loads(resp)
    return [item["embedding"] for item in data["data"]]


# ---------------- Supabase REST ----------------
def supabase_request(method, path, body=None, extra_headers=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=60).read().decode()


def delete_chunks(department, source_file):
    path = f"knowledge_chunks?department=eq.{urllib.parse.quote(department)}&source_file=eq.{urllib.parse.quote(source_file)}"
    supabase_request("DELETE", path)


def index_document(department, source_file, markdown, company_id=None):
    # Remove versões antigas do mesmo arquivo antes de reindexar
    try:
        delete_chunks(department, source_file)
    except Exception as e:
        print(f"[index] aviso ao limpar chunks antigos: {e}")

    chunks = chunk_text(markdown)
    if not chunks:
        return 0

    embeddings = embed_texts(chunks)
    rows = []
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        rows.append({
            "department": department,
            "company_id": company_id,
            "source_file": source_file,
            "chunk_index": i,
            "content": chunk,
            "embedding": emb,
        })
    supabase_request("POST", "knowledge_chunks", rows)
    return len(rows)


import urllib.parse


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        if self.path == '/health':
            self._json(200, {"status": "ok"})
            return
        if self.path == '/list':
            result = []
            for dept in os.listdir(KNOWLEDGE_DIR):
                dept_path = os.path.join(KNOWLEDGE_DIR, dept)
                if os.path.isdir(dept_path):
                    for f in os.listdir(dept_path):
                        result.append({"department": dept, "file": f})
            self._json(200, {"files": result})
            return
        if self.path.startswith('/read'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            department = qs.get('department', [''])[0]
            filename = qs.get('file', [''])[0]
            md_path = os.path.join(KNOWLEDGE_DIR, department, filename)
            if not os.path.exists(md_path):
                self._json(404, {"error": "not found"})
                return
            with open(md_path) as mf:
                content = mf.read()
            self._json(200, {"department": department, "file": filename, "content": content})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        content_type = self.headers.get('Content-Type', '')
        length = int(self.headers.get('Content-Length', 0))
        data = self.rfile.read(length)

        # ---------- Busca semântica ----------
        if self.path == '/search':
            try:
                payload = json.loads(data)
                query = payload.get('query', '')
                department = payload.get('department')
                match_count = payload.get('match_count', 5)
                q_emb = embed_texts([query])[0]
                rpc_body = {
                    "query_embedding": q_emb,
                    "match_count": match_count,
                    "filter_department": department,
                    "filter_company": payload.get("company_id"),
                }
                res = supabase_request("POST", "rpc/match_knowledge", rpc_body)
                self._json(200, {"results": json.loads(res)})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return


        # ---------- Delete ----------
        if self.path == '/delete':
            try:
                payload = json.loads(data)
                filename = payload.get('filename', '')
                department = payload.get('department', 'geral')
                md_path = os.path.join(KNOWLEDGE_DIR, department, filename + ".md")
                if os.path.exists(md_path):
                    os.unlink(md_path)
                try:
                    delete_chunks(department, filename + ".md")
                except Exception as e:
                    print(f"[delete] aviso ao remover chunks: {e}")
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        # ---------- Upload ----------
        if self.path == '/upload' and 'multipart/form-data' in content_type:
            boundary_match = re.search(r'boundary=([^\s;]+)', content_type)
            if not boundary_match:
                self._json(400, {"error": "no boundary"})
                return

            boundary = boundary_match.group(1).encode()
            filename, body, fields = parse_multipart(data, boundary)
            department = fields.get('department', 'geral')
            company_id = fields.get("company_id") or None

            if not filename or body is None:
                self._json(400, {"error": "no file"})
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

                indexed = 0
                index_error = None
                try:
                    indexed = index_document(department, filename + ".md", markdown, company_id)
                except Exception as e:
                    index_error = str(e)
                    print(f"[upload] erro ao indexar no pgvector: {e}")

                self._json(200, {
                    "ok": True,
                    "output": output_path,
                    "department": department,
                    "chars": len(markdown),
                    "chunks_indexed": indexed,
                    "index_error": index_error,
                })
            except Exception as e:
                try:
                    os.unlink(tmppath)
                except Exception:
                    pass
                self._json(500, {"error": str(e)})
            return

        self._json(400, {"error": "invalid request"})


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


print("Docling service running on port 3002")
HTTPServer(("0.0.0.0", 3002), Handler).serve_forever()
