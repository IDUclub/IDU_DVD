# Архитектура

## Обзор

Приложение — это FastAPI-сервис, который оркеструет цепочку обработки документа и хранит
результат в Qdrant. Тяжёлая работа (разбор документа, разметка структуры, тегирование,
векторизация) выполняется в фоне; статусы фоновых задач и реестр документов ведутся в Redis;
большая языковая модель вызывается через Ollama, эмбеддинги — через GPU-сервис giga-vectorizer
(Ollama остаётся резервным провайдером — `DVD_EMBEDDINGS_PROVIDER`).

## Стек

| Компонент | Роль |
|-----------|------|
| FastAPI | HTTP API, фоновые задачи |
| Qdrant | векторная база; отдельная коллекция под каждое векторное пространство (namespacing), payload-индексы |
| Redis | статусы задач парсинга, реестр документов и версий (неймспейсится по коллекции), outbox Kafka-событий |
| Ollama | LLM (разметка, мердж, теги, версия); резервный провайдер эмбеддингов |
| giga-vectorizer | эмбеддинги (Giga-Embeddings-instruct, 2048-d) через OpenAI-совместимый `/v1/embeddings`; только CUDA, отдельный репозиторий |
| Kafka (otteroad) | опциональная публикация событий жизненного цикла документов (`DocumentProcessed` / `DocumentUpdated` / `DocumentDeleted`) для смежных сервисов |
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
и векторизатор (`create_embedder()`) создаются внутри сервисов на каждую операцию, что делает
фоновую обработку потокобезопасной.

Состав контейнера:

```
Dependencies(
    settings, qdrant, redis, jobs, registry,
    parser, structure, hierarchy, tagger, version_detector,
    reference_extractor, reference_resolver,
    outbox, publisher,
    ingestion, search, documents, library,
)
```

## Структура проекта

| Путь | Содержимое |
|------|------------|
| `src/common/config/app_config.py` | `Settings` — конфигурация (pydantic-settings) |
| `src/api_clients/ollama_client.py` | `OllamaClient`, `OllamaError` |
| `src/api_clients/embeddings_client.py` | `GigaEmbeddingsClient`, `EmbeddingsError`, `create_embedder` (выбор провайдера) |
| `src/common/db/qdrant_client.py` | `QdrantRepository` |
| `src/common/db/redis_client.py` | `RedisClient`, `JobStore`, `DocumentRegistry` |
| `src/broker/` | интеграция с Kafka: модели событий `DocumentProcessed` / `DocumentUpdated` / `DocumentDeleted` (`events.py`), `EventOutbox` (`outbox.py`), `KafkaPublisher` (`publisher.py`) |
| `src/dvd_service/modules/doc_parsers.py` | `DocumentParser` (этапы 1 и 1.5) |
| `src/dvd_service/modules/structure.py` | `StructureTagger` (этапы 2, 3, 3.5) |
| `src/dvd_service/modules/hierarchy.py` | `HierarchyBuilder` (этап 4 и развёртка узлов) |
| `src/dvd_service/modules/tagging.py` | `Tagger`, `VersionDetector` |
| `src/dvd_service/modules/windowing.py` | `make_windows`, `reconcile` |
| `src/dvd_service/services/dvd_service.py` | `IngestionService`, `SearchService`, `DocumentsService`, `LibraryService` |
| `src/dvd_service/modules/identity.py` | хелперы идентичности документа (`normalize_key`, `make_version_id`, `make_span_id`, `build_aliases`, `build_lookup_keys`) |
| `src/dvd_service/dto/` | `NodePayload` (`node_payload.py`) и DTO запросов/ответов (`upload.py`, `search.py`, `document.py`, `reference.py`) |
| `src/dvd_service/routers/` | HTTP-эндпоинты (`documents.py`, `search.py`, `library.py`) |
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
- `GigaEmbeddingsClient` — синхронный клиент сервиса giga-vectorizer (OpenAI-совместимый
  `/v1/embeddings` с расширением `prompt`). `embed_documents(texts)` векторизует без
  инструкции-префикса, `embed_query(text)` — с ней (Giga-Embeddings-instruct — асимметричная
  модель). `create_embedder()` возвращает клиент настроенного `DVD_EMBEDDINGS_PROVIDER`; оба
  провайдера имеют одинаковый интерфейс `embed_documents` / `embed_query`.
- `QdrantRepository` — обёртка над клиентом Qdrant: `ensure_collection()` (идемпотентное
  создание коллекции и payload-индексов; в фиксированном режиме также падает при несовпадении
  размерности), `upsert(points)`, `search(vector, filter, limit)`, `retrieve(ids)`,
  `set_other_versions(name, version, other_versions)`. Имя физической коллекции берётся из
  `Settings.effective_collection` (неймспейсится по векторному пространству — см. «Конфигурацию»).
- `RedisClient` — подключение к Redis. `JobStore` — статусы задач (`dvd:job:{id}`).
  `DocumentRegistry(prefix)` — хэши документов для дедупликации (`{prefix}:hash:{hash}`) и множества
  версий по имени документа (`{prefix}:versions:{name}`); `prefix` (`Settings.registry_prefix`)
  скоупит реестр по физической коллекции, так что у каждого векторного пространства свои дедуп/версии.
- `EventOutbox` / `KafkaPublisher` (`src/broker/`) — опциональная публикация в Kafka через
  фреймворк [otteroad](https://github.com/IDUclub/otteroad) (AVRO + Schema Registry).
  `IngestionService` добавляет события жизненного цикла в outbox-список в Redis (топик
  `document.events`): `DocumentProcessed` при первичной загрузке, `DocumentUpdated` при
  дельта-обновлении/полной перезагрузке, `DocumentDeleted` при удалении; асинхронный публикатор,
  запускаемый в `lifespan`, доотправляет их в Kafka с ретраями (at-least-once), перенося
  исчерпавшие попытки события в dead-letter-список. Полностью выключено, пока не задан
  `DVD_KAFKA_BOOTSTRAP_SERVERS`.

### Конвейер

- `DocumentParser` — извлечение из `.docx` (`extract_raw`), хэш полного текста
  (`content_hash`), дробление и склейка в логические части (`to_logical_parts`, `semantic_merge`).
- `StructureTagger` — разметка структуры **и тегирование фрагментов** за один прогон LLM (`tag`),
  нормализация типа (`categorize`), удаление дублирующего номера из текста
  (`strip_leading_numbering`), ранг нумерации (`numbering_rank`, `numbering_ranks`).
- `HierarchyBuilder` — построение дерева (`build`), пост-валидация (`cap_unnumbered_nesting`),
  группировка изменений (`group_amendment`), развёртка в плоские узлы (`flatten`); переносит теги из
  прогона разметки на каждый узел.
- `VersionDetector` — определение имени и версии документа (`detect`).

### Сервисы

- `IngestionService.ingest(file_path, raw, content_hash, ...)` — полный конвейер обработки и
  загрузка узлов в Qdrant.
- `SearchService.search(request, kind)` — векторизация запроса, фильтрация (`name`,
  `document_names`, `version`, `block`, `types`, `tags`), поиск и сборка контекста по соседним
  фрагментам.
- `DocumentsService.list_documents(...)` — представление по документам, агрегированное по
  `(name, version)` из payload фрагментов Qdrant (число узлов, присутствующие блоки, объединение
  тегов, время загрузки), с фильтрами по `name`, `version`, `block`, `tags` и диапазону
  `uploaded_at`.
- `LibraryService` — документ-API для потребителей (например, сервиса MSI-TSIM):
  `list_documents()` (из реестра Redis), `get_document(doc_id)` (собранный полный текст + метадата
  + упорядоченные фрагменты с привязкой к источнику, из Qdrant) и `find_documents(key)` (резолв
  документов по точному ключу/внешнему идентификатору).

## MCP

`src/mcp_server/server.py` оформляет read-only геттеры приложения как инструменты MCP (fastmcp)
поверх того же контейнера `Dependencies` — без отдельной инициализации БД/Redis:

- `search_texts`, `search_tables`, `search_all` — обёртки над `SearchService.search` (фильтры:
  `name`, `document_names`, `version`, `block`, `types`, `tags`).
- `list_documents` — обёртка над `DocumentsService.list_documents`.
- `job_status` — обёртка над `JobStore.get`.
- `document_versions` — обёртка над `DocumentRegistry.versions`.
- `pending_references` — обёртка над `DocumentRegistry.peek_pending`.
- `get_document` — обёртка над `LibraryService.get_document` (полный текст + метадата + фрагменты).
- `find_document` — обёртка над `LibraryService.find_documents` (резолв по ключу/внешнему id).
- `get_tags` — обёртка над `TagsService.get_tags` (без параметров; все уникальные теги коллекции,
  отсортированные по алфавиту).

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
| `title` | str | человекочитаемый заголовок, если отличается от `name` |
| `version` | str | версия/редакция |
| `version_id` | str | стабильный id конкретной редакции/файла (`<норм. имя>__sha256_<12>`) |
| `other_versions` | list[str] | другие версии этого документа в базе |
| `content_hash` | str | хэш полного текста документа |
| `doc_type` | str | класс документа: `document` / `regulation` / `article` / `book` / `web` / … |
| `corpus` | str | логический корпус/неймспейс документа |
| `lang` | str | код языка ISO-639, если известен |
| `external_ids` | dict | переданные вызывающим id (`{code, doi, isbn, url, …}`) — хранятся как есть, не интерпретируются |
| `aliases` | list[str] | человекочитаемые обозначения (имя + значения внешних id) |
| `lookup_keys` | list[str] | ключи точного поиска (нормализованное имя + формы внешних id) |
| `status` | str | `active` / `archived` |
| `effective_date` | str | дата вступления в силу, если задана |
| `supersedes` / `superseded_by` | list[str] | связи жизненного цикла версий (зарезервировано) |
| `source` | str | имя исходного файла |
| `source_uri` | str | путь/URL источника |
| `char_start` / `char_end` | int | смещения в нормализованном тексте источника — спан фрагмента |
| `page_start` / `page_end` | int | страница(ы) источника, если формат их даёт (PDF/скан) |
| `bbox` | list[float] | `[x0, y0, x1, y1]`, если доступно |
| `span_id` | str | стабильный id спана источника |
| `kind` | str | `text` или `table` |
| `type` | str | тип структурного элемента |
| `numbering` | str | собственный номер фрагмента |
| `block` | str | `main` или `amendment` |
| `depth` | int | глубина в иерархии |
| `order` | int | позиция в порядке чтения документа (для реконструкции) |
| `parent_id` | str | идентификатор родительского узла |
| `parent_text` | str | текст родителя |
| `child_ids` | list[str] | идентификаторы дочерних узлов |
| `prev_id` | str | предыдущий фрагмент по порядку чтения |
| `next_id` | str | следующий фрагмент по порядку чтения |
| `breadcrumb` | str | путь от корня (раздел / пункт) |
| `tags` | list[str] | теги |
| `metadata` | dict | открытый слот для доменных атрибутов |
| `table_html` | str | HTML-представление таблицы (для `kind=table`) |
| `references` | list[DocumentRef] | исходящие ссылки на другие документы/пункты (см. ниже) |
| `payload_schema_version` | int | версия схемы payload (сейчас `2`) |
| `parser_version` | str | версия парсера, создавшего узел |
| `embedding_meta` | dict | векторизатор сохранённого вектора (`{model, dim, metric, normalized}`) — задел под мульти-вектор/мультисёрч |
| `uploaded_at` | str | метка времени ISO 8601 UTC, выставляется один раз на вызов ingest (одно и то же значение у всех узлов этой загрузки) |
| `text` | str | текст фрагмента |

Поля сверх исходного ядра — общего назначения (доменно-нейтральные): идентичность/ключи поиска для
кросс-сервисных связей, привязка к источнику (`char_*` / `page_*` / `bbox` / `span_id`), чтобы любой
потребитель мог сослаться на точное место в источнике, и открытые слоты `external_ids` / `metadata`,
куда доменный сервис (например, MSI-TSIM) кладёт свои данные, а DVD их не интерпретирует. У всех есть
безопасные значения по умолчанию, поэтому точки, записанные до этой схемы, продолжают валидироваться.

Каждый элемент `references` — это `DocumentRef`: `raw` (дословный текст ссылки),
`target_name` / `target_numbering` (обозначение и пункт цели), `scope` (`internal`/`external`),
резолв `target_doc_id` / `target_version` / `target_node_id` (id точки Qdrant конкретного пункта)
и `resolved`.

Payload-индексы создаются по полям `doc_id`, `name`, `version`, `version_id`, `kind`, `type`,
`block`, `parent_id`, `content_hash`, `tags`, `numbering`, `references[].target_name`, `doc_type`,
`corpus`, `lang`, `lookup_keys`, `span_id` и `order`.

## Хранилища

- Qdrant: отдельная коллекция под каждое векторное пространство (базовое имя по умолчанию
  `documents`, неймспейсится в, напр., `documents__giga_embeddings_instruct_2048`), вектор
  размерности `vector_size`, метрика косинус. Тексты и таблицы лежат в одной коллекции и различаются
  полем `kind`.
- Redis: статусы задач (`dvd:job:{job_id}`, с TTL, без неймспейса) и скоупленный по коллекции реестр
  под `{registry_prefix}` (по умолчанию `dvd:{effective_collection}`): реестр хэшей (`…:hash:{hash}`),
  версий (`…:versions:{name}`), множество всех имён документов (`…:names`, для сопоставления ссылок)
  и очереди отложенных ссылок (`…:pending_ref:{normalized_name}`).
- Выученные паттерны ссылок хранятся в отдельной долговечной коллекции Qdrant (по умолчанию
  `ref_patterns`, dummy-векторы размерности 1 как key/value-хранилище) — они переживают сброс
  Redis; seed-паттерны закоммичены в `reference_patterns.py`.
