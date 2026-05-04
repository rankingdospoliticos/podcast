"""
Baixa áudio (yt-dlp), envia para R2, atualiza feed.xml sem duplicar <item> pelo mesmo guid.
URLs públicas vêm de R2_PUBLIC_URL (sem barra final).
Fonte do vídeo: variável de ambiente YOUTUBE_URL (no Actions vem do workflow_dispatch).
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent
FEED_PATH = ROOT / "feed.xml"
ENV_FILE = ROOT / ".env"


def load_dotenv_file() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"Variável obrigatória ausente: {name}", file=sys.stderr)
        sys.exit(1)
    return v


def stable_guid(source_url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", source_url)
    if m:
        return m.group(1)
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:32]


def r2_client():
    account_id = require_env("R2_ACCOUNT_ID")
    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=require_env("R2_ACCESS_KEY"),
        aws_secret_access_key=require_env("R2_SECRET_KEY"),
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def parse_feed() -> tuple[ET.ElementTree, ET.Element]:
    tree = ET.parse(FEED_PATH)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        print("feed.xml sem <channel>", file=sys.stderr)
        sys.exit(1)
    return tree, channel


def existing_guids(channel: ET.Element) -> set[str]:
    out: set[str] = set()
    for item in channel.findall("item"):
        g = item.find("guid")
        if g is not None and g.text:
            out.add(g.text.strip())
    return out


def sync_public_urls(channel: ET.Element, base: str) -> None:
    """Garante <link> e atom:self alinhados à base pública estável."""
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    link_el = channel.find("link")
    if link_el is not None:
        link_el.text = base
    atom_links = channel.findall("atom:link", ns)
    for al in atom_links:
        if al.get("rel") == "self":
            al.set("href", f"{base}/feed.xml")
            break
    else:
        ET.SubElement(
            channel,
            "{http://www.w3.org/2005/Atom}link",
            {
                "href": f"{base}/feed.xml",
                "rel": "self",
                "type": "application/rss+xml",
            },
        )


def format_pub_date() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def download_audio(source_url: str, work: Path) -> tuple[Path, str]:
    pattern = str(work / "%(id)s.%(ext)s")
    subprocess.run(
        [
            "yt-dlp",
            "-f",
            "ba/b",
            "-x",
            "--audio-format",
            "mp3",
            "--no-playlist",
            "--extractor-args",
            "youtube:client=android",
            "-o",
            pattern,
            source_url,
        ],
        check=True,
    )
    files = [p for p in work.iterdir() if p.is_file()]
    if len(files) != 1:
        print(f"Esperado 1 arquivo após yt-dlp, encontrados: {files}", file=sys.stderr)
        sys.exit(1)
    audio = files[0]
    return audio, audio.stem


def upload_file(client, bucket: str, key: str, path: Path, content_type: str) -> None:
    client.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type})


def append_item(
    channel: ET.Element,
    *,
    guid: str,
    title: str,
    enclosure_url: str,
    length_bytes: int,
) -> None:
    item = ET.SubElement(channel, "item")
    t = ET.SubElement(item, "title")
    t.text = title
    g = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    g.text = guid
    pd = ET.SubElement(item, "pubDate")
    pd.text = format_pub_date()
    ET.SubElement(
        item,
        "enclosure",
        {
            "url": enclosure_url,
            "length": str(length_bytes),
            "type": "audio/mpeg",
        },
    )


def write_feed(tree: ET.ElementTree) -> None:
    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    ET.indent(tree, space="  ")
    tree.write(FEED_PATH, encoding="utf-8", xml_declaration=True)


def upload_feed_to_r2(client, bucket: str) -> None:
    """Publica feed.xml no R2 (objeto `feed.xml` na raiz do bucket)."""
    client.upload_file(
        str(FEED_PATH),
        bucket,
        "feed.xml",
        ExtraArgs={"ContentType": "application/rss+xml; charset=utf-8"},
    )


def main() -> None:
    load_dotenv_file()
    bucket = require_env("R2_BUCKET_NAME")
    base = require_env("R2_PUBLIC_URL").rstrip("/")
    source = require_env("YOUTUBE_URL")
    guid = stable_guid(source)

    tree, channel = parse_feed()
    sync_public_urls(channel, base)

    if guid in existing_guids(channel):
        print(f"Episódio já existe no feed (guid={guid}). Sincronizando feed no R2.")
        write_feed(tree)
        client = r2_client()
        upload_feed_to_r2(client, bucket)
        return

    client = r2_client()

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        audio_path, stem = download_audio(source, work)
        size = audio_path.stat().st_size
        ext = audio_path.suffix.lower().lstrip(".") or "mp3"
        object_key = f"episodes/{guid}.{ext}"
        ctype = "audio/mpeg" if ext == "mp3" else "application/octet-stream"
        upload_file(client, bucket, object_key, audio_path, ctype)

    public_audio = f"{base}/{object_key}"
    title = f"Episódio {stem}"
    append_item(
        channel,
        guid=guid,
        title=title,
        enclosure_url=public_audio,
        length_bytes=size,
    )
    write_feed(tree)
    upload_feed_to_r2(client, bucket)
    print(f"Publicado: {public_audio}")


if __name__ == "__main__":
    main()
