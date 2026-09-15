"""Read-only dev parsing snapshot; credentials remain in process memory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/diagnostics/dev-parsing-2026-09-14"
BASE = "http://10.32.11.90:31002"


def save(name, data):
    (OUT / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", type=Path, default=OUT)
    ap.add_argument("--probe-doc-id", default="a7e1c951-c663-4366-aec8-6c3ebffbef29")
    ap.add_argument(
        "--probe-patterns",
        nargs="*",
        default=["1.1", "3.1", "4.1", "6.1", "8.1", "10.1"],
    )
    ap.add_argument("--documents", action="store_true")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--probes", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--eligibility", action="store_true")
    args = ap.parse_args()
    OUT = args.output_dir.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = dotenv_values(ROOT.parent / "gMART/env/.env.benchmark.local")
    with httpx.Client(timeout=60, trust_env=False) as c:
        r = c.post(
            cfg["KEYCLOAK_TOKEN_URL"],
            data={
                "grant_type": "client_credentials",
                "client_id": cfg["KEYCLOAK_CLIENT_ID"],
                "client_secret": cfg["KEYCLOAK_CLIENT_SECRET"],
            },
        )
        r.raise_for_status()
        c.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        if args.status:
            for name, path in [
                ("recent-jobs", "/documents/jobs/recent?limit=100"),
                ("active-jobs", "/documents/jobs/active"),
                ("queue", "/documents/jobs/queue"),
            ]:
                r = c.get(BASE + path)
                r.raise_for_status()
                data = r.json()
                save(name + ".json", data)
                if name == "recent-jobs":
                    print(
                        json.dumps(
                            [
                                {
                                    k: j.get(k)
                                    for k in [
                                        "job_id",
                                        "name",
                                        "status",
                                        "stage",
                                        "phase",
                                        "progress",
                                        "progress_total",
                                        "error",
                                    ]
                                }
                                for j in data["jobs"][:3]
                            ],
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            return
        if args.eligibility:
            r = c.post(BASE + "/documents/reparse", params={"dry_run": "true"})
            r.raise_for_status()
            save("eligibility.json", r.json())
            print(json.dumps(r.json(), ensure_ascii=False), flush=True)
            return
        if args.probes:
            for num in args.probe_patterns:
                r = c.post(
                    BASE + "/search/structure",
                    json={
                        "pattern": num,
                        "doc_id": args.probe_doc_id,
                        "include_children": True,
                        "context_height": 0,
                        "limit": 100,
                    },
                )
                save("search-" + num + ".json", r.json())
                print(
                    num,
                    r.status_code,
                    "hits",
                    len(r.json().get("hits", [])),
                    flush=True,
                )
            r = c.post(
                BASE + "/search/structure",
                json={
                    "pattern": "31.6.1",
                    "document_names": ["СП 308.1325800.2017"],
                    "include_children": True,
                    "context_height": 1,
                    "limit": 100,
                },
            )
            save("search-sp308-31.6.1.json", r.json())
            print(
                "31.6.1",
                r.status_code,
                "hits",
                len(r.json().get("hits", [])),
                flush=True,
            )
            return
        if not args.documents:
            for name, path in [
                ("catalog", "/library/documents"),
                ("recent-jobs", "/documents/jobs/recent?limit=100"),
                ("active-jobs", "/documents/jobs/active"),
                ("openapi", "/openapi.json"),
            ]:
                r = c.get(BASE + path)
                print(name, r.status_code, flush=True)
                if r.is_success:
                    save(name + ".json", r.json())
            r = c.get(BASE + "/system/settings")
            if r.is_success:
                data = r.json()

                # Store only parsing-related settings, never auth or infrastructure values.
                def pick(obj):
                    result = []
                    if isinstance(obj, dict):
                        identity = str(
                            obj.get(
                                "field",
                                obj.get("env", obj.get("key", obj.get("name", ""))),
                            )
                        ).lower()
                        if any(
                            s in identity
                            for s in ["partition", "parser", "window", "merge", "model"]
                        ):
                            result.append(obj)
                        else:
                            for k, v in obj.items():
                                if any(
                                    s in k.lower()
                                    for s in [
                                        "partition",
                                        "parser",
                                        "window",
                                        "merge",
                                        "model",
                                    ]
                                ):
                                    result.append({k: v})
                                elif isinstance(v, (dict, list)):
                                    result.extend(pick(v))
                    elif isinstance(obj, list):
                        for v in obj:
                            result.extend(pick(v))
                    return result

                save("parsing-settings.json", pick(data))
            return
        catalog = json.loads((OUT / "catalog.json").read_text(encoding="utf-8"))
        for d in catalog["documents"]:
            key = d["doc_id"]
            if args.ids and key not in args.ids:
                continue
            r = c.get(BASE + "/library/documents/" + key)
            r.raise_for_status()
            detail = r.json()
            save(key + ".json", detail)
            url = (
                d.get("source_file_url")
                or "/documents/" + quote(d["name"], safe="") + "/source"
            )
            if url.startswith("/"):
                url = BASE + url
            if not url.startswith(BASE + "/"):
                raise ValueError("Unexpected source host")
            r = c.get(url, params={"version": d["version"]})
            if r.is_success:
                (OUT / (key + ".docx")).write_bytes(r.content)
            print(
                d["name"],
                d["version"],
                len(detail["fragments"]),
                "source",
                r.status_code,
                flush=True,
            )


if __name__ == "__main__":
    main()
