class ParsingError(Exception):
    """Базовое исключение при парсинге"""
    pass


class SettingsNotFoundError(ParsingError):
    """Код поставщика отсутствует в settings.json"""
    pass


class InvalidTableStructureError(ParsingError):
    """Таблица в PDF не соответствует ожидаемой структуре"""
    pass


class PriceParsingError(ParsingError):
    """Не удалось распарсить цены или дату из объединённой ячейки"""
    pass
