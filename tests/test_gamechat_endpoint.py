"""Exercise the real HTTP client against a local game-chat-shaped server."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import engine


class GameChatEndpointTests(unittest.TestCase):
    def test_local_origin_and_last_id(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                body = json.dumps([{'id': 42, 'sender': 'Pilot', 'msg': 'Cover me!',
                                    'mode': 'Team', 'enemy': False}]).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.object(engine, 'API_ROOT', f'http://127.0.0.1:{server.server_port}'):
                engine._drop_chat_conn()
                self.assertEqual(engine.fetch_chat(41)[0]['id'], 42)
                self.assertEqual(requests, ['/gamechat?lastId=41'])
        finally:
            engine._drop_chat_conn()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
