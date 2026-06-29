# API

Базовый URL по умолчанию — `http://localhost:8000`. Интерактивная документация (Swagger) —
`/docs`. Все модели запросов и ответов основаны на pydantic и описаны в каталоге
`src/dvd_service/dto/`.

## Перечень эндпоинтов

| Метод и путь | Назначение |
|--------------|------------|
| `POST /documents` | загрузка документа и постановка в очередь обработки |
| `GET /documents` | список загруженных документов, агрегированных по (name, version), с фильтрами |
| `GET /documents/{job_id}` | статус задачи обработки |
| `POST /search/texts` | поиск релевантных текстовых фрагментов |
| `POST /search/tables` | поиск релевантных таблиц |
| `POST /search` | поиск по всем сущностям (тексты и таблицы) |
| `GET /library/documents` | список документов из реестра (метадата идентичности/корпуса) |
| `GET /library/documents/{doc_id}` | один документ: собранный текст + метадата + упорядоченные фрагменты |
| `GET /library/lookup` | резолв документов по точному ключу / внешнему id |
| `GET /ping` | проверка работоспособности |
| `GET /` | редирект на `/docs` |

## POST /documents

Загрузка документа. Тело — multipart-форма.

Поля формы:

- `file` — файл документа (обязательно);
- `version` — строка с версией для переопределения автоопределения (необязательно);
- `doc_type` — класс документа (`document` / `regulation` / `article` / `book` / `web` / …) (необязательно);
- `corpus` — логический корпус/неймспейс документа (необязательно);
- `lang` — код языка ISO-639 (необязательно);
- `title` — человекочитаемый заголовок (необязательно);
- `source_uri` — путь/URL источника (необязательно);
- `effective_date` — дата вступления в силу (необязательно);
- `external_ids` — JSON-объект с id вызывающего, например `{"code": "СП 19.13330.2019", "doi": "..."}` (необязательно);
- `metadata` — JSON-объект произвольных доменных атрибутов (необязательно).

Вся необязательная метадата сохраняется на каждом узле документа, чтобы сервисы-потребители могли
связывать, фильтровать и цитировать без повторного разбора. `external_ids` / `metadata` должны быть
JSON-объектами (иначе — `422`).

Поведение:

- Принимаемые форматы задаются `DVD_ALLOWED_EXTENSIONS` (по умолчанию `.docx`, `.txt`, `.md`,
  `.html`, `.htm` — OCR-free форматы через `unstructured`; скан-PDF/OCR отложен). Иной формат — `415`.
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

## GET /documents

Документы, уже находящиеся в базе, агрегированные по `(name, version)` — одна запись на версию
документа, а не на фрагмент. Строится сканированием Qdrant и группировкой payload фрагментов;
источник — не реестр в Redis (он хранит только факт существования имени/версии, но не эти данные).

Параметры запроса:

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|--------------|----------|
| `name` | str | null | фильтр по названию документа |
| `version` | str | null | фильтр по версии |
| `block` | str | null | фильтр по `main`/`amendment` — оставляет документы, у которых есть хотя бы один узел этого блока |
| `tags` | list[str] (повторяемый) | null | фильтр по тегам (любой из) |
| `uploaded_from` | str (ISO 8601) | null | только документы, загруженные не раньше этой метки времени |
| `uploaded_to` | str (ISO 8601) | null | только документы, загруженные не позже этой метки времени |

`name`/`version`/`block`/`tags` передаются как payload-фильтры Qdrant (все четыре поля
проиндексированы); `uploaded_from`/`uploaded_to` применяются после агрегации, поскольку время
загрузки — это факт уровня документа, собранный из фрагментов, а не индексированное поле
фрагмента.

Ответ (`DocumentListResponse`):

```json
{
  "count": 1,
  "documents": [
    {
      "doc_id": "9f63...",
      "name": "СП 19.13330.2019",
      "version": "СП 19.13330.2019 (с Изменением N 1)",
      "other_versions": [],
      "blocks": ["amendment", "main"],
      "tags": ["зонирование", "противопожарные расстояния"],
      "node_count": 266,
      "uploaded_at": "2026-06-28T12:34:56.789012+00:00",
      "source": "СП_19.13330.2019_с_И1.docx"
    }
  ]
}
```

`blocks` и `tags` — объединение по всем фрагментам этой версии документа; `node_count` — число
фрагментов (тексты и таблицы вместе).

Примеры:

```
curl "http://localhost:8000/documents"
curl "http://localhost:8000/documents?name=СП%2019.13330.2019"
curl "http://localhost:8000/documents?block=amendment&tags=зонирование&tags=здания"
curl "http://localhost:8000/documents?uploaded_from=2026-06-01T00:00:00%2B00:00"
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
| `block` | str | null | фильтр по `main`/`amendment` |
| `types` | list[str] | null | фильтр по структурному уровню (`chapter`/`clause`/`subclause`/...; любой из) |
| `doc_id` | str | null | фильтр по конкретному документу |
| `doc_type` | str | null | фильтр по типу документа (`regulation`/`article`/…) |
| `corpus` | str | null | фильтр по логическому корпусу/неймспейсу |
| `lang` | str | null | фильтр по языку |
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
      "block": "main",
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

Кроме показанных выше полей, каждый хит несёт общие поля идентичности и привязки к источнику из
payload: `title`, `version_id`, `doc_type`, `corpus`, `lang`, `external_ids`, `order`, `metadata`
и спан источника (`source_uri`, `char_start`, `char_end`, `page_start`, `page_end`, `span_id`) —
чтобы вызывающий мог сослаться на точное место в источнике для каждого хита.

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

curl -X POST http://localhost:8000/search/texts \
     -H "Content-Type: application/json" \
     -d '{"query": "расстояния", "block": "amendment", "types": ["clause", "subclause"]}'
```

Ввод кириллицы в `-d` из консоли Windows может искажаться кодировкой; для ручной проверки
удобнее использовать Swagger (`/docs`).

## Library (документ-API чтения)

API чтения для потребителей (например, для сервиса MSI-TSIM), дополняющее семантический поиск
прямым доступом по документам: перечислить документы и получить один по `doc_id` как собранный
текст + метадата + упорядоченные фрагменты, каждый со своей привязкой к источнику.

### GET /library/documents

Все документы, зарегистрированные в базе (из реестра Redis). Ответ (`DocumentList`):

```json
{
  "count": 1,
  "documents": [
    {
      "doc_id": "9f63...",
      "name": "СП 19.13330.2019",
      "title": null,
      "version": "СП 19.13330.2019 (с Изменением N 1)",
      "version_id": "сп_19_13330_2019__sha256_ab12cd34ef56",
      "other_versions": [],
      "doc_type": "regulation",
      "corpus": "norms",
      "lang": "ru",
      "status": "active",
      "external_ids": { "code": "СП 19.13330.2019" },
      "source_uri": "СП_19.13330.2019_с_И1.docx",
      "content_hash": "…",
      "node_count": 266,
      "uploaded_at": "2026-06-28T12:34:56.789012+00:00"
    }
  ]
}
```

### GET /library/lookup

Резолв документов по точному ключу поиска / значению внешнего id (например, по обозначению норматива).
Параметр запроса `key` (обязательно). Возвращает ту же структуру `DocumentList`.

```
curl "http://localhost:8000/library/lookup?key=СП%2019.13330.2019"
```

### GET /library/documents/{doc_id}

Один документ как `DocumentDetail` — поля `DocumentSummary` выше плюс собранный полный `text`
(фрагменты, склеенные в порядке чтения) и массив `fragments`. Если документ не найден — `404`.

Каждый фрагмент несёт `id`, `order`, `kind`, `type`, `numbering`, `depth`, `breadcrumb`,
`parent_id`/`prev_id`/`next_id`, привязку к источнику (`char_start`, `char_end`, `page_start`,
`page_end`, `span_id`), `tags`, `metadata`, `text` и `table_html`.

```json
{
  "doc_id": "9f63...",
  "name": "СП 19.13330.2019",
  "version": "СП 19.13330.2019 (с Изменением N 1)",
  "node_count": 266,
  "text": "… полный текст документа в порядке чтения …",
  "fragments": [
    {
      "id": "a1b2...",
      "order": 0,
      "kind": "text",
      "type": "title_page",
      "numbering": "",
      "char_start": 0,
      "char_end": 38,
      "span_id": "9f63...:span:0:38",
      "text": "СП 19.13330.2019 …"
    }
  ]
}
```

```
curl "http://localhost:8000/library/documents/9f63..."
```
