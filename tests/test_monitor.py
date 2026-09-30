import gzip
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import sys
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
TEGRA_LINE = 'RAM 1024/8192MB CPU [4%@1200] EMC_FREQ 12%@1600 GR3D_FREQ 42%@[306,612] CPU@46.5C GPU@-256C VDD_CPU_GPU_CV 4200mW/4000mW VDD_IN 8000/7000'


def fixture(ts, **values):
    metrics = {key: None for key in m.METRICS}
    metrics.update(values)
    return {'timestamp': ts, 'metrics': metrics, 'details': {}}


class ParserTests(unittest.TestCase):
    def test_tegrastats_shared_metrics_and_invalid_sensor(self):
        v = m.parse_tegrastats(TEGRA_LINE)
        self.assertEqual(v['gpu_percent'], 42)
        self.assertEqual(v['gpu_freq_mhz'], 612)
        self.assertEqual(v['cpu_temp_c'], 46.5)
        self.assertEqual(v['emc_percent'], 12)
        self.assertEqual(v['emc_freq_mhz'], 1600)
        self.assertEqual(v['power_rails_w']['VDD_CPU_GPU_CV'], 4.2)
        self.assertEqual(v['power_rails_w']['VDD_IN'], 8)
        for key in ('gpu_temp_c', 'gpu_power_w', 'gpu_memory_percent', 'gpu_memory_freq_mhz',
                    'gpu_vram_used_bytes', 'gpu_vram_total_bytes', 'ram_used_bytes'):
            self.assertNotIn(key, v)

    def test_tegrastats_legacy_and_missing_fields(self):
        v = m.parse_tegrastats('GR3D_FREQ 0%@318 GPU@37.5C POM_5V_GPU 554/600')
        self.assertEqual(v['gpu_percent'], 0)
        self.assertEqual(v['gpu_freq_mhz'], 318)
        self.assertEqual(v['gpu_temp_c'], 37.5)
        self.assertEqual(v['gpu_power_w'], .554)
        self.assertEqual(m.parse_tegrastats('VDD_GPU 1.5W/2W')['gpu_power_w'], 1.5)
        self.assertEqual(m.parse_tegrastats('GR3D_FREQ @[306,612]')['gpu_freq_mhz'], 612)
        self.assertNotIn('gpu_percent', m.parse_tegrastats('GR3D_FREQ @[306,612]'))
        self.assertNotIn('gpu_freq_mhz', m.parse_tegrastats('GR3D_FREQ 0%'))
        self.assertNotIn('gpu_percent', m.parse_tegrastats('GR3D_FREQ 101%'))
        self.assertFalse(set(m.METRICS) & set(m.parse_tegrastats('sensor unavailable')))

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


class HardwareDiscoveryTests(unittest.TestCase):
    def test_optional_hardware_detection(self):
        cases = [
            ('nvidia,tegra234', {}, True),
            ('', {}, False),
            ('', {'vendor': '0x8086', 'class': '0x030000'}, False),
            ('', {'vendor': '0x1002', 'class': '0x030000'}, False),
            ('', {'vendor': '0x10de', 'class': '0x030200'}, True),
            ('', {'vendor': '0x10de', 'class': '0x120000'}, True),
            ('', {'vendor': '0x10de', 'class': '0x020000'}, False),
            ('', {'vendor': '0x10de', 'class': ''}, None),
        ]
        for compatible, attributes, expected in cases:
            with self.subTest(compatible=compatible, attributes=attributes):
                def read(path):
                    return compatible if str(path) == '/proc/device-tree/compatible' else attributes.get(Path(path).name, '')
                with mock.patch.object(m, 'read_text', side_effect=read), \
                        mock.patch.object(Path, 'iterdir', return_value=[Path('/device')] if attributes else []):
                    self.assertIs(m.nvidia_gpu_present(), expected)
        with mock.patch.object(m, 'read_text', return_value=''), \
                mock.patch.object(Path, 'iterdir', side_effect=PermissionError):
            self.assertIsNone(m.nvidia_gpu_present())


class NvidiaReaderTests(unittest.TestCase):
    def test_missing_auto_tool_is_absent(self):
        with mock.patch.object(m.shutil, 'which', return_value=None), \
                mock.patch.object(Path, 'is_file', return_value=False):
            reader = m.NvidiaReader(dict(m.DEFAULTS))
        values, status = reader.sample()
        self.assertEqual(values, {})
        self.assertEqual(status, m.telemetry_status('absent'))

    def test_missing_binary(self):
        cfg = dict(m.DEFAULTS, nvidia_smi_path='/definitely/not/here')
        values, status = m.NvidiaReader(cfg).sample()
        self.assertEqual(values, {})
        self.assertFalse(status['available'])
        self.assertEqual(status['state'], 'error')  # An explicit invalid path needs attention.

    def test_installed_tool_failure_without_hardware_is_quiet(self):
        failure = mock.Mock(returncode=1, stdout='', stderr='No devices were found')
        with mock.patch.object(m.shutil, 'which', return_value='/usr/bin/nvidia-smi'):
            reader = m.NvidiaReader(dict(m.DEFAULTS))
        for present, state in ((False, 'absent'), (True, 'error'), (None, 'error')):
            with self.subTest(hardware=present), \
                    mock.patch.object(m, 'nvidia_gpu_present', return_value=present), \
                    mock.patch.object(m.subprocess, 'run', return_value=failure):
                values, status = reader.sample()
                self.assertEqual(status['state'], state)
                self.assertEqual(bool(status['message']), state == 'error')

    def test_loss_of_working_gpu_still_warns(self):
        with mock.patch.object(m.shutil, 'which', return_value='/usr/bin/nvidia-smi'):
            reader = m.NvidiaReader(dict(m.DEFAULTS))
        success = mock.Mock(returncode=0, stdout=GPU_LINE, stderr='')
        failure = mock.Mock(returncode=1, stdout='', stderr='GPU lost')
        with mock.patch.object(m.subprocess, 'run', side_effect=[success, failure]), \
                mock.patch.object(m, 'nvidia_gpu_present', return_value=False):
            self.assertEqual(reader.sample()[1]['state'], 'ready')
            self.assertEqual(reader.sample()[1]['state'], 'error')

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


class TegraReaderTests(unittest.TestCase):
    def test_startup_is_not_an_error(self):
        reader = m.TegraReader(dict(m.DEFAULTS, tegrastats_path='/unused'), threading.Event())
        reader.stop.set()
        self.assertEqual(reader.sample()[1]['state'], 'starting')

    def test_unused_auto_tool_is_not_started_without_hardware(self):
        with mock.patch.object(m.shutil, 'which', return_value='/unused/tegrastats'), \
                mock.patch.object(m, 'nvidia_gpu_present', return_value=False):
            reader = m.TegraReader(dict(m.DEFAULTS), threading.Event())
        self.assertEqual(reader.sample()[1]['state'], 'absent')
        self.assertIsNone(reader.thread.ident)

    def test_missing_tool_and_nonzero_gpu(self):
        for path, index in ((None, 0), ('/unused/tegrastats', 1)):
            cfg = dict(m.DEFAULTS, gpu_index=index)
            with mock.patch.object(m.shutil, 'which', return_value=path):
                reader = m.TegraReader(cfg, threading.Event())
            values, status = reader.sample()
            self.assertFalse(status['available'])
            self.assertEqual(values, {})
            self.assertIsNone(reader.thread.ident)

    def test_stale_snapshot_and_discovered_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / 'arbitrary-bus-address'
            (device / 'device').mkdir(parents=True)
            (device / 'device/driver').symlink_to(Path(tmp) / 'gk20a')
            (device / 'cur_freq').write_text('306000000')
            reader = m.TegraReader(dict(m.DEFAULTS, tegrastats_path='/unused'), threading.Event())
            reader.stop.set()  # Inspect snapshots without launching a tool.
            reader.last, reader.updated, reader.error = {'gpu_percent': 0}, time.monotonic(), ''
            with mock.patch.object(m.glob, 'glob', return_value=[str(device)]):
                values, status = reader.sample()
                self.assertTrue(status['available'])
                self.assertEqual(values['gpu_freq_mhz'], 306)
                self.assertEqual(values['gpu_percent'], 0)
                reader.last['gpu_freq_mhz'] = 612
                self.assertEqual(reader.sample()[0]['gpu_freq_mhz'], 612)
                reader.updated -= 60
                values, status = reader.sample()
                self.assertEqual(values, {})
                self.assertFalse(status['available'])
                self.assertIn('stale', status['message'])
                self.assertEqual(status['state'], 'error')

    def test_stream_and_owned_child_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / 'fake-tegrastats'
            tool.write_text('#!' + sys.executable + '\nimport time\nprint(' + repr(TEGRA_LINE) + ', flush=True)\ntime.sleep(60)\n')
            tool.chmod(0o700)
            reader = m.TegraReader(dict(m.DEFAULTS, tegrastats_path=str(tool)), threading.Event())
            popen = m.subprocess.Popen
            children = []
            def launch(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                return child
            with mock.patch.object(m.subprocess, 'Popen', side_effect=launch) as start:
                try:
                    reader.sample()
                    deadline = time.monotonic() + 3
                    while reader.updated is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    values, status = reader.sample()
                    self.assertTrue(status['available'], status)
                    self.assertEqual(values['gpu_percent'], 42)
                    reader.sample()
                    self.assertEqual(start.call_count, 1)
                    self.assertEqual(start.call_args.args[0], [str(tool), '--interval', '5000'])
                finally:
                    reader.close()
            self.assertFalse(reader.thread.is_alive())
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].returncode)

    def test_inaccessible_or_ambiguous_clock_keeps_tool_readings(self):
        reader = m.TegraReader(dict(m.DEFAULTS, tegrastats_path='/unused'), threading.Event())
        reader.stop.set()
        reader.last, reader.updated = {'gpu_percent': 42}, time.monotonic()
        for error in (PermissionError('sensor inaccessible'), RuntimeError('symlink loop')):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(m.glob, 'glob', return_value=['/sys/class/devfreq/example']), \
                    mock.patch.object(Path, 'resolve', side_effect=error):
                values, status = reader.sample()
                self.assertTrue(status['available'])
                self.assertEqual(values['gpu_percent'], 42)
                self.assertNotIn('gpu_freq_mhz', values)
        with mock.patch.object(m.glob, 'glob', return_value=['/gpu-a', '/gpu-b']), \
                mock.patch.object(Path, 'resolve', return_value=Path('/drivers/gk20a')):
            self.assertNotIn('gpu_freq_mhz', reader.sample()[0])

    def test_stalled_tool_invalidates_sample_and_is_reaped(self):
        reader = m.TegraReader(dict(m.DEFAULTS, tegrastats_path='/unused'), threading.Event())
        reader.last, reader.updated = {'gpu_percent': 42}, 0
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.side_effect = [m.subprocess.TimeoutExpired('tegrastats', 3), 0]
        with mock.patch.object(m.subprocess, 'Popen', return_value=child), \
                mock.patch.object(m.time, 'monotonic', side_effect=[0, 31]), \
                mock.patch.object(reader.stop, 'wait', side_effect=lambda _: reader.stop.set()), \
                self.assertLogs(m.LOG, level='WARNING'):
            reader.run()
        self.assertEqual(reader.last, {})
        self.assertIsNone(reader.updated)
        self.assertIn('stopped producing', reader.error)
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_count, 2)

    def test_permission_failure_does_not_escape_reader(self):
        reader = m.TegraReader(dict(m.DEFAULTS, tegrastats_path='/not-executable'), threading.Event())
        with mock.patch.object(m.subprocess, 'Popen', side_effect=PermissionError('permission denied')):
            try:
                with self.assertLogs(m.LOG, level='WARNING'):
                    reader.sample()
                    deadline = time.monotonic() + 3
                    while 'failed' not in reader.error and time.monotonic() < deadline:
                        time.sleep(.01)
                values, status = reader.sample()
                self.assertEqual(values, {})
                self.assertFalse(status['available'])
                self.assertIn('permission denied', status['message'])
            finally:
                reader.close()
        self.assertFalse(reader.thread.is_alive())


class CollectorTests(unittest.TestCase):
    def test_host_only_without_gpu_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(m.DEFAULTS, database=str(Path(tmp) / 'monitor.db'), storage_path=tmp)
            with mock.patch.object(m.shutil, 'which', return_value=None), \
                    mock.patch.object(Path, 'is_file', return_value=False):
                app = m.Application(cfg)
            sample = app.collect()
            self.assertGreater(sample['metrics']['ram_total_bytes'], 0)
            self.assertGreater(sample['metrics']['disk_total_bytes'], 0)
            self.assertIsNone(sample['metrics']['gpu_percent'])
            self.assertIsNone(sample['metrics']['gpu_vram_total_bytes'])
            self.assertFalse(sample['details']['gpu_telemetry']['available'])
            self.assertEqual(sample['details']['gpu_telemetry']['state'], 'absent')
            self.assertEqual(sample['details']['gpu_telemetry']['message'], '')
            self.assertIsNone(app.tegra.thread.ident)

    def test_optional_status_only_reports_real_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = m.Application(dict(m.DEFAULTS, database=str(Path(tmp) / 'monitor.db'), storage_path=tmp))
            cases = [
                ('absent', 'absent', 'absent', ''),
                ('absent', 'starting', 'starting', ''),
                ('error', 'absent', 'error', 'NVIDIA failure'),
                ('absent', 'error', 'error', 'Tegra failure'),
                ('error', 'error', 'error', 'NVIDIA failure; Tegra failure'),
                ('error', 'ready', 'ready', ''),
            ]
            for nvidia, tegra, state, message in cases:
                with self.subTest(nvidia=nvidia, tegra=tegra), \
                        mock.patch.object(app.nvidia, 'sample', return_value=(
                            {}, m.telemetry_status(nvidia, 'NVIDIA failure' if nvidia == 'error' else ''))), \
                        mock.patch.object(app.tegra, 'sample', return_value=(
                            {}, m.telemetry_status(tegra, 'Tegra failure' if tegra == 'error' else ''))):
                    status = app.collect()['details']['gpu_telemetry']
                    self.assertEqual(status['state'], state)
                    self.assertEqual(status['message'], message)

    def test_gpu_fallback_preserves_host_readings_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(m.DEFAULTS, database=str(Path(tmp) / 'monitor.db'), storage_path=tmp)
            app = m.Application(cfg)
            host = {'ram_used_bytes': 123, 'gpu_vram_used_bytes': None, 'gpu_percent': None}
            failed = m.telemetry_status('error', 'tool failed')
            ready = m.telemetry_status('ready', 'reading')
            with mock.patch.object(app.linux, 'sample', return_value=(host, {})), \
                    mock.patch.object(app.nvidia, 'sample', return_value=({}, failed)), \
                    mock.patch.object(app.tegra, 'sample', return_value=(m.parse_tegrastats(TEGRA_LINE), ready)) as tegra:
                sample = app.collect()
                self.assertEqual(sample['metrics']['ram_used_bytes'], 123)
                self.assertIsNone(sample['metrics']['gpu_vram_used_bytes'])
                self.assertEqual(sample['metrics']['gpu_percent'], 42)
                self.assertEqual(sample['details']['gpu_telemetry']['source'], 'tegrastats')
                self.assertFalse(sample['details']['nvidia_smi']['available'])
                self.assertTrue(sample['details']['gpu_telemetry']['available'])
                tegra.return_value = ({}, failed)
                app.linux.sample.return_value = ({'ram_used_bytes': 124, 'gpu_percent': None}, {})
                sample = app.collect()
                self.assertEqual(sample['metrics']['ram_used_bytes'], 124)
                self.assertIsNone(sample['metrics']['gpu_percent'])
                self.assertFalse(sample['details']['gpu_telemetry']['available'])
            with mock.patch.object(app.nvidia, 'sample', return_value=(m.parse_nvidia_csv(GPU_LINE), ready)), \
                    mock.patch.object(app.tegra, 'sample') as tegra:
                sample = app.collect()
                self.assertEqual(sample['metrics']['gpu_percent'], 96)
                self.assertEqual(sample['details']['gpu_telemetry']['source'], 'nvidia-smi')
                tegra.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_tegrastats_path_and_existing_config_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('{}')
            self.assertEqual(m.config_load(path)['tegrastats_path'], 'auto')
            path.write_text(json.dumps({'tegrastats_path': '/custom/tegrastats'}))
            self.assertEqual(m.config_load(path)['tegrastats_path'], '/custom/tegrastats')
            for invalid in ('', None, False):
                path.write_text(json.dumps({'tegrastats_path': invalid}))
                with self.assertRaises(ValueError):
                    m.config_load(path)

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
