"""Offline checks: no account login, ntfy publish or Telegram send."""
import asyncio
import atexit
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import pyotp

temporary = tempfile.TemporaryDirectory(prefix='ntfy-bridge-test-')
os.environ.update(TG_API_ID='1', TG_API_HASH='offline-test-hash',
                  NTFY_URL='https://ntfy.example.com', NTFY_TOPIC='test-notifications',
                  TOTP_SECRET=pyotp.random_base32(), DATA_DIR=temporary.name,
                  NOTIFICATION_DELAY='0')
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
import notifier

def cleanup():
    notifier.client.session.close()
    loop.close()
    temporary.cleanup()
atexit.register(cleanup)

class StorageTests(unittest.TestCase):
    def setUp(self):
        notifier.init_db()

    def test_contact_alias_stays_stable(self):
        first = notifier.get_or_create_short_id(42, 'Synthetic person')
        self.assertEqual(first, notifier.get_or_create_short_id(42, 'Renamed example'))
        self.assertEqual(notifier.get_contact_by_short_id(first)['name'], 'Renamed example')

    def test_event_deduplication(self):
        self.assertFalse(notifier.command_already_processed('offline-event'))
        notifier.mark_command_processed('offline-event')
        self.assertTrue(notifier.command_already_processed('offline-event'))

    def test_totp_rejects_malformed_codes(self):
        self.assertTrue(notifier.check_totp(notifier.totp.now()))
        for code in ['12345', 'not-a-code', '', '1234567']:
            self.assertFalse(notifier.check_totp(code))

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_message_suppresses_notification(self):
        notifier.read_messages[42] = 10
        with patch.object(notifier, 'send_new_message_notification', new_callable=AsyncMock) as send:
            await notifier.delayed_notification(42, 9, 'Synthetic person', 12345)
            send.assert_not_called()

    async def test_unread_message_notifies(self):
        notifier.read_messages.pop(43, None)
        with patch.object(notifier, 'send_new_message_notification', new_callable=AsyncMock) as send:
            await notifier.delayed_notification(43, 11, 'Synthetic person', 12345)
            send.assert_awaited_once_with('Synthetic person', 12345)

    async def test_reply_requires_valid_totp(self):
        with patch.object(notifier, 'command_reply', new_callable=AsyncMock) as reply:
            await notifier.process_command('/reply 12345 invalid example text')
            reply.assert_not_called()
            await notifier.process_command('/reply 12345 ' + notifier.totp.now() + ' example text')
            reply.assert_awaited_once_with(12345, 'example text')
