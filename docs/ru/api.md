# API

Базовый URL по умолчанию — `http://localhost:8000`. Интерактивная документация (Swagger) —
`/docs`. Все модели запросов и ответов основаны на pydantic и описаны в каталоге
`src/dvd_service/dto/`.

## Перечень эндпоинтов

| Метод и путь | Назначение |
|--------------|------------|
| `POST /documents` | загрузка документа `.docx` и постановка в очередь обработки |
| `GET /documents/{job_id}` | статус задачи обработки |
| `POST /search/texts` | поиск релевантных текстовых фрагментов |
| `POST /search/tables` | поиск релевантных таблиц |
| `POST /search` | поиск по всем сущностям (тексты и таблицы) |
| `GET /ping` | проверка работоспособности |
| `GET /` | редирект на `/docs` |

## POST /documents

Загрузка документа. Тело — multipart-форма.

Поля формы:

- `file` — файл `.docx` (обязательно);
- `version` — строка с версией для переопределения автоопределения (необязательно).

Поведение:

- Принимается только `.docx`. Иной формат — `415`.
- Файл, текст которого полностью совпадает с уже загруженным, отклоняется — `400`.
- Файл, который не удалось разобрать, — `422`.
- В успешном случае — `202` и идентификатор задачи; обработка идёт в фоне.

Ответ (`202`):

```json
{ "job_id": "1f0c...", "status": "queued" }
```

Пример:

```
curl -X POST http://localhost:8000/documents \
     -F "file=@docs_data/docs_examples/СП_19.13330.2019_с_И1.docx"
```

## GET /documents/{job_id}

Статус фоновой задачи. Источник — Redis.

Ответ:

```json
{
  "job_id": "1f0c...",
  "status": "done",
  "filename": "СП_19.13330.2019_с_И1.docx",
  "doc_id": "9f63...",
  "name": "СП 19.13330.2019",
  "version": "СП 19.13330.2019 (с Изменением N 1)",
  "other_versions": [],
  "nodes": 266,
  "error": null
}
```

Возможные значения `status`: `queued`, `processing`, `done`, `error`. Если задача не найдена —
`404`.

## Поиск

Эндпоинты `/search/texts`, `/search/tables` и `/search` принимают одно и то же тело запроса;
различаются только сущностью (`kind`), по которой идёт поиск: тексты, таблицы или всё.

Тело запроса (`SearchRequest`):

| Поле | Тип | По умолчанию | Описание |
|------|-----|--------------|----------|
| `query` | str | — | поисковый запрос |
| `name` | str | null | фильтр по названию документа |
| `version` | str | null | фильтр по версии |
| `tags` | list[str] | null | фильтр по тегам (любой из) |
| `limit` | int | 10 | число результатов |
| `context_height` | int | 0 | сколько фрагментов до и после подклеить |

Ответ (`SearchResponse`):

```json
{
  "count": 3,
  "hits": [
    {
      "id": "a1b2...",
      "score": 0.704,
      "doc_id": "9f63...",
      "name": "СП 19.13330.2019",
      "version": "СП 19.13330.2019 (с Изменением N 1)",
      "other_versions": [],
      "kind": "text",
      "type": "clause",
      "numbering": "7.13",
      "breadcrumb": "СП 19.13330.2019 / 7 Инженерные сети / 7.13",
      "parent_id": "...",
      "prev_id": "...",
      "next_id": "...",
      "tags": ["противопожарные расстояния", "здания"],
      "references": [
        {
          "raw": "СП 42.13330.2016, п. 7.5",
          "target_name": "СП 42.13330.2016",
          "target_numbering": "7.5",
          "scope": "external",
          "target_doc_id": "c0ffee...",
          "target_version": "СП 42.13330.2016",
          "target_node_id": "b1ab1a...",
          "resolved": true
        }
      ],
      "text": "Расстояния от зданий и сооружений ...",
      "context": "... предыдущий фрагмент ... целевой фрагмент ... следующий фрагмент ...",
      "table_html": null
    }
  ]
}
```

Результаты отсортированы по убыванию релевантности (`score` — косинусная близость). Поле
`context` заполняется только при `context_height > 0`. Для таблиц заполняется `table_html`.

Примеры:

```
curl -X POST http://localhost:8000/search/texts \
     -H "Content-Type: application/json" \
     -d '{"query": "санитарно-защитная зона", "limit": 3, "context_height": 1}'

curl -X POST http://localhost:8000/search/tables \
     -H "Content-Type: application/json" \
     -d '{"query": "расстояния между зданиями", "limit": 3}'

curl -X POST http://localhost:8000/search/texts \
     -H "Content-Type: application/json" \
     -d '{"query": "размещение предприятий", "version": "СП 19.13330.2019 (с Изменением N 1)", "tags": ["зонирование"]}'
```

Ввод кириллицы в `-d` из консоли Windows может искажаться кодировкой; для ручной проверки
удобнее использовать Swagger (`/docs`).
