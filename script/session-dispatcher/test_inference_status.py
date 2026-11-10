"""Loopback metadata fixtures only: no real inference or agent sessions."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

from dispatch import DispatchError
from operations import inference_status


class InferenceFixture:
    def __init__(self):
        self.calls = []
        self.responses = {
            '/health': (200, b'', {}),
            '/v1/models': (200, b'{"data":[{"id":"fixture-model"}]}', {}),
            '/metrics': (200, b'''# ignored comment
vllm:num_requests_running{engine="0"} 2
vllm:num_requests_waiting{engine="0"} 1
vllm:kv_cache_usage_perc{engine="0"} 0.25
vllm:num_preemptions_total{engine="0"} 0
vllm:prompt_tokens_total{engine="0"} 1000
vllm:generation_tokens_total{engine="0"} 42
vllm:cache_config_info{private="DO-NOT-ECHO",kv_cache_memory_bytes="40000000000",kv_cache_size_tokens="1034172",kv_cache_max_concurrency="3.94"} 1
''', {}),
        }
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fixture.calls.append((self.command, self.path))
                response = fixture.responses.get(self.path, (404, b'', {}))
                if response is None:
                    self.close_connection = True
                    return
                status, body, headers = response
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class InferenceStatusTests(unittest.TestCase):
    def test_healthy_metadata_only_and_named_pressure_samples(self):
        with InferenceFixture() as fixture:
            value = inference_status(fixture.url)
        self.assertEqual(value['state'], 'responding')
        self.assertEqual(value['models'], ['fixture-model'])
        self.assertEqual(value['metrics']['num_requests_running'], [2.0])
        self.assertEqual(value['metrics']['kv_cache_usage_perc'], [0.25])
        self.assertEqual(value['cache']['kv_cache_memory_bytes'], [40000000000.0])
        self.assertEqual(fixture.calls, [('GET', '/health'), ('GET', '/v1/models'), ('GET', '/metrics')])
        self.assertNotIn('DO-NOT-ECHO', json.dumps(value))
        self.assertIn('no generation test', value['limits'])

    def test_unhealthy_response_does_not_echo_private_error_body(self):
        with InferenceFixture() as fixture:
            fixture.responses['/health'] = (503, b'PRIVATE ERROR token=secret', {})
            value = inference_status(fixture.url)
        self.assertEqual(value['state'], 'unavailable')
        self.assertEqual(value['checks']['/health']['http_status'], 503)
        self.assertNotIn('PRIVATE ERROR', json.dumps(value))
        self.assertNotIn('secret', json.dumps(value))

    def test_redirects_are_not_followed(self):
        with InferenceFixture() as fixture:
            fixture.responses['/health'] = (302, b'', {'Location': fixture.url + '/captured'})
            value = inference_status(fixture.url)
        self.assertEqual(value['state'], 'unavailable')
        self.assertEqual(value['checks']['/health']['http_status'], 302)
        self.assertNotIn(('GET', '/captured'), fixture.calls)

    def test_reset_is_reported_and_not_retried(self):
        with InferenceFixture() as fixture:
            fixture.responses['/health'] = None
            value = inference_status(fixture.url)
        self.assertEqual(value['state'], 'unavailable')
        self.assertEqual(value['checks']['/health']['error_type'], 'RemoteDisconnected')
        self.assertEqual(fixture.calls.count(('GET', '/health')), 1)

    def test_malformed_models_and_nonfinite_samples_do_not_claim_zero(self):
        with InferenceFixture() as fixture:
            fixture.responses['/v1/models'] = (200, b'{"data":"PRIVATE"}', {})
            fixture.responses['/metrics'] = (200, b'vllm:kv_cache_usage_perc NaN\n', {})
            value = inference_status(fixture.url)
        self.assertEqual(value['state'], 'unavailable')
        self.assertEqual(value['metrics'], {})
        self.assertEqual(value['cache'], {})
        self.assertNotIn('PRIVATE', json.dumps(value))

    def test_empty_model_list_is_not_ready_metadata(self):
        with InferenceFixture() as fixture:
            fixture.responses['/v1/models'] = (200, b'{"data":[]}', {})
            value = inference_status(fixture.url)
        self.assertEqual(value['state'], 'unavailable')
        self.assertEqual(value['models'], [])

    def test_large_metadata_is_bounded(self):
        with InferenceFixture() as fixture:
            fixture.responses['/v1/models'] = (200, b'x' * 65537, {})
            value = inference_status(fixture.url)
        self.assertEqual(value['state'], 'unavailable')
        self.assertEqual(value['models'], [])

    def test_nonlocal_urls_credentials_paths_and_queries_refused_without_requests(self):
        with InferenceFixture() as fixture:
            for url in ['http://example.invalid', fixture.url+'/path', fixture.url+'?private=1',
                        fixture.url+'#fragment', fixture.url.replace('http://', 'http://user:secret@')]:
                with self.assertRaises(DispatchError):
                    inference_status(url)
        self.assertEqual(fixture.calls, [])


if __name__ == '__main__':
    unittest.main()
