#!/usr/bin/env python3
"""
RabbitMQ backup & restore (duplicate‑friendly) + offline de‑duplication tool
===========================================================================
* **export** – *non‑destructive*: pulls messages with `ack_requeue_true`, so the
  source queue залишається неушкодженою. У файлі **можуть бути дублікати**.
* **dedup**  – новий режим. Проганяє *один* `.jsonl.gz` файл і прибирає всі
  дублікати (визначає по `message_id`, фолбек SHA‑256 тіла). Можна або
  перезаписати файл in‑place (`--inplace`), або створити `*.dedup.jsonl.gz`.
* **import** – як у patch 6 (passive‑safe).
Changes in **2025‑06‑09 patch 7 (offline de‑duplication)**
---------------------------------------------------------
* додано CLI‑підкоманду `dedup`; 
* `_fingerprint()` повернено, бо потрібен для dedup; 
* до логів додано підсумкову статистику `read / kept / removed`.
"""
from __future__ import annotations
import argparse
import base64
import gzip
import hashlib
import json
import logging
import os
import pathlib
import queue
import shutil
import threading
import urllib.parse
from typing import Dict, Iterator, List, Set
import pika
import requests
from requests.auth import HTTPBasicAuth
from tqdm import tqdm

# --------------------------- constants -------------------------------------
ALLOWED_PROPS = {
    "content_type",
    "content_encoding",
    "delivery_mode",
    "priority",
    "correlation_id",
    "reply_to",
    "expiration",
    "message_id",
    "timestamp",
    "type",
    "user_id",
    "app_id",
    "cluster_id",
}
MAX_PAGE_SIZE = 500      # RabbitMQ REST API upper limit
DEFAULT_BATCH = 1        # with ack_requeue_true we pull 1 msg / request
SUFFIX = ".jsonl.gz"     # export file name suffix
RESERVED_PREFIXES: tuple[str, ...] = (
    "amq.",                # RabbitMQ internal
    "reply_",              # RPC reply queues
    "rabbitmq.recovery.",  # quorum helper
)

# --------------------------- helper functions -----------------------------
def new_session(user: str, password: str) -> requests.Session:
    s = requests.Session()
    s.auth = HTTPBasicAuth(user, password)
    s.headers.update({"Accept-Encoding": "gzip"})
    s.timeout = (15, 300)
    return s

def props_from(obj: dict) -> pika.BasicProperties:
    pr = {k: v for k, v in (obj.get("properties") or {}).items() if k in ALLOWED_PROPS}
    return pika.BasicProperties(headers=obj.get("headers"), **pr)

def _fingerprint(msg: dict) -> str:
    """Uniqueness fingerprint: message_id if present else SHA256(payload)."""
    mid = (msg.get("properties") or {}).get("message_id")
    if mid:
        return f"id:{mid}"
    payload_field = "payload" if "payload" in msg else "payload_bytes"
    body = (
        msg[payload_field].encode()
        if payload_field == "payload"
        else base64.b64decode(msg[payload_field])
    )
    return "sha:" + hashlib.sha256(body).hexdigest()

# --------------------------- pagination ------------------------------------
def iter_queue_pages(sess: requests.Session, api_root: str) -> Iterator[List[dict]]:
    page = 1
    while True:
        url = (
            f"{api_root}/queues?page={page}&page_size={MAX_PAGE_SIZE}"
            "&pagination=true&disable_stats=true&enable_queue_totals=true"
        )
        r = sess.get(url)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", data)
        if not items:
            break
        yield items
        if len(items) < MAX_PAGE_SIZE:
            break
        page += 1

def list_queues(sess: requests.Session, api_root: str) -> List[dict]:
    queues: List[dict] = []
    for page in iter_queue_pages(sess, api_root):
        queues.extend(page)
    return queues

# --------------------------- export logic ---------------------------------
def export_queue(
    sess: requests.Session,
    api_root: str,
    qinfo: dict,
    out_dir: pathlib.Path,
    _batch: int,  # kept for signature but ignored (always 1)
) -> None:
    vhost: str = qinfo["vhost"]
    qname: str = qinfo["name"]
    expected: int = qinfo["messages"]
    
    # Якщо черга порожня, пропускаємо
    if expected == 0:
        logging.info("⏭ %s/%s — черга порожня, пропущено", vhost, qname)
        return
    
    v_enc = urllib.parse.quote(vhost, safe="")
    q_enc = urllib.parse.quote(qname, safe="")
    url = f"{api_root}/queues/{v_enc}/{q_enc}/get"
    
    # ВИПРАВЛЕННЯ: count повинен бути 1, а не більше при ack_requeue_true
    payload = {
        "count": 1,                     # ВАЖЛИВО: завжди 1 для ack_requeue_true
        "ackmode": "ack_requeue_true",  # queue stays intact
        "encoding": "auto",             # text base64
        #"truncate": 0,                  # не обрізати повідомлення
    }
    
    out_path = out_dir / v_enc / f"{q_enc}{SUFFIX}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    saved = 0
    consecutive_empty = 0  # лічильник порожніх відповідей
    max_empty_attempts = 3  # максимум спроб отримати порожню відповідь
    
    bar = tqdm(total=expected, desc=f"{vhost}/{qname}", unit="msg", leave=False)
    
    with gzip.open(out_path, "wt") as gz:
        while saved < expected:
            try:
                r = sess.post(url, json=payload)
                r.raise_for_status()
                msgs = r.json()
                
                # ВИПРАВЛЕННЯ: перевірка на порожню відповідь
                if not msgs or len(msgs) == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= max_empty_attempts:
                        logging.warning("⚠️ %s/%s — отримано %d порожніх відповідей поспіль, зупиняємо", 
                                      vhost, qname, consecutive_empty)
                        break
                    continue
                
                consecutive_empty = 0  # скидаємо лічильник
                
                # ВИПРАВЛЕННЯ: додаткова перевірка на валідність повідомлень
                for m in msgs:
                    if not m or not isinstance(m, dict):
                        logging.warning("⚠️ %s/%s — отримано недійсне повідомлення: %r", vhost, qname, m)
                        continue
                    
                    # Перевіряємо наявність payload
                    if "payload" not in m and "payload_bytes" not in m:
                        logging.warning("⚠️ %s/%s — повідомлення без payload: %r", vhost, qname, m)
                        continue
                    
                    gz.write(json.dumps(m) + "\n")
                    saved += 1
                    bar.update(1)
                    
            except requests.exceptions.RequestException as e:
                logging.error("❌ %s/%s — помилка HTTP запиту: %s", vhost, qname, e)
                break
            except json.JSONDecodeError as e:
                logging.error("❌ %s/%s — помилка парсингу JSON: %s", vhost, qname, e)
                break
            except Exception as e:
                logging.error("❌ %s/%s — неочікувана помилка: %s", vhost, qname, e)
                break
    
    bar.close()
    
    if saved > 0:
        logging.info("✓ %s/%s — %d msg exported (duplicates possible)", vhost, qname, saved)
    else:
        logging.warning("⚠️ %s/%s — не вдалося експортувати жодного повідомлення", vhost, qname)
        # Видаляємо порожній файл
        if out_path.exists() and out_path.stat().st_size == 0:
            out_path.unlink()

# threaded export wrapper
def export_all(args: argparse.Namespace) -> None:
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    sess = new_session(args.user, args.password)
    api_root = f"http://{args.host}:{args.http_port}/api"
    
    # Перевірка підключення
    try:
        r = sess.get(f"{api_root}/overview")
        r.raise_for_status()
        logging.info("✓ Підключення до RabbitMQ Management API успішне")
    except Exception as e:
        logging.error("❌ Не вдалося підключитися до RabbitMQ Management API: %s", e)
        return
    
    queues = list_queues(sess, api_root)
    if args.vhost:
        queues = [q for q in queues if q["vhost"] in args.vhost]
    
    # Фільтруємо порожні черги для логування
    non_empty_queues = [q for q in queues if q.get("messages", 0) > 0]
    empty_queues = [q for q in queues if q.get("messages", 0) == 0]
    
    if empty_queues:
        logging.info("📋 Знайдено %d порожніх черг (будуть пропущені)", len(empty_queues))
    
    if not non_empty_queues:
        logging.warning("⚠️ Не знайдено черг з повідомленнями для експорту")
        return
    
    logging.info("📋 Знайдено %d черг з повідомленнями для експорту", len(non_empty_queues))
    
    work: "queue.Queue[dict]" = queue.Queue()
    for q in non_empty_queues:  # Додаємо тільки непорожні черги
        work.put(q)
    
    def worker() -> None:
        while True:
            try:
                qi = work.get_nowait()
            except queue.Empty:
                break
            try:
                export_queue(sess, api_root, qi, out_dir, args.batch)
            except Exception as e:
                logging.error("❌ Помилка при експорті черги %s/%s: %s", 
                            qi.get("vhost", "?"), qi.get("name", "?"), e)
            finally:
                work.task_done()
    
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.parallel)]
    for t in threads:
        t.start()
    
    work.join()
    logging.info("✅ Export finished → %s", out_dir)

# --------------------------- dedup logic ----------------------------------
def dedup_file(path: pathlib.Path, inplace: bool) -> None:
    if not path.exists():
        logging.error("File %s not found", path)
        return
    
    tmp_path = path.with_suffix(".dedup.jsonl.gz") if not inplace else path.with_suffix(".tmp")
    total_read = 0
    kept = 0
    seen: Set[str] = set()
    
    bar = tqdm(desc=f"dedup {path.name}", unit="msg", leave=False)
    
    with gzip.open(path, "rt") as src, gzip.open(tmp_path, "wt") as dst:
        for line in src:
            total_read += 1
            try:
                obj = json.loads(line)
                fp = _fingerprint(obj)
                if fp in seen:
                    continue
                seen.add(fp)
                dst.write(line)
                kept += 1
                bar.update()
            except json.JSONDecodeError as e:
                logging.warning("⚠️ Пропускаємо недійсний JSON: %s", e)
                continue
    
    bar.close()
    
    if inplace:
        shutil.move(tmp_path, path)
    
    logging.info("✓ %s: read %d, kept %d, removed %d", path.name, total_read, kept, total_read - kept)

# --------------------------- import logic (unchanged from patch‑6) --------
def decode_legacy_vhost(raw: str, legacy: bool) -> str:
    if raw == "%2F":
        return "/"
    if legacy and raw == "_":
        return "/"
    return urllib.parse.unquote(raw)

def decode_legacy_queue(raw: str, legacy: bool) -> str:
    if legacy and raw == "_":
        return "_"
    return urllib.parse.unquote(raw)

def import_all(args: argparse.Namespace) -> None:
    in_dir = pathlib.Path(args.indir or args.out)
    if not in_dir.exists():
        logging.error("Input directory '%s' not found.", in_dir)
        return
    
    conn_cache: Dict[str, pika.BlockingConnection] = {}
    
    def channel_for(vhost: str):
        if vhost not in conn_cache:
            creds = pika.PlainCredentials(args.user, args.password)
            conn_cache[vhost] = pika.BlockingConnection(
                pika.ConnectionParameters(args.host, args.amqp_port, vhost, creds)
            )
        return conn_cache[vhost].channel()
    
    for vdir in in_dir.iterdir():
        if not vdir.is_dir():
            continue
        
        vhost = decode_legacy_vhost(vdir.name, args.legacy_sanitize)
        if args.vhost and vhost not in args.vhost:
            continue
        
        for f in vdir.glob(f"*{SUFFIX}"):
            raw_name = f.name[: -len(SUFFIX)]
            qname = decode_legacy_queue(raw_name, args.legacy_sanitize)
            
            if qname.startswith(RESERVED_PREFIXES):
                logging.info("Skipping reserved/internal queue %s", qname)
                continue
            
            ch = channel_for(vhost)
            if args.declare_queue:
                try:
                    ch.queue_declare(queue=qname, passive=True)
                except pika.exceptions.ChannelClosedByBroker as e:
                    if e.reply_code == 404:
                        ch = channel_for(vhost)
                        ch.queue_declare(queue=qname, durable=True, passive=False)
                    else:
                        ch = channel_for(vhost)
            
            total = 0
            bar = tqdm(desc=f"restore {vhost}/{qname}", unit="msg", leave=False)
            
            with gzip.open(f, "rt") as gz:
                for line in gz:
                    try:
                        obj = json.loads(line)
                        payload_field = "payload" if "payload" in obj else "payload_bytes"
                        
                        if payload_field == "payload":
                            # payload - це рядок, який потрібно закодувати
                            body = obj[payload_field].encode()
                        else:
                            # payload_bytes - це base64 encoded рядок
                            body = base64.b64decode(obj[payload_field])
                        
                        ch.basic_publish("", qname, body, props_from(obj))
                        total += 1
                        bar.update()
                    except (json.JSONDecodeError, KeyError, base64.binascii.Error) as e:
                        logging.warning("⚠️ Пропускаємо недійсне повідомлення: %s", e)
                        continue
            
            bar.close()
            logging.info("✓ %s/%s — %d msg restored", vhost, qname, total)
    
    for conn in conn_cache.values():
        conn.close()
    
    logging.info("✅ Import finished.")

# --------------------------- CLI -----------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="RabbitMQ non‑destructive backup/restore via HTTP /get (pagination)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="mode", required=True)
    
    # Export command
    exp = sub.add_parser("export", help="Dump all queues (non‑destructive)")
    exp.add_argument("--host", default="localhost")
    exp.add_argument("--http-port", type=int, default=15672)
    exp.add_argument("-u", "--user", required=True)
    exp.add_argument("-p", "--password", required=True)
    exp.add_argument("-o", "--out", default="rmq_http_dump")
    exp.add_argument("--vhost", action="append", help="Export only these vhosts")
    exp.add_argument("--parallel", type=int, default=os.cpu_count() or 8)
    exp.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="/get batch size")
    
    # Import command
    imp = sub.add_parser("import", help="Restore from dump")
    imp.add_argument("--host", default="localhost")
    imp.add_argument("--amqp-port", type=int, default=5672)
    imp.add_argument("-u", "--user", required=True)
    imp.add_argument("-p", "--password", required=True)
    imp.add_argument("--indir", help="Directory with dump (defaults to --out path)")
    imp.add_argument("-o", "--out", default="rmq_http_dump", help="Used if --indir not set")
    imp.add_argument("--vhost", action="append", help="Import only these vhosts")
    imp.add_argument("--declare-queue", action="store_true", help="Create queue if missing")
    imp.add_argument("--legacy-sanitize", action="store_true", help="Decode old '_' names")
    
    # Dedup command
    dedup = sub.add_parser("dedup", help="Remove duplicates from exported file")
    dedup.add_argument("file", help="Path to .jsonl.gz file to deduplicate")
    dedup.add_argument("--inplace", action="store_true", 
                      help="Modify file in-place instead of creating .dedup version")
    
    return p

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    if args.mode == "export":
        export_all(args)
    elif args.mode == "import":
        import_all(args)
    elif args.mode == "dedup":
        dedup_file(pathlib.Path(args.file), args.inplace)

if __name__ == "__main__":
    main()
