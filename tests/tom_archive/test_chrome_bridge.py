import json
import queue
import threading
import unittest
import urllib.error
import urllib.request
from mtnews_dl.sources.tom_archive.chrome_bridge import BRIDGE_VERSION, ChromeBridge

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = ChromeBridge(threading.Event(), token='test-secret', port=0)
        self.url = 'http://127.0.0.1:' + str(self.bridge.server.server_port)
    def tearDown(self):
        self.bridge.close()
    def request(self, path, data=None, token='test-secret', version=BRIDGE_VERSION):
        headers = {'X-Bridge-Token': token}
        if version:
            headers['X-Bridge-Version'] = version
        request = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)
    def test_unauthenticated_client_cannot_take_command(self):
        self.bridge.commands.put({'id': 'secret-command'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/command', token='wrong')
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.request('/command')['id'], 'secret-command')
        self.assertIsNone(self.request('/command'))
    def test_connection_tab_from_older_version_is_told_to_reload(self):
        told = []
        self.bridge.emit_stale = lambda: told.append(1)
        self.bridge.commands.put({'id': 'kept', 'action': 'snapshot'})
        for _ in range(2):
            self.assertEqual(self.request('/command', version=None)['action'], 'reload')
        self.assertEqual(told, [1])
        self.assertFalse(self.bridge.connected.is_set())
        self.assertEqual(self.request('/command')['id'], 'kept')   # nothing lost meanwhile
    def test_real_http_command_result_round_trip(self):
        results = queue.Queue()
        worker = threading.Thread(target=lambda: results.put(self.bridge.call('snapshot')))
        worker.start()
        command = self.request('/command')
        self.assertEqual(command['action'], 'snapshot')
        self.request('/result', {'id': 'stale-result', 'value': 'ignored'})
        self.request('/result', {'id': command['id'], 'value': {'title': 'Archive'}})
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results.get_nowait(), {'title': 'Archive'})
    def test_server_error_stops_without_resubmitting(self):
        errors = queue.Queue()
        def run():
            try:
                self.bridge.call('download', url='https://timesofmalta.com/download/20261031-001')
            except RuntimeError as error:
                errors.put(str(error))
        worker = threading.Thread(target=run)
        worker.start()
        command = self.request('/command')
        self.request('/result', {'id': command['id'], 'error': 'Download interrupted'})
        worker.join(3)
        self.assertEqual(errors.get_nowait(), 'Download interrupted')
        self.assertIsNone(self.request('/command'))

if __name__ == '__main__': unittest.main()
