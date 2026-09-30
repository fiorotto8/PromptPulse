import gzip
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('monitor', ROOT / 'monitor.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

GPU_LINE = 'NVIDIA RTX PRO 6000 Blackwell Workstation Edition, GPU-abc, 580.82.07, 00000000:01:00.0, 96, 72, 48231, 97887, 61, 412.35, 600.00, 2385, 17501, 43, P2'


def fixture(ts, **values):
    metrics = {key: None for key in m.METRICS}
    metrics.update(values)
    return {'timestamp': ts, 'metrics': metrics, 'details': {}}


class ParserTests(unittest.TestCase):
    def test_nvidia_csv(self):
        v = m.parse_nvidia_csv(GPU_LINE)
        self.assertEqual(v['gpu_name'], 'NVIDIA RTX PRO 6000 Blackwell Workstation Edition')
        self.assertEqual(v['gpu_percent'], 96)
        self.assertEqual(v['gpu_memory_percent'], 72)
        self.assertEqual(v['gpu_temp_c'], 61)
        self.assertEqual(v['gpu_power_w'], 412.35)
        self.assertEqual(v['gpu_power_limit_w'], 600)
        self.assertEqual(v['gpu_freq_mhz'], 2385)
        self.assertEqual(v['gpu_memory_freq_mhz'], 17501)
        self.assertEqual(v['gpu_fan_percent'], 43)
        self.assertEqual(v['gpu_vram_used_bytes'], 48231 * 1024 * 1024)
        self.assertEqual(v['gpu_vram_total_bytes'], 97887 * 1024 * 1024)
        self.assertEqual(v['gpu_pstate'], 'P2')

    def test_nvidia_na(self):
        line = 'RTX 6000 Ada Generation, GPU-x, 570.1, 0000:01:00.0, 0, N/A, 100, 49140, 35, N/A, N/A, 210, 405, N/A, P8'
        v = m.parse_nvidia_csv(line)
        self.assertNotIn('gpu_memory_percent', v)
        self.assertNotIn('gpu_power_w', v)
        self.assertNotIn('gpu_fan_percent', v)
        self.assertEqual(v['gpu_percent'], 0)

    def test_bad_field_count(self):
        with self.assertRaises(ValueError):
            m.parse_nvidia_csv('a,b,c')

    def test_cpu_delta(self):
        self.assertEqual(m.cpu_delta(None, (100, 50, 10)), (None, None))
        self.assertEqual(m.cpu_delta((100, 50, 10), (200, 100, 20)), (50, 10))

    def test_safe_binding(self):
        for host in ('0.0.0.0', '192.168.1.1', '::', 'localhost'):
            if host == '192.168.1.1':
                self.assertEqual(m.tailnet_address(host), host)
            else:
                with self.assertRaises(ValueError):
                    m.tailnet_address(host)
        self.assertEqual(m.tailnet_address('127.0.0.1'), '127.0.0.1')

    def test_public_binding_rejected(self):
        with self.assertRaises(ValueError):
            m.tailnet_address('8.8.8.8')

    def test_tailnet_interface_binding(self):
        raw = b'\0' * 20 + socket.inet_aton('100.80.90.100') + b'\0' * 8
        with mock.patch.object(m.fcntl, 'ioctl', return_value=raw):
            self.assertEqual(m.tailnet_address('tailscale'), '100.80.90.100')
            with self.assertRaises(RuntimeError):
                m.tailnet_address('100.80.90.101')


class NvidiaReaderTests(unittest.TestCase):
    def test_missing_binary(self):
        cfg = dict(m.DEFAULTS, nvidia_smi_path='/definitely/not/here')
        values, status = m.NvidiaReader(cfg).sample()
        self.assertEqual(values, {})
        self.assertFalse(status['available'])

    def test_mock_command(self):
        cfg = dict(m.DEFAULTS, nvidia_smi_path='/usr/bin/nvidia-smi')
        proc = mock.Mock(returncode=0, stdout=GPU_LINE + '\n', stderr='')
        with mock.patch.object(m.subprocess, 'run', return_value=proc) as run:
            values, status = m.NvidiaReader(cfg).sample()
        self.assertTrue(status['available'])
        self.assertEqual(values['gpu_percent'], 96)
        args = run.call_args.args[0]
        self.assertIn('-i', args)
        self.assertEqual(args[-1], '0')

    def test_mock_error(self):
        cfg = dict(m.DEFAULTS, nvidia_smi_path='/usr/bin/nvidia-smi')
        proc = mock.Mock(returncode=1, stdout='', stderr='NVIDIA-SMI has failed')
        with mock.patch.object(m.subprocess, 'run', return_value=proc):
            values, status = m.NvidiaReader(cfg).sample()
        self.assertEqual(values, {})
        self.assertFalse(status['available'])
        self.assertIn('failed', status['message'])


class ConfigTests(unittest.TestCase):
    def test_unsafe_bind_rejected_during_config_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            for bind in ('0.0.0.0', '::', '8.8.8.8', 'localhost', '192.168.1.999', '10.example.com'):
                with self.subTest(bind=bind):
                    path.write_text(json.dumps({'bind': bind}))
                    with self.assertRaises(ValueError):
                        m.config_load(path)

    def test_private_config_does_not_require_network_interface(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            for bind in ('tailscale', '192.168.1.50', '10.0.0.10', '172.16.0.10'):
                with self.subTest(bind=bind):
                    path.write_text(json.dumps({'bind': bind}))
                    with mock.patch.object(m.fcntl, 'ioctl', side_effect=AssertionError('No interface lookup during config validation')):
                        self.assertEqual(m.config_load(path)['bind'], bind)

    def test_unknown_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'unknown': True}))
            with self.assertRaises(ValueError):
                m.config_load(path)

    def test_invalid_sampling(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'interval_seconds': 0, 'storage_path': tmp}))
            with self.assertRaises(ValueError):
                m.config_load(path)

    def test_invalid_gpu_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'gpu_index': -1, 'storage_path': tmp}))
            with self.assertRaises(ValueError):
                m.config_load(path)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(m.DEFAULTS, database=str(Path(self.tmp.name) / 'monitor.db'), storage_path=self.tmp.name)
        self.store = m.Store(self.cfg)
        self.conn = self.store.connect()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_empty(self):
        self.assertIsNone(self.store.last())
        data = self.store.history(0, 3600, 100)
        self.assertEqual(data['sample_count'], 0)

    def test_roundtrip(self):
        sample = fixture(1000, cpu_percent=42, gpu_percent=88, gpu_vram_used_bytes=1234)
        self.store.write(self.conn, sample)
        self.assertEqual(m.Store(self.cfg).last(), sample)
        data = self.store.history(995, 1010, 100)
        self.assertEqual(data['series']['gpu_percent']['mean'], [88])

    def test_min_max_preserves_spike(self):
        for i in range(20):
            self.store.write(self.conn, fixture(1000 + i * 5, gpu_percent=99 if i == 5 else 10))
        data = self.store.history(1000, 11000, 100)
        self.assertEqual(data['series']['gpu_percent']['max'][0], 99)
        self.assertEqual(data['series']['gpu_percent']['min'][0], 10)

    def test_gap_inserts_null(self):
        self.store.write(self.conn, fixture(1000, cpu_percent=10))
        self.store.write(self.conn, fixture(1200, cpu_percent=20))
        data = self.store.history(995, 1205, 100)
        self.assertEqual(data['series']['cpu_percent']['mean'], [10, None, 20])

    def test_retention(self):
        now = time.time()
        self.store.write(self.conn, fixture(now - 31 * 86400, cpu_percent=1))
        self.store.write(self.conn, fixture(now, cpu_percent=2))
        self.store.prune(self.conn, now)
        rows = self.conn.execute('SELECT cpu_percent FROM samples').fetchall()
        self.assertEqual([r[0] for r in rows], [2])

    def test_linux_sampler(self):
        reader = m.LinuxReader(self.cfg)
        first, details = reader.sample()
        self.assertIsNone(first['cpu_percent'])
        self.assertGreater(first['ram_total_bytes'], 0)
        time.sleep(.02)
        second, details = reader.sample()
        self.assertTrue(0 <= second['cpu_percent'] <= 100)
        self.assertIn('cpu_cores', details)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = dict(m.DEFAULTS, database=str(Path(self.tmp.name) / 'monitor.db'), storage_path=self.tmp.name,
                   nvidia_smi_path='/definitely/not/here')
        self.app = m.Application(cfg)
        self.server = m.MonitorServer(('127.0.0.1', 0), self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.tmp.cleanup()

    def request(self, path, headers=None, method='GET'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        base = {'Host': '127.0.0.1:%d' % self.port}
        base.update(headers or {})
        conn.request(method, path, headers=base)
        response = conn.getresponse(); body = response.read(); status = response.status; hdr = dict(response.getheaders()); conn.close()
        return status, hdr, body

    def test_pages(self):
        for path, title in (('/', b'Current readings'), ('/history', b'Performance history')):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(title, body)
            self.assertIn('Content-Security-Policy', headers)

    def test_json(self):
        status, _, body = self.request('/api/current')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['stale'])
        status, _, body = self.request('/api/history')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['sample_count'], 0)

    def test_no_file_browsing(self):
        for path in ('/monitor.py', '/config.json', '/data/monitor.db', '/static/../monitor.py'):
            self.assertEqual(self.request(path)[0], 404)

    def test_host_origin_rejected(self):
        self.assertEqual(self.request('/', {'Host': 'untrusted.example'})[0], 403)
        self.assertEqual(self.request('/api/current', {'Origin': 'https://untrusted.example'})[0], 403)

    def test_gzip(self):
        status, headers, body = self.request('/history', {'Accept-Encoding': 'gzip'})
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertIn(b'Performance history', gzip.decompress(body))


if __name__ == '__main__':
    unittest.main()
