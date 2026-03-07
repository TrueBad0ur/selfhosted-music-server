from http.server import HTTPServer, BaseHTTPRequestHandler
import os, cgi, base64

# Папка назначения для загрузки
UPLOAD_DIR = "/uploads"
INDEX_HTML = "/app/upload.html"

# Логин и пароль для Basic Auth
USERNAME = "admin"
PASSWORD = "NFiu1_Fjn1f_g2no10GF!_f1fo1f1Fq1"

class SimpleUploader(BaseHTTPRequestHandler):

    def do_AUTHHEAD(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Uploader"')
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()

    def check_auth(self):
        auth_header = self.headers.get('Authorization')
        if auth_header is None:
            return False
        try:
            auth_type, encoded = auth_header.split(' ', 1)
            if auth_type.lower() != 'basic':
                return False
            decoded = base64.b64decode(encoded.strip()).decode('utf-8')
            username, password = decoded.split(':', 1)
            return username == USERNAME and password == PASSWORD
        except Exception:
            return False

    def do_GET(self):
        if not self.check_auth():
            self.do_AUTHHEAD()
            self.wfile.write(b"<h3>401 Unauthorized</h3>")
            return

        if self.path == '/':
            with open(INDEX_HTML, 'rb') as f:
                self.send_response(200)
                self.send_header('Content-type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(f.read())
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        if not self.check_auth():
            self.do_AUTHHEAD()
            self.wfile.write(b"<h3>401 Unauthorized</h3>")
            return

        if self.path != '/upload':
            self.send_error(404, "Not Found")
            return

        ctype, pdict = cgi.parse_header(self.headers.get('Content-Type'))
        if ctype == 'multipart/form-data':
            pdict['boundary'] = bytes(pdict['boundary'], "utf-8")
            pdict['CONTENT-LENGTH'] = int(self.headers['Content-Length'])
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={'REQUEST_METHOD':'POST'},
                keep_blank_values=True
            )

            files = form['file']
            if not isinstance(files, list):
                files = [files]

            uploaded_files = []

            os.makedirs(UPLOAD_DIR, exist_ok=True)
            for uploaded in files:
                if uploaded.filename:
                    filename = os.path.basename(uploaded.filename)
                    save_path = os.path.join(UPLOAD_DIR, filename)
                    with open(save_path, 'wb') as f:
                        f.write(uploaded.file.read())
                    uploaded_files.append(filename)

            if uploaded_files:
                self.send_response(200)
                self.send_header('Content-type', 'text/html; charset=utf-8')
                self.end_headers()
                files_list = "<br>".join(f"✅ {f}" for f in uploaded_files)
                self.wfile.write(f"""
                <h3>✅ Uploaded files:</h3>
                <p>{files_list}</p>
                <a href="/">Back</a>
                """.encode('utf-8'))
            else:
                self.send_error(400, "No files selected")
        else:
            self.send_error(400, "Invalid request")

if __name__ == "__main__":
    port = 8091
    print(f"Uploader running on http://0.0.0.0:{port} (Basic Auth enabled, multi-file upload supported)")
    HTTPServer(('0.0.0.0', port), SimpleUploader).serve_forever()
