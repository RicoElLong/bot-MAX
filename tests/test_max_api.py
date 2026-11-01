import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import bot
from max_api import MAXClient, MAXError, Message, attachments, dispatch, run_polling
from scheduling import SAMARA
from storage import Store
from datetime import datetime


class MAXFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.temp.name) / 'bot.sqlite3')
        self.calls = []
        def handle(request):
            body = json.loads(request.content) if request.content else None
            self.calls.append((request.method, request.url.path, dict(request.url.params), body))
            return httpx.Response(200, json={'success': True, 'message': {'body': {'mid': 'out'}}})
        self.client = MAXClient('test-token', httpx.MockTransport(handle))
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={'store': self.db, 'admins': {99}}), bot=self.client)
        for target in ('scheduling.local_now', 'storage.local_now'):
            timer = patch(target, return_value=datetime(2030, 1, 1, 7, tzinfo=SAMARA))
            timer.start()
            self.addCleanup(timer.stop)

    def tearDown(self):
        self.client.close()
        self.db.db.close()
        self.temp.cleanup()

    def event(self, text='', uid=12, contact=None, chat_type='dialog'):
        raw = {'update_type': 'message_created', 'message': {
            'sender': {'user_id': uid, 'first_name': 'Клиент'},
            'recipient': {'chat_type': chat_type, 'chat_id': 50},
            'body': {'mid': 'in', 'text': text, 'attachments': []}}}
        if contact:
            raw['message']['body']['attachments'].append({'type': 'contact', 'payload': contact})
        return raw

    async def send(self, text='', **kwargs):
        await dispatch(self.event(text, **kwargs), self.client, self.context)

    async def click(self, payload, uid=12):
        raw = {'update_type': 'message_callback', 'callback': {
            'callback_id': 'cb', 'user': {'user_id': uid, 'first_name': 'Клиент'}, 'payload': payload},
            'message': {'recipient': {'chat_type': 'dialog'}, 'body': {'mid': 'keyboard', 'text': 'Выберите'}}}
        await dispatch(raw, self.client, self.context)

    async def start(self):
        with patch.object(self.client, 'upload_photo', AsyncMock(return_value={'type': 'image', 'payload': {'photos': {'1': {'token': 'photo'}}}})):
            await dispatch({'update_type': 'bot_started', 'user': {'user_id': 12, 'first_name': 'Клиент'}, 'chat_id': 50}, self.client, self.context)
        self.assertEqual(self.calls[-1][3]['attachments'][0]['type'], 'image')
        button = self.calls[-1][3]['attachments'][1]['payload']['buttons'][0][0]
        self.assertEqual(button['type'], 'request_contact')

    async def test_max_flow_contact_booking_admin_delete(self):
        await self.start()
        await self.send(contact={'vcf_info': 'BEGIN:VCARD\r\nTEL;TYPE=CELL:+79991234567\r\nEND:VCARD', 'max_info': {'user_id': 12}})
        self.assertEqual(self.db.user(12)['phone'], '+79991234567')
        await self.send('Bosch, не сливает')
        await self.click('menu:Далее')
        request_id = self.db.latest(12)['id']
        await self.click('book:day:2030-01-01')
        await self.click('book:slot:2030-01-01:10:00')
        self.assertEqual(self.db.latest(12)['booking'], '01.01.2030 10:00')
        await self.click('menu:Контакты')
        self.assertIn('Контакты', self.calls[-1][3]['text'])
        await self.send('/admin', uid=99)
        await self.click('admin:requests:0', uid=99)
        await self.click(f'admin:delete:{request_id}:0', uid=99)
        self.assertIsNotNone(self.db.request(request_id))
        await self.click(f'admin:confirm_delete:{request_id}:0', uid=12)
        self.assertIsNotNone(self.db.request(request_id))
        await self.click(f'admin:confirm_delete:{request_id}:0', uid=99)
        self.assertIsNone(self.db.request(request_id))
        self.assertIn('10:00', self.db.available_times('2030-01-01', 12))
        self.assertTrue(any(call[0] == 'PUT' and call[2].get('message_id') == 'keyboard' for call in self.calls))

    async def test_foreign_and_unverifiable_contact_rejected(self):
        await self.start()
        for info in ({'user_id': 42}, {}):
            await self.send(contact={'vcf_info': 'TEL:+79991234567', 'max_info': info})
            self.assertIsNone(self.db.user(12)['phone'])
        await self.send('+79991234567')
        self.assertEqual(self.db.user(12)['phone'], '+79991234567')

    async def test_groups_and_bot_messages_ignored(self):
        await self.send('/start', chat_type='chat')
        raw = self.event('/start')
        raw['message']['sender']['is_bot'] = True
        await dispatch(raw, self.client, self.context)
        self.assertEqual(self.db.counts(), (0, 0, 0))
        self.assertEqual(self.calls, [])

    async def test_api_authorization_and_keyboard_schema(self):
        await self.client.send_message(12, 'Меню', bot.MENU)
        method, path, params, body = self.calls[-1]
        self.assertEqual((method, path, params), ('POST', '/messages', {'user_id': '12'}))
        self.assertEqual(body['attachments'][0]['payload']['buttons'][0][0],
                         {'type': 'callback', 'text': 'Поломка', 'payload': 'menu:Поломка'})
        self.assertEqual(attachments(bot.ReplyKeyboardRemove()), [])

    async def test_api_error_does_not_expose_token(self):
        client = MAXClient('sensitive-token', httpx.MockTransport(lambda r: httpx.Response(401, json={'code': 'verify.token', 'message': 'sensitive-token'})))
        try:
            with self.assertRaises(MAXError) as caught:
                await client.request('GET', '/me')
            self.assertNotIn('sensitive-token', str(caught.exception))
            self.assertEqual(caught.exception.status, 401)
        finally:
            client.close()

    async def test_photo_upload_and_cache(self):
        self.client.request = AsyncMock(return_value={'url': 'https://upload.example.org/photo'})
        def upload(request):
            self.assertNotIn('authorization', request.headers)
            self.assertIn(b'name="data"', request.content)
            self.assertIn(b'filename="welcome.png"', request.content)
            return httpx.Response(200, json={'photos': {'1': {'token': 'photo'}}})
        upload_client = httpx.Client(transport=httpx.MockTransport(upload))
        photo = Path(bot.ROOT) / 'assets/welcome.png'
        with patch('max_api.httpx.Client', return_value=upload_client):
            with photo.open('rb') as stream:
                media = await self.client.upload_photo(stream)
            with photo.open('rb') as stream:
                cached = await self.client.upload_photo(stream)
        self.assertEqual(media, {'type': 'image', 'payload': {'photos': {'1': {'token': 'photo'}}}})
        self.assertEqual(cached, media)
        self.client.request.assert_awaited_once_with('POST', '/uploads', {'type': 'image'})

    async def test_attachment_processing_retry(self):
        self.client.request = AsyncMock(side_effect=[MAXError(400, 'attachment.not.ready'), {'message': {}}])
        with patch('max_api.asyncio.sleep', new_callable=AsyncMock) as pause:
            await self.client.send_message(12, 'Приветствие', media=[{'type': 'image', 'payload': {'token': 'photo'}}])
        self.assertEqual(self.client.request.await_count, 2)
        pause.assert_awaited_once_with(1)

    async def test_unknown_callback_and_missing_message_safe(self):
        await self.click('unknown')
        self.assertEqual(self.calls[-1][1], '/answers')
        self.assertIn('устарела', self.calls[-1][3]['notification'])
        await dispatch({'update_type': 'message_callback', 'callback': {}}, self.client, self.context)

    async def test_polling_marker_and_authentication(self):
        client = SimpleNamespace(request=AsyncMock(side_effect=[
            {'user_id': 1}, {'subscriptions': []}, {'updates': [], 'marker': 0}, MAXError(401, 'verify.token')]))
        db = Store(Path(self.temp.name) / 'poll.sqlite3')
        with self.assertRaises(MAXError):
            await run_polling(client, db, set())
        self.assertEqual(client.request.call_args_list[-1].args[2]['marker'], 0)

    async def test_webhook_conflict_does_not_delete_subscription(self):
        client = SimpleNamespace(request=AsyncMock(side_effect=[{}, {'subscriptions': [{'url': 'https://example.org'}]}]))
        db = Store(Path(self.temp.name) / 'webhook.sqlite3')
        with self.assertRaisesRegex(RuntimeError, 'webhook'):
            await run_polling(client, db, set())
        self.assertEqual(client.request.await_count, 2)
