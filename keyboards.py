"""Reusable inline keyboards for bot navigation."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu(has_access: bool, can_buy: bool, is_admin: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_access:
        rows.append([
            InlineKeyboardButton(text="Статус", callback_data="menu:status"),
            InlineKeyboardButton(text="Настройки", callback_data="menu:settings"),
        ])
    if can_buy:
        rows.append([InlineKeyboardButton(text="Подписка", callback_data="menu:buy")])
    if is_admin:
        rows.append([InlineKeyboardButton(text="Админ-панель", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="В главное меню", callback_data="menu:home")]])


def settings_menu(paused: bool) -> InlineKeyboardMarkup:
    action = "resume" if paused else "pause"
    label = "Включить сигналы" if paused else "Поставить на паузу"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"settings:{action}")],
        [InlineKeyboardButton(text="Площадки и фильтры", callback_data="markets:open")],
        [InlineKeyboardButton(text="В главное меню", callback_data="menu:home")],
    ])


def marketplaces_menu(skinport_enabled: bool, dmarket_enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Skinport: {'вкл' if skinport_enabled else 'выкл'}",
            callback_data="markets:toggle:skinport")],
        [InlineKeyboardButton(
            text=f"DMarket: {'вкл' if dmarket_enabled else 'выкл'}",
            callback_data="markets:toggle:dmarket")],
        [InlineKeyboardButton(text="Фильтры DMarket", callback_data="markets:filters:dmarket")],
        [InlineKeyboardButton(text="Назад к настройкам", callback_data="menu:settings")],
    ])


def admin_menu(stars_enabled: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Участники", callback_data="admin:users"),
         InlineKeyboardButton(text="Приглашение", callback_data="admin:invite")],
        [InlineKeyboardButton(text="Как выдать / продлить", callback_data="admin:usage:sub"),
         InlineKeyboardButton(text="Как отозвать", callback_data="admin:usage:revoke")],
    ]
    if stars_enabled:
        rows.append([InlineKeyboardButton(text="Как вернуть Stars", callback_data="admin:usage:refund")])
    rows.append([InlineKeyboardButton(text="В главное меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
