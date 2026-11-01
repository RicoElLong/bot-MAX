import logging
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from max_api import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                     ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, MAXError)
from storage import Store, SlotUnavailable, canonical_phone
from scheduling import SAMARA, booking_days

ROOT = Path(__file__).resolve().parent
BUTTON_DEFAULTS = {
    'PHONE': 'Отправить телефон',
    'NEXT': 'Далее',
    'BREAKDOWN': 'Поломка',
    'BOOKING': 'Запись',
    'CONTACTS': 'Контакты',
    'REVIEWS': 'Отзывы',
    'OPEN_REVIEWS': 'Открыть отзывы',
    'ADMIN_VISITS': 'Кто и когда заходил',
    'ADMIN_REQUESTS': 'Заявки и телефоны',
    'DELETE_REQUEST': 'Удалить заявку',
    'CONFIRM_DELETE': 'Да, удалить',
    'CANCEL_DELETE': 'Отмена',
    'BACK': '← Назад',
    'FORWARD': 'Далее →',
    'CHANGE_DATE': 'Выбрать другую дату',
}


def configure_buttons():
    global BUTTONS, MENU, NEXT, PHONE, PROMPT
    BUTTONS = {key: os.getenv('BUTTON_' + key, default).strip() or default
               for key, default in BUTTON_DEFAULTS.items()}
    if any(len(value) > 64 for value in BUTTONS.values()):
        raise ValueError('Названия кнопок должны содержать не более 64 символов.')
    menu_keys = ['BREAKDOWN', 'BOOKING', 'CONTACTS', 'REVIEWS']
    if len({BUTTONS[key] for key in menu_keys}) != len(menu_keys):
        raise ValueError('Названия четырёх кнопок меню должны различаться.')
    MENU = ReplyKeyboardMarkup([
        [BUTTONS['BREAKDOWN'], BUTTONS['BOOKING']],
        [BUTTONS['CONTACTS'], BUTTONS['REVIEWS']],
    ], resize_keyboard=True)
    NEXT = ReplyKeyboardMarkup([[BUTTONS['NEXT']]], resize_keyboard=True)
    PHONE = ReplyKeyboardMarkup([[KeyboardButton(BUTTONS['PHONE'], request_contact=True)]], resize_keyboard=True)
    PROMPT = ('Укажите по возможности, какая проблема вас беспокоит и модель стиральной машины.\n\n'
              f'Напишите одним сообщением. Затем нажмите «{BUTTONS["NEXT"]}». '
              f'Можно пропустить описание кнопкой «{BUTTONS["NEXT"]}».')


configure_buttons()


def normalize_phone(text):
    if not re.fullmatch(r'\+?[\d\s()\-]+', text.strip()):
        return None
    digits = re.sub(r'\D', '', text)
    return canonical_phone(text) if 10 <= len(digits) <= 15 else None


def store(context):
    return context.application.bot_data['store']


def is_admin(uid, context):
    return uid in context.application.bot_data['admins']


async def notify(context, text):
    for uid in context.application.bot_data['admins']:
        try:
            await context.bot.send_message(uid, text)
        except MAXError:
            # Заявка уже сохранена в SQLite и доступна через /admin.
            logging.getLogger(__name__).warning('Не удалось доставить уведомление администратору %s', uid)


async def start(update: Update, context):
    db = store(context)
    db.visit(update.effective_user)
    user = db.user(update.effective_user.id)
    await notify(context, f'Вход в бот: {user["name"]} (ID {user["id"]})\nВремя: {display_time(user["last_seen"])}')
    db.stage(user['id'], 'phone')
    db.clear_draft(user['id'])
    caption = 'Добро пожаловать, укажите пожалуйста свой телефон, это необходимо для обратной связи с мастером.\n\nНажмите кнопку или введите номер текстом. Отправляя данные, вы передаёте их администратору сервиса для связи по ремонту.\n/myid — ваш MAX ID'
    photo = Path(os.getenv('WELCOME_PHOTO', 'assets/welcome.png'))
    if not photo.is_absolute():
        photo = ROOT / photo
    with photo.open('rb') as stream:
        await update.message.reply_photo(stream, caption=caption, reply_markup=PHONE)


async def myid(update, context):
    await update.message.reply_text(f'Ваш MAX ID: {update.effective_user.id}')


def display_time(value):
    return datetime.fromisoformat(value).astimezone(SAMARA).strftime('%d.%m.%Y %H:%M Самара, UTC+4')


async def admin(update, context):
    if not is_admin(update.effective_user.id, context):
        await update.message.reply_text('Доступ только для администратора.')
        return
    users, visits, requests = store(context).counts()
    await update.message.reply_text(f'Админ-панель\nКлиентов: {users}\nВходов через /start: {visits}\nЗаявок: {requests}', reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton(BUTTONS['ADMIN_VISITS'], callback_data='admin:visits:0')],
        [InlineKeyboardButton(BUTTONS['ADMIN_REQUESTS'], callback_data='admin:requests:0')]]))


async def admin_page(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id, context):
        await query.answer('Доступ запрещён', show_alert=True)
        return
    await query.answer()
    _, kind, raw_page = query.data.split(':')
    page = int(raw_page)
    limit = 1 if kind == 'requests' else 5
    rows = store(context).page(kind, page, limit)
    entries = []
    for row in rows[:limit]:
        text = f'{display_time(row["created"])}\n{row["name"]} (ID {row["user_id"] if kind == "requests" else row["id"]})'
        if kind == 'requests':
            text += f'\nЗаявка №{row["id"]}\nТелефон: {row["phone"]}\nПроблема: {row["problem"]}\nЗапись: {(row["booking"] or "не указана")}'
        entries.append(text)
    buttons = []
    if page:
        buttons.append(InlineKeyboardButton(BUTTONS['BACK'], callback_data=f'admin:{kind}:{page-1}'))
    if len(rows) > limit:
        buttons.append(InlineKeyboardButton(BUTTONS['FORWARD'], callback_data=f'admin:{kind}:{page+1}'))
    keyboard = [buttons] if buttons else []
    if kind == 'requests' and rows:
        keyboard.insert(0, [InlineKeyboardButton(BUTTONS['DELETE_REQUEST'],
                           callback_data=f'admin:delete:{rows[0]["id"]}:{page}')])
    await query.edit_message_text('\n\n'.join(entries) or 'Пока нет записей.',
                                  reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)


async def admin_delete(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id, context):
        await query.answer('Доступ запрещён', show_alert=True)
        return
    await query.answer()
    _, action, raw_id, raw_page = query.data.split(':')
    request_id, page = int(raw_id), int(raw_page)
    db = store(context)
    back = InlineKeyboardMarkup([[InlineKeyboardButton(BUTTONS['ADMIN_REQUESTS'], callback_data='admin:requests:0')]])
    row = db.request(request_id)
    if row is None:
        await query.edit_message_text('Эта заявка уже удалена.', reply_markup=back)
        return
    if action == 'delete':
        await query.edit_message_text(
            f'Удалить заявку №{request_id}?\nТелефон: {row["phone"]}\nЗапись: {row["booking"] or "не указана"}\nЗанятое время будет освобождено.',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(BUTTONS['CONFIRM_DELETE'], callback_data=f'admin:confirm_delete:{request_id}:{page}')],
                [InlineKeyboardButton(BUTTONS['CANCEL_DELETE'], callback_data=f'admin:requests:{page}')],
            ]))
        return
    deleted = db.delete_request(request_id)
    text = f'Заявка №{request_id} удалена. Занятое время освобождено.' if deleted else 'Эта заявка уже удалена.'
    await query.edit_message_text(text, reply_markup=back)


async def message(update, context):
    if update.effective_chat.type != 'private':
        return
    msg, uid = update.message, update.effective_user.id
    db = store(context)
    user = db.user(uid)
    if not user:
        await msg.reply_text('Для начала нажмите /start.')
        return
    text = (msg.text or '').strip()
    stage = user['stage']
    if stage == 'phone':
        if msg.contact and msg.contact.user_id != uid:
            await msg.reply_text('Пожалуйста, отправьте свой контакт кнопкой ниже.', reply_markup=PHONE)
            return
        phone = normalize_phone(msg.contact.phone_number if msg.contact else text)
        if not phone:
            await msg.reply_text('Введите телефон из 10–15 цифр, например +79991234567, или нажмите кнопку.', reply_markup=PHONE)
            return
        db.phone(uid, phone)
        await msg.reply_text(PROMPT, reply_markup=NEXT)
    elif stage == 'problem':
        if text == BUTTONS['NEXT']:
            existing = db.latest(uid)
            request_id = db.submit(uid)
            row = db.latest(uid)
            title = 'Заявка обновлена' if existing else 'Новая заявка'
            await notify(context, f'{title} №{request_id}\n{user["name"]} (ID {uid})\nТелефон: {row["phone"]}\nПроблема и модель: {row["problem"]}')
            result = 'Заявка обновлена' if existing else 'Заявка сохранена'
            await msg.reply_text(f'Спасибо! {result}. Теперь выберите дату и время визита мастера.', reply_markup=ReplyKeyboardRemove())
            await show_dates(msg, uid, context)
        elif text:
            if len(text) > 2000:
                await msg.reply_text('Сократите описание до 2000 символов.')
                return
            db.draft(uid, text)
            await msg.reply_text(f'Описание принято. Нажмите «{BUTTONS["NEXT"]}», чтобы отправить заявку.', reply_markup=NEXT)
        else:
            await msg.reply_text(f'Опишите проблему текстом или нажмите «{BUTTONS["NEXT"]}».', reply_markup=NEXT)
    elif text == BUTTONS['CONTACTS']:
        await msg.reply_text(os.getenv('CONTACTS_TEXT', 'Контакты сервиса пока не заполнены.'), reply_markup=MENU)
    elif text == BUTTONS['REVIEWS']:
        url = os.getenv('REVIEWS_URL', '')
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(BUTTONS['OPEN_REVIEWS'], url=url)]]) if url.startswith('https://') else MENU
        await msg.reply_text(os.getenv('REVIEWS_TEXT', 'Отзывы пока не добавлены.'), reply_markup=markup)
    elif text == BUTTONS['BREAKDOWN']:
        db.stage(uid, 'problem')
        db.clear_draft(uid)
        await msg.reply_text(PROMPT, reply_markup=NEXT)
    elif text == BUTTONS['BOOKING']:
        if not db.latest(uid):
            await msg.reply_text('Заявки нет. Создайте новую через /start.', reply_markup=MENU)
            return
        await msg.reply_text('Выберите дату и время визита мастера.', reply_markup=ReplyKeyboardRemove())
        await show_dates(msg, uid, context)
    elif stage in ('booking', 'booking_date', 'booking_time'):
        if stage == 'booking_time' and db.selected_day(uid) in booking_days():
            await show_times(msg, uid, db.selected_day(uid), context)
        else:
            await show_dates(msg, uid, context)

    else:
        await msg.reply_text('Выберите раздел меню.', reply_markup=MENU)


async def show_dates(msg, uid, context):
    db = store(context)
    db.begin_booking(uid)
    buttons = [[InlineKeyboardButton(datetime.fromisoformat(day).strftime('%d.%m.%Y'), callback_data=f'book:day:{day}')]
               for day in booking_days()]
    await msg.reply_text('Выберите дату визита (время Самары, UTC+4). Отмена: /cancel.',
                         reply_markup=InlineKeyboardMarkup(buttons))


async def show_times(msg, uid, day, context):
    clocks = store(context).available_times(day, uid)
    buttons = [InlineKeyboardButton(clock, callback_data=f'book:slot:{day}:{clock}') for clock in clocks]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(BUTTONS['CHANGE_DATE'], callback_data='book:dates')])
    caption = f'{datetime.fromisoformat(day):%d.%m.%Y}: выберите свободное время (Самара, UTC+4).'
    if not clocks:
        caption = 'На эту дату свободного времени нет. Выберите другую дату.'
    await msg.reply_text(caption, reply_markup=InlineKeyboardMarkup(rows))


async def booking_callback(update, context):
    query = update.callback_query
    uid = query.from_user.id
    db = store(context)
    user = db.user(uid)
    if not user or not db.latest(uid):
        await query.answer('Сначала заполните анкету через /start.', show_alert=True)
        return
    if user['stage'] not in ('booking', 'booking_date', 'booking_time'):
        await query.answer('Эта кнопка устарела. Откройте «Запись» в меню.', show_alert=True)
        return
    await query.answer()
    if query.data == 'book:dates':
        await show_dates(query.message, uid, context)
        return
    parts = query.data.split(':')
    day = parts[2]
    if day not in booking_days():
        await show_dates(query.message, uid, context)
        return
    if parts[1] == 'day':
        db.select_day(uid, day)
        await show_times(query.message, uid, day, context)
        return
    if user['stage'] != 'booking_time' or db.selected_day(uid) != day:
        await query.message.reply_text('Выберите дату заново.')
        await show_dates(query.message, uid, context)
        return
    clock = ':'.join(parts[3:])
    try:
        booking = db.book(uid, day, clock)
    except (SlotUnavailable, ValueError) as error:
        await query.message.reply_text(str(error))
        await show_times(query.message, uid, day, context)
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except MAXError:
        # Запись уже сохранена; ошибка старого сообщения не отменяет уведомления.
        pass
    row = db.latest(uid)
    await notify(context, f'Запись к мастеру, заявка №{row["id"]}\n{user["name"]} (ID {uid})\nТелефон: {user["phone"]}\nДата и время: {booking} Самара, UTC+4\nПроблема: {row["problem"]}')
    await query.message.reply_text(f'Вы записаны на {booking} (Самара, UTC+4). Выберите раздел меню.', reply_markup=MENU)


async def cancel(update, context):
    db = store(context)
    uid = update.effective_user.id
    if db.latest(uid):
        db.stage(uid, 'menu')
        db.clear_draft(uid)
        await update.message.reply_text('Вы вернулись в меню.', reply_markup=MENU)
    else:
        await update.message.reply_text('Сначала завершите анкету. Начать заново: /start.')


async def error_handler(update, context):
    logging.getLogger(__name__).error('Ошибка обработки: %s', type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text('Не удалось выполнить действие. Попробуйте ещё раз или нажмите /start.')
        except MAXError:
            pass


def main():
    load_dotenv(ROOT / '.env.local')
    load_dotenv(ROOT / '.env')
    configure_buttons()
    token = os.getenv('BOT_TOKEN', '').strip()
    admins = {int(value.strip()) for value in os.getenv('ADMIN_IDS', '').split(',') if value.strip()}
    if not token:
        raise SystemExit('Укажите BOT_TOKEN в .env.local (см. .env.example).')
    if not admins:
        logging.warning('ADMIN_IDS не заполнен: уведомления администраторам отключены. Получите ID командой /myid.')
    path = Path(os.getenv('DATABASE_PATH', 'data/bot.sqlite3'))
    if not path.is_absolute():
        path = ROOT / path
    import asyncio
    from max_api import MAXClient, run_polling
    client = MAXClient(token)
    try:
        asyncio.run(run_polling(client, Store(path), admins))
    finally:
        client.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    main()
