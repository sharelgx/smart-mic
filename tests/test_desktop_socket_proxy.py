from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from desktop_socket_proxy import server_args


class DesktopProxyTests(unittest.TestCase):
    def test_desktop_settings_and_flags_preserved(self):
        args = ['-c','features.code_mode_host=true','app-server','--analytics-default-enabled',
                '-c','plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true',
                '-c','approval_policy="on-request"']
        self.assertEqual(server_args(args, Path('/tmp/mb.sock')), args+['--listen','unix:///tmp/mb.sock'])

    def test_non_desktop_commands_delegate(self):
        for args in [['--version'],['app-server','proxy'],['app-server','--help'],
                     ['app-server','--analytics-default-enabled','--print-startup-config'],
                     ['app-server','daemon','version']]:
            self.assertIsNone(server_args(args,Path('/tmp/mb.sock')))

    def test_explicit_transport_never_overwritten(self):
        for tail in [['--listen','stdio://'],['--listen=off'],['--stdio']]:
            with self.assertRaises(ValueError):
                server_args(['app-server','--analytics-default-enabled',*tail], Path('/tmp/mb.sock'))


if __name__ == '__main__':
    unittest.main()
