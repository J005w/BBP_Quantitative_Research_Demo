"""Real loopback HTTP checks for the read-only offline playback."""

from __future__ import annotations

import copy
import http.client
import json
import re
import socket
import threading
import unittest
from urllib.parse import quote

from bbp_demo.dashboard import MAX_REQUEST_BODY, TOKEN_HEADER, make_server, render_dashboard


REPORT = {
    "evidence": "SYNTHETIC_CORRECTNESS_ONLY",
    "coverage": {"source": "deterministic synthetic", "bars": 3},
    "assumptions": {"cost_per_trade": 0.25},
    "bars": [{"time": 1_700_000_000 + i * 60, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15} for i in range(3)],
    "decisions": [{"time": 1_700_000_060, "available_at": 1_700_000_000, "action": "ACCEPT", "reason": "known data only"}],
    "trades": [], "accounting": {"reconciled": True},
    "results": {"trade_count": 0, "net_profit": 0.0, "native_raw_ohlc": {"trade_count": 0}},
}


class RenderingTests(unittest.TestCase):
    def test_payload_cannot_break_out_of_json_or_create_html(self):
        report = copy.deepcopy(REPORT)
        attack = '</script><script>alert("injected")</script><img src=x onerror=alert(1)>&\u2028'
        report["decisions"][0]["reason"] = attack
        page = render_dashboard(report)
        payload = re.search(r'<script id="report-data"[^>]*>(.*?)</script>', page, re.S).group(1)
        self.assertNotIn(attack, page)
        self.assertNotIn("<", payload)
        self.assertNotIn("&", payload)
        self.assertEqual(json.loads(payload), report)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<script src=", page)

    def test_report_placeholder_strings_remain_unchanged(self):
        report = {"note": "__NONCE__ __TOKEN__ __REPORT__"}
        page = render_dashboard(report, token="abc", nonce="fixed")
        payload = re.search(r'<script id="report-data"[^>]*>(.*?)</script>', page, re.S).group(1)
        self.assertEqual(json.loads(payload), report)

    def test_nonfinite_report_rejected_before_socket_bind(self):
        with self.assertRaises(ValueError):
            make_server({"value": float("nan")})

    def test_public_bind_and_nonliteral_names_rejected(self):
        for host in ("0.0.0.0", "::", "192.0.2.1", "example.com", "127.0.0.2", "127.1", "2130706433"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                make_server(REPORT, host)

    def test_invalid_ports_rejected(self):
        for port in (-1, 65536, "8765", True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                make_server(REPORT, port=port)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = copy.deepcopy(REPORT)
        cls.server = make_server(cls.source)
        cls.port = cls.server.server_address[1]
        cls.authority = f"127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()
        status, _, page = cls.request("GET", "/")
        if status != 200:
            raise AssertionError("Playback root failed")
        cls.token = json.loads(re.search(r'<script id="session-data"[^>]*>(.*?)</script>', page.decode(), re.S).group(1))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    @classmethod
    def request(cls, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_root_security_headers_and_nonce_match(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        nonce = re.search(rb'<style nonce="([^"]+)"', body).group(1).decode()
        self.assertIn(f"'nonce-{nonce}'", headers["Content-Security-Policy"])
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertNotIn(b"__REPORT__", body)

    def test_each_page_has_fresh_csp_nonce(self):
        first = self.request("GET", "/")[1]["Content-Security-Policy"]
        second = self.request("GET", "/")[1]["Content-Security-Policy"]
        self.assertNotEqual(first, second)

    def test_api_accepts_session_header_and_query(self):
        for path, headers in (("/api/report", {TOKEN_HEADER: self.token}), ("/api/report?token=" + quote(self.token), {})):
            with self.subTest(path=path):
                status, response_headers, body = self.request("GET", path, headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), REPORT)
                self.assertEqual(response_headers["Content-Type"], "application/json; charset=utf-8")
                self.assertNotIn("Access-Control-Allow-Origin", response_headers)

    def test_api_rejects_missing_wrong_duplicate_and_conflicting_tokens(self):
        cases = [("/api/report", {}), ("/api/report?token=wrong", {}),
                 (f"/api/report?token={self.token}&token={self.token}", {}),
                 (f"/api/report?token={self.token}", {TOKEN_HEADER: "wrong"}),
                 (f"/api/report?token={self.token}&extra=1", {}),
                 ("/api/report?token=%E2%98%83", {})]
        for path, headers in cases:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, headers)[0], 403)

    def test_session_tokens_do_not_authorize_other_servers(self):
        other = make_server(REPORT)
        thread = threading.Thread(target=other.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", other.server_address[1], timeout=3)
            connection.request("GET", "/api/report", headers={TOKEN_HEADER: self.token})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
        finally:
            other.shutdown()
            other.server_close()
            thread.join(timeout=2)

    def test_bad_hosts_rejected_on_root_and_authenticated_api(self):
        for host in ("evil.example", "evil.example:" + str(self.port), "127.0.0.1", "127.0.0.1:1", "127.0.0.1.evil:" + str(self.port), "user@" + self.authority):
            for path in ("/", "/api/report"):
                with self.subTest(host=host, path=path):
                    self.assertEqual(self.request("GET", path, {"Host": host, TOKEN_HEADER: self.token})[0], 403)

    def test_localhost_and_same_origin_allowed(self):
        for host in (self.authority, f"localhost:{self.port}"):
            self.assertEqual(self.request("GET", "/api/report", {"Host": host, "Origin": "http://" + host, TOKEN_HEADER: self.token})[0], 200)

    def test_cross_origin_and_cross_site_rejected_even_with_valid_token(self):
        for origin in ("https://evil.example", "null", "http://127.0.0.1:1", "http://" + self.authority + "/", "https://" + self.authority):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("GET", "/api/report", {"Origin": origin, TOKEN_HEADER: self.token})[0], 403)
        self.assertEqual(self.request("GET", "/", {"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_all_writing_and_unsafe_methods_are_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "BOGUS"):
            with self.subTest(method=method):
                status, headers, body = self.request(method, "/api/report", {TOKEN_HEADER: self.token}, body=b'{}')
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET, HEAD")
                self.assertNotIn(b'{}', body)

    def test_oversized_body_rejected_for_read_and_write(self):
        for method in ("GET", "POST", "PUT"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method, "/", {"Content-Length": str(MAX_REQUEST_BODY + 1)})[0], 413)

    def test_get_body_invalid_length_and_transfer_encoding_rejected(self):
        cases = [({"Content-Length": "-1"}, 400), ({"Content-Length": "invalid"}, 400),
                 ({"Transfer-Encoding": "chunked"}, 400), ({"Content-Length": "1"}, 400)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", "/", headers)[0], expected)

    def test_no_arbitrary_files_or_absolute_proxy_urls(self):
        for path in ("/../README.md", "/%2e%2e/pyproject.toml", "/C:/Windows/win.ini", "/report.json", "/?file=secret", "/favicon.ico"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("GET", "http://evil.example/api/report", {"Host": self.authority})[0], 400)

    def test_many_query_parameters_fail_cleanly(self):
        self.assertEqual(self.request("GET", "/api/report?" + "&".join("a=1" for _ in range(11)))[0], 400)

    def test_head_uses_same_access_controls_and_has_no_body(self):
        status, headers, body = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(body, b"")
        self.assertEqual(self.request("HEAD", "/api/report")[0], 403)
        self.assertEqual(self.request("HEAD", "/api/report", {TOKEN_HEADER: self.token})[0], 200)

    def test_server_report_is_a_snapshot(self):
        self.source["coverage"]["bars"] = 999
        status, _, body = self.request("GET", "/api/report", {TOKEN_HEADER: self.token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["coverage"]["bars"], 3)

    def raw_request(self, text):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as connection:
            connection.sendall(text.encode("ascii"))
            chunks = []
            while True:
                part = connection.recv(65536)
                if not part:
                    break
                chunks.append(part)
            return b"".join(chunks)

    def test_duplicate_or_missing_host_rejected(self):
        for host_header in ("", f"Host: {self.authority}\r\nHost: {self.authority}\r\n"):
            response = self.raw_request("GET / HTTP/1.1\r\n" + host_header + "Connection: close\r\n\r\n")
            self.assertIn(b" 403 ", response.split(b"\r\n")[0])

    def test_duplicate_origin_or_body_length_rejected(self):
        for extra, status in ((f"Origin: http://{self.authority}\r\nOrigin: http://{self.authority}\r\n", b" 403 "),
                              ("Content-Length: 0\r\nContent-Length: 0\r\n", b" 400 ")):
            response = self.raw_request(f"GET / HTTP/1.1\r\nHost: {self.authority}\r\n" + extra + "Connection: close\r\n\r\n")
            self.assertIn(status, response.split(b"\r\n")[0])


if __name__ == "__main__":
    unittest.main()
