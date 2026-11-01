from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SAMARA = ZoneInfo('Europe/Samara')
TIMES = tuple(f'{hour:02d}:00' for hour in range(8, 23, 2))
BOOKING_DAYS = 7


def local_now():
    return datetime.now(SAMARA)


def booking_days():
    today = local_now().date()
    return [(today + timedelta(days=offset)).isoformat() for offset in range(BOOKING_DAYS)]


def slot_start(day, clock):
    parsed = date.fromisoformat(day)
    if parsed.isoformat() != day or clock not in TIMES:
        raise ValueError('Некорректная дата или время')
    return datetime.combine(parsed, time.fromisoformat(clock), SAMARA)


def validate_slot(day, clock):
    start = slot_start(day, clock)
    if day not in booking_days() or start <= local_now():
        raise ValueError('Это время недоступно. Выберите будущую дату и время.')
    return start
