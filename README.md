# Visual Kei Bookshelf — server search prototype

Это первый серверный прототип поискового слоя.

## Запуск
Требуется Python 3.10+.

```bash
python server.py
```

Открой:
http://127.0.0.1:8765

Поиск идёт через сервер в:
- Google Books
- Open Library
- OPDS из sources.json

OPDS-поиск реализован через OpenSearch discovery: сервер сначала читает каталог, ищет `rel="search"`, затем получает search template и выполняет запрос.

ISBN не обязателен.

## Важно
Это прототип. Сервер не скачивает книги и не обходит защиту каталогов. Он работает только с открытыми HTTP-эндпоинтами, которые разрешают запросы.
