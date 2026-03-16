#!/usr/bin/env python3
import json, os, http.server, urllib.parse, threading, secrets, requests, warnings
warnings.filterwarnings('ignore')

with open('/Users/air/Downloads/client_secret_453822230599-f89tbo2edqd8l1pqrolqklchjfldfnkm.apps.googleusercontent.com.json') as f:
    cfg = json.load(f)['installed']

CLIENT_ID = cfg['client_id']
CLIENT_SECRET = cfg['client_secret']
REDIRECT_URI = 'http://localhost:8080/'
SCOPES = 'https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/documents'

state = secrets.token_urlsafe(16)
auth_url = (
    'https://accounts.google.com/o/oauth2/auth'
    f'?response_type=code'
    f'&client_id={CLIENT_ID}'
    f'&redirect_uri={urllib.parse.quote(REDIRECT_URI)}'
    f'&scope={urllib.parse.quote(SCOPES)}'
    f'&state={state}'
    f'&access_type=offline'
    f'&prompt=consent'
)

# Save URL to file so it can be read externally
with open('/tmp/google_auth_url.txt', 'w') as f:
    f.write(auth_url)

print(f"Auth URL saved to /tmp/google_auth_url.txt", flush=True)
print(f"\nOPEN THIS URL:\n{auth_url}\n", flush=True)

# Start local server to catch callback
code_received = threading.Event()
auth_code = [None]

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if 'code' in params:
            auth_code[0] = params['code'][0]
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h1>Authentication successful! You can close this tab.</h1>')
            code_received.set()
        else:
            self.send_response(400)
            self.end_headers()
    def log_message(self, *args):
        pass

server = http.server.HTTPServer(('localhost', 8080), Handler)
t = threading.Thread(target=server.serve_forever)
t.daemon = True
t.start()
print("Waiting for OAuth callback on localhost:8080...", flush=True)

code_received.wait(timeout=300)
server.shutdown()

if not auth_code[0]:
    print("ERROR: Timed out waiting for auth code")
    exit(1)

# Exchange code for tokens
resp = requests.post('https://oauth2.googleapis.com/token', data={
    'code': auth_code[0],
    'client_id': CLIENT_ID,
    'client_secret': CLIENT_SECRET,
    'redirect_uri': REDIRECT_URI,
    'grant_type': 'authorization_code',
})
tokens = resp.json()
if 'error' in tokens:
    print("ERROR:", tokens)
    exit(1)

token_data = {
    "token": tokens.get('access_token'),
    "refresh_token": tokens.get('refresh_token'),
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "scopes": SCOPES.split(),
}

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token.json')
with open(out, 'w') as f:
    json.dump(token_data, f, indent=2)

print(f"token.json saved! Refresh token length: {len(tokens.get('refresh_token', ''))}")
