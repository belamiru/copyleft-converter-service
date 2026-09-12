# Copyleft Converter Service

Сервис конвертации PDF, DOCX, XLSX и PPTX в PDF для сайта типографии Copyleft.

## Развёртывание на VPS

1. Получить актуальный код:
   git pull

2. Убедиться, что в корне проекта есть файл `.env` с переменной:
   CONVERTER_API_KEY=...

3. Пересобрать и запустить сервис:
   docker compose up -d --build converter-api

4. Проверить состояние:
   docker compose ps
