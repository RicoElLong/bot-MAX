"""MAX Bot API transport and message adapters for the service's conversation handlers.

Wire formats follow max-messenger/max-bot-api-client-ts (MIT), commit a5c40a0.
No Telegram SDK or Telegram network calls are used.
"""
import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx


class MAXError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(f'MAX API: HTTP {status}, {code}')


@dataclass
class InlineKeyboardButton:
    text: str
    callback_data: str = None
    url: str = None


@dataclass
class KeyboardButton:
    text: str
    request_contact: bool = False


class InlineKeyboardMarkup:
    def __init__(self, rows):
        self.inline_keyboard = rows


class ReplyKeyboardMarkup:
    def __init__(self, rows, **kwargs):
        self.keyboard = [[KeyboardButton(b) if isinstance(b, str) else b for b in row] for row in rows]


class ReplyKeyboardRemove:
    pass


def attachments(markup):
    if markup is None or isinstance(markup, ReplyKeyboardRemove):
        return []
    rows = getattr(markup, 'inline_keyboard', None)
    if rows is None:
        rows = markup.keyboard
    buttons = []
    for row in rows:
        result = []
        for button in row:
            item = {'text': button.text}
            if isinstance(button, InlineKeyboardButton):
                if button.url:
                    item.update(type='link', url=button.url)
                else:
                    item.update(type='callback', payload=button.callback_data)
            elif button.request_contact:
                item['type'] = 'request_contact'
            else:
                item.update(type='callback', payload='menu:' + button.text)
            result.append(item)
        if result:
            buttons.append(result)
    return [{'type': 'inline_keyboard', 'payload': {'buttons': buttons}}] if buttons else []


class MAXClient:
    def __init__(self, token, transport=None):
        base = os.getenv('MAX_API_URL', 'https://platform-api2.max.ru').rstrip('/')
        if urlparse(base).scheme != 'https':
            raise ValueError('MAX_API_URL должен использовать HTTPS.')
        self.http = httpx.Client(base_url=base, headers={'Authorization': token},
                                 timeout=60, transport=transport)
        self.photo_cache = {}

    def close(self):
        self.http.close()

    def _request(self, method, path, params, body):
        try:
            response = self.http.request(method, path, params=params, json=body)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise MAXError(0, 'transport.error') from None
        if response.is_error or data.get('success') is False:
            # Do not include remote error text: it may echo a token or personal data.
            code = re.sub(r'[^a-zA-Z0-9_.-]', '', str(data.get('code', 'request.failed')))[:80]
            raise MAXError(response.status_code, code)
        return data

    async def request(self, method, path, params=None, body=None):
        return await asyncio.to_thread(self._request, method, path, params, body)

    async def send_message(self, uid, text, reply_markup=None, media=None):
        body = {'text': text, 'attachments': (media or []) + attachments(reply_markup)}
        for attempt in range(4):
            try:
                return await self.request('POST', '/messages', {'user_id': uid}, body)
            except MAXError as error:
                if error.code != 'attachment.not.ready' or attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)

    async def upload_photo(self, stream):
        path = Path(stream.name)
        key = (str(path.resolve()), path.stat().st_mtime_ns)
        if key not in self.photo_cache:
            info = await self.request('POST', '/uploads', {'type': 'image'})
            url = info['url']
            if urlparse(url).scheme != 'https':
                raise MAXError(0, 'upload.insecure')
            content = stream.read()
            def upload():
                # Separate client: never forward the bot Authorization header to an upload host.
                try:
                    with httpx.Client(timeout=60) as http:
                        response = http.post(url, files={'data': (path.name, content, 'image/png')})
                        response.raise_for_status()
                        return response.json()
                except (httpx.HTTPError, ValueError):
                    raise MAXError(0, 'upload.failed') from None
            self.photo_cache[key] = await asyncio.to_thread(upload)
        return {'type': 'image', 'payload': self.photo_cache[key]}


def user_from(raw):
    return SimpleNamespace(id=raw['user_id'], username=raw.get('username'),
                           full_name=' '.join(filter(None, [raw.get('first_name'), raw.get('last_name')])) or raw.get('name') or str(raw['user_id']))


class Message:
    def __init__(self, client, uid, raw=None, text=None):
        self.client, self.uid = client, uid
        self.raw = raw or {}
        body = self.raw.get('body') or {}
        self.text = text if text is not None else body.get('text')
        self.contact = None
        for item in body.get('attachments') or []:
            if item.get('type') == 'contact':
                payload = item.get('payload') or {}
                vcf = re.sub(r'\r?\n[ \t]', '', payload.get('vcf_info', ''))
                phone = re.search(r'^TEL(?:;[^:\r\n]*)?:(?:tel:)?([^\r\n]+)', vcf, re.M | re.I)
                info = payload.get('max_info') or {}
                self.contact = SimpleNamespace(user_id=info.get('user_id'), phone_number=phone.group(1) if phone else '')

    async def reply_text(self, text, reply_markup=None):
        return await self.client.send_message(self.uid, text, reply_markup)

    async def reply_photo(self, stream, caption, reply_markup=None):
        media = await self.client.upload_photo(stream)
        return await self.client.send_message(self.uid, caption, reply_markup, [media])


class Callback:
    def __init__(self, client, raw):
        self.client = client
        callback = raw['callback']
        self.from_user = user_from(callback['user'])
        self.data = callback.get('payload') or ''
        self.id = callback['callback_id']
        self.message = Message(client, self.from_user.id, raw.get('message'))

    async def answer(self, text=None, show_alert=False):
        return await self.client.request('POST', '/answers', {'callback_id': self.id}, {'notification': text})

    async def edit_message_text(self, text, reply_markup=None):
        return await self.client.request('PUT', '/messages', {'message_id': self.message.raw['body']['mid']},
                                         {'text': text, 'attachments': attachments(reply_markup)})

    async def edit_message_reply_markup(self, reply_markup=None):
        # Keep original text when removing the obsolete booking keyboard.
        return await self.edit_message_text(self.message.text, reply_markup)


class Update:
    pass


async def dispatch(raw, client, context):
    import bot
    kind = raw.get('update_type')
    update = Update()
    update.callback_query = None
    if kind == 'message_callback':
        msg_raw = raw.get('message')
        if not msg_raw or msg_raw.get('recipient', {}).get('chat_type') != 'dialog':
            return
        query = Callback(client, raw)
        update.callback_query = query
        update.effective_user = query.from_user
        update.message = query.message
    elif kind == 'bot_started':
        if raw['user'].get('is_bot'):
            return
        update.effective_user = user_from(raw['user'])
        update.message = Message(client, update.effective_user.id, text='/start')
    elif kind == 'message_created':
        msg_raw = raw['message']
        sender = msg_raw.get('sender') or {}
        if msg_raw.get('recipient', {}).get('chat_type') != 'dialog' or sender.get('is_bot') or not sender:
            return
        update.effective_user = user_from(sender)
        update.message = Message(client, update.effective_user.id, msg_raw)
    else:
        return
    update.effective_message = update.message
    update.effective_chat = SimpleNamespace(type='private')
    if update.callback_query:
        query = update.callback_query
        data = query.data
        if data.startswith('menu:'):
            await query.answer()
            update.message.text = data[5:]
            update.message.contact = None
            await bot.message(update, context)
        elif re.fullmatch(r'book:(?:dates|day:\d{4}-\d{2}-\d{2}|slot:\d{4}-\d{2}-\d{2}:\d{2}:00)', data):
            await bot.booking_callback(update, context)
        elif re.fullmatch(r'admin:(delete|confirm_delete):\d+:\d+', data):
            await bot.admin_delete(update, context)
        elif re.fullmatch(r'admin:(visits|requests):\d+', data):
            await bot.admin_page(update, context)
        else:
            await query.answer('Эта кнопка устарела. Начните с /start.')
    else:
        command = (update.message.text or '').strip().split(maxsplit=1)
        handler = {'/start': bot.start, '/myid': bot.myid, '/admin': bot.admin, '/cancel': bot.cancel}.get(command[0] if command else '')
        await (handler or bot.message)(update, context)


async def run_polling(client, store, admins):
    context = SimpleNamespace(application=SimpleNamespace(bot_data={'store': store, 'admins': admins}), bot=client)
    marker = None
    try:
        await client.request('GET', '/me')
        subscriptions = await client.request('GET', '/subscriptions')
        if subscriptions.get('subscriptions'):
            raise RuntimeError('У бота есть webhook-подписки. Удалите их перед запуском long polling.')
        logging.warning('MAX: подключение проверено, бот запущен.')
        while True:
            params = {'timeout': 30, 'types': 'bot_started,message_created,message_callback'}
            if marker is not None:
                params['marker'] = marker
            try:
                batch = await client.request('GET', '/updates', params)
            except MAXError as error:
                if error.status in (401, 403):
                    raise
                logging.warning('MAX: получение событий не удалось (%s); повтор через 5 секунд.', error.code)
                await asyncio.sleep(5)
                continue
            for raw in batch.get('updates', []):
                try:
                    await dispatch(raw, client, context)
                except Exception as error:
                    logging.error('MAX: ошибка обработки события (%s).', type(error).__name__)
            marker = batch.get('marker', marker)
    finally:
        store.db.close()
