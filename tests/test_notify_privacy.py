import unittest
from unittest.mock import patch
import notify


class NotifyPrivacyTests(unittest.TestCase):
    def test_config_exception_does_not_expose_source_line(self):
        marker = 'private-config-value-for-test'
        with patch.object(notify, 'load_config', side_effect=ValueError(marker)):
            with self.assertLogs('notify', level='WARNING') as logs:
                notify.notify_failures([])
        self.assertNotIn(marker, '\n'.join(logs.output))
        self.assertIn('ValueError', '\n'.join(logs.output))

    def test_request_exception_does_not_expose_url_and_retry_is_preserved(self):
        marker = 'private-request-value-for-test'
        cfg = {'enabled': True, 'sendkey': marker}
        with patch.object(notify, 'load_config', return_value=cfg), \
                patch.object(notify, '_load_pending', return_value=[]), \
                patch.object(notify, '_save_pending') as save, \
                patch.object(notify, '_send', side_effect=OSError('https://example.invalid/' + marker)):
            with self.assertLogs('notify', level='WARNING') as logs:
                notify.notify_failures(['test failure'])
        self.assertNotIn(marker, '\n'.join(logs.output))
        self.assertNotIn('https://', '\n'.join(logs.output))
        self.assertEqual(len(save.call_args.args[0]), 1)
        self.assertGreater(save.call_args.args[0][0]['next_after'], 0)


if __name__ == '__main__':
    unittest.main()
