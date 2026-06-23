# Архитектура

## Обзор

Приложение — это FastAPI-сервис, который оркеструет цепочку обработки документа и хранит
результат в Qdrant. Тяжёлая работа (разбор документа, разметка структуры, тегирование,
векторизация) выполняется в фоне; статусы фоновых задач и реестр документов ведутся в Redis;
большая языковая модель и эмбеддинг-модель вызываются через Ollama.

## Стек

| Компонент | Роль |
|-----------|------|
| FastAPI | HTTP API, фоновые задачи |
| Qdrant | векторная база; одна коллекция, payload-индексы |
| Redis | статусы задач парсинга, реестр документов и версий |
| Ollama | LLM (разметка, мердж, теги, версия) и эмбеддинги |
| unstructured (python-docx) | извлечение текста и таблиц из `.docx` |
| pydantic-settings | конфигурация через переменные окружения |
| structlog | структурированное логирование |

## ООП и контейнер зависимостей

Каждый функциональный модуль оформлен отдельным классом. Все объекты собираются в контейнер
`Dependencies` функцией `init_dependencies()` (`src/dependencies/init_dependencies.py`).
`Dependencies` — синглтон: он наполняется один раз на старте приложения в `lifespan` и затем
доступен из любого места. Обработчики получают отдельные зависимости через геттеры FastAPI,
например `Depends(Dependencies.get_search)`; весь контейнер целиком доступен через
`get_dependencies()`.

Классы-этапы конвейера не хранят состояние между документами. Клиент Ollama (`OllamaClient`)
создаётся внутри сервисов на каждую операцию, что делает фоновую обработку потокобезопасной.

Состав контейнера:

```
Dependencies(
    settings, qdrant, redis, jobs, registry,
    parser, structure, hierarchy, tagger, version_detector,
    ingestion, search,
)
```

## Структура проекта

| Путь | Содержимое |
|------|------------|
| `src/common/config/app_config.py` | `Settings` — конфигурация (pydantic-settings) |
| `src/api_clients/ollama_client.py` | `OllamaClient`, `OllamaError` |
| `src/common/db/qdrant_client.py` | `QdrantRepository` |
| `src/common/db/redis_client.py` | `RedisClient`, `JobStore`, `DocumentRegistry` |
| `src/dvd_service/modules/doc_parsers.py` | `DocumentParser` (этапы 1 и 1.5) |
| `src/dvd_service/modules/structure.py` | `StructureTagger` (этапы 2, 3, 3.5) |
| `src/dvd_service/modules/hierarchy.py` | `HierarchyBuilder` (этап 4 и развёртка узлов) |
| `src/dvd_service/modules/tagging.py` | `Tagger`, `VersionDetector` |
| `src/dvd_service/modules/windowing.py` | `make_windows`, `reconcile` |
| `src/dvd_service/services/dvd_service.py` | `IngestionService`, `SearchService` |
| `src/dvd_service/dto/` | `NodePayload` (`node_payload.py`) и DTO запросов/ответов (`upload.py`, `search.py`) |
| `src/dvd_service/routers/` | HTTP-эндпоинты (`documents.py`, `search.py`) |
| `src/dependencies/dependencies.py` | `Dependencies` (синглтон) и геттеры |
| `src/dependencies/init_dependencies.py` | `init_dependencies` |
| `src/mcp_server/server.py` | MCP-сервер (fastmcp): инструменты-геттеры |
| `src/mcp_server/app.py` | ASGI-приложение MCP-сервера для монтирования |
| `src/main.py` | сборка приложения, `lifespan` |
| `src/dev_runner.py` | запуск uvicorn для разработки |

## Классы

### Инфраструктура

- `OllamaClient` — синхронный клиент Ollama. Метод `chat(system, user, schema)` выполняет
  запрос со строгой JSON-схемой ответа; `embed(texts)` возвращает векторы; `available()`
  проверяет доступность.
- `QdrantRepository` — обёртка над клиентом Qdrant: `ensure_collection()` (идемпотентное
  создание коллекции и payload-индексов), `upsert(points)`, `search(vector, filter, limit)`,
  `retrieve(ids)`, `set_other_versions(name, version, other_versions)`.
- `RedisClient` — подключение к Redis. `JobStore` — статусы задач (`dvd:job:{id}`).
  `DocumentRegistry` — хэши документов для дедупликации (`dvd:hash:{hash}`) и множества версий
  по имени документа (`dvd:versions:{name}`).

### Конвейер

- `DocumentParser` — извлечение из `.docx` (`extract_raw`), хэш полного текста
  (`content_hash`), дробление и склейка в логические части (`to_logical_parts`, `semantic_merge`).
- `StructureTagger` — разметка структуры (`tag`), нормализация типа (`categorize`), удаление
  дублирующего номера из текста (`strip_leading_numbering`), ранг нумерации
  (`numbering_rank`, `numbering_ranks`).
- `HierarchyBuilder` — построение дерева (`build`), пост-валидация (`cap_unnumbered_nesting`),
  группировка изменений (`group_amendment`), развёртка в плоские узлы (`flatten`).
- `Tagger` — тегирование фрагментов (`tag_nodes`).
- `VersionDetector` — определение имени и версии документа (`detect`).

### Сервисы

- `IngestionService.ingest(file_path, raw, content_hash, ...)` — полный конвейер обработки и
  загрузка узлов в Qdrant.
- `SearchService.search(request, kind)` — векторизация запроса, фильтрация, поиск и сборка
  контекста по соседним фрагментам.

## MCP

`src/mcp_server/server.py` оформляет read-only геттеры приложения как инструменты MCP (fastmcp)
поверх того же контейнера `Dependencies` — без отдельной инициализации БД/Redis:

- `search_texts`, `search_tables`, `search_all` — обёртки над `SearchService.search`.
- `job_status` — обёртка над `JobStore.get`.
- `document_versions` — обёртка над `DocumentRegistry.versions`.

ASGI-приложение MCP-сервера (`src/mcp_server/app.py`) монтируется в основное FastAPI-приложение
(`src/main.py`) на пути `/mcp` (streamable HTTP transport); `lifespan` MCP-сервера объединён с
`lifespan` приложения, чтобы оба запускались в одном процессе.

## Модель данных

Каждый узел документа — отдельная точка Qdrant. Идентификатор точки — UUID. Векторизуется поле
`text`. Состав payload (`NodePayload`):

| Поле | Тип | Описание |
|------|-----|----------|
| `doc_id` | str | идентификатор загрузки документа |
| `name` | str | обозначение документа (например, «СП 19.13330.2019») |
| `version` | str | версия/редакция |
| `other_versions` | list[str] | другие версии этого документа в базе |
| `content_hash` | str | хэш полного текста документа |
| `source` | str | имя исходного файла |
| `kind` | str | `text` или `table` |
| `type` | str | тип структурного элемента |
| `numbering` | str | собственный номер фрагмента |
| `block` | str | `main` или `amendment` |
| `depth` | int | глубина в иерархии |
| `parent_id` | str | идентификатор родительского узла |
| `parent_text` | str | текст родителя |
| `child_ids` | list[str] | идентификаторы дочерних узлов |
| `prev_id` | str | предыдущий фрагмент по порядку чтения |
| `next_id` | str | следующий фрагмент по порядку чтения |
| `breadcrumb` | str | путь от корня (раздел / пункт) |
| `tags` | list[str] | теги |
| `table_html` | str | HTML-представление таблицы (для `kind=table`) |
| `text` | str | текст фрагмента |

Payload-индексы создаются по полям `doc_id`, `name`, `version`, `kind`, `type`, `block`,
`parent_id`, `content_hash`, `tags`.

## Хранилища

- Qdrant: одна коллекция (по умолчанию `documents`), вектор размерности `vector_size`, метрика
  косинус. Тексты и таблицы лежат в одной коллекции и различаются полем `kind`.
- Redis: статусы задач (`dvd:job:{job_id}`, с TTL), реестр хэшей (`dvd:hash:{hash}`) и версий
  (`dvd:versions:{name}`).
