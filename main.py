"""
Baixa áudio (yt-dlp), miniatura e metadados do YouTube, envia para R2, atualiza feed.xml.
URLs públicas vêm de R2_PUBLIC_URL (sem barra final).
Fonte do vídeo: variável de ambiente YOUTUBE_URL (no Actions vem do workflow_dispatch).
Cookies opcionais: arquivo cookies.txt na raiz do projeto (gerado no CI a partir do secret YOUTUBE_COOKIES)
ou caminho em YOUTUBE_COOKIES_PATH; repassado ao yt-dlp como --cookies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests
from botocore.config import Config

ROOT = Path(__file__).resolve().parent
FEED_PATH = ROOT / "feed.xml"
ENV_FILE = ROOT / ".env"

YT_EXTRACTOR_ARGS = "youtube:client=ios,tv,web"
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"


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


def _cookies_cli() -> list[str]:
    """Netscape cookies para o YouTube (--cookies), se o arquivo existir."""
    raw = os.environ.get("YOUTUBE_COOKIES_PATH", "").strip()
    path = Path(raw) if raw else (ROOT / "cookies.txt")
    try:
        if path.is_file() and path.stat().st_size > 0:
            return ["--cookies", str(path.resolve())]
    except OSError:
        pass
    return []


def _yt_dlp_base_cmd() -> list[str]:
    return [
        "yt-dlp",
        *_cookies_cli(),
        "--no-playlist",
        "--extractor-args",
        YT_EXTRACTOR_ARGS,
    ]


def fetch_youtube_metadata(source_url: str) -> dict:
    cmd = _yt_dlp_base_cmd() + ["--dump-single-json", source_url]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(proc.stdout)


def ordered_thumbnail_urls(info: dict) -> list[str]:
    """URLs da melhor para piores miniaturas (deduplicadas)."""
    thumbs = list(info.get("thumbnails") or [])

    def area(t: dict) -> int:
        return (t.get("width") or 0) * (t.get("height") or 0)

    thumbs.sort(key=area, reverse=True)
    ordered: list[str] = []
    seen: set[str] = set()
    top = info.get("thumbnail")
    if isinstance(top, str) and top.startswith("http") and top not in seen:
        ordered.append(top)
        seen.add(top)
    for t in thumbs:
        u = t.get("url")
        if isinstance(u, str) and u.startswith("http") and u not in seen:
            ordered.append(u)
            seen.add(u)

    def prefer_raster(u: str) -> int:
        ul = u.lower()
        if ".jpg" in ul or ".jpeg" in ul or "image%2fjpeg" in ul:
            return 3
        if ".png" in ul or "image%2fpng" in ul:
            return 2
        if "webp" in ul:
            return 0
        return 1

    ordered.sort(key=prefer_raster, reverse=True)
    return ordered


def download_thumbnail_bytes(urls: list[str]) -> tuple[bytes, str, str]:
    """
    Baixa a miniatura. Retorna (corpo, extensão sem ponto, content-type S3).
    Falha se nenhuma URL funcionar.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    last_err: str | None = None
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=120)
            r.raise_for_status()
            data = r.content
            if len(data) < 256:
                last_err = "resposta muito pequena"
                continue
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            ext, s3_ct = _guess_image_format(url, ctype, data[:12])
            return data, ext, s3_ct
        except OSError as e:
            last_err = str(e)
        except requests.RequestException as e:
            last_err = str(e)
    print(
        f"Não foi possível baixar miniatura do YouTube ({last_err}). "
        "O feed exige itunes:image; abortando.",
        file=sys.stderr,
    )
    sys.exit(1)


def _guess_image_format(url: str, content_type: str, head: bytes) -> tuple[str, str]:
    if "jpeg" in content_type or "jpg" in content_type:
        return "jpg", "image/jpeg"
    if "png" in content_type:
        return "png", "image/png"
    if "webp" in content_type:
        return "webp", "image/webp"
    if "gif" in content_type:
        return "gif", "image/gif"
    u = url.lower()
    if ".jpg" in u or ".jpeg" in u:
        return "jpg", "image/jpeg"
    if ".png" in u:
        return "png", "image/png"
    if ".webp" in u:
        return "webp", "image/webp"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if head.startswith(b"RIFF") and b"WEBP" in head[:12]:
        return "webp", "image/webp"
    return "jpg", "image/jpeg"


def download_audio(source_url: str, work: Path) -> tuple[Path, str]:
    pattern = str(work / "%(id)s.%(ext)s")
    cmd = _yt_dlp_base_cmd() + [
        "-f",
        "ba/b",
        "-x",
        "--audio-format",
        "mp3",
        "-o",
        pattern,
        source_url,
    ]
    subprocess.run(cmd, check=True)
    audio_files = [
        p
        for p in work.iterdir()
        if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".opus", ".webm", ".ogg"}
    ]
    if len(audio_files) != 1:
        print(f"Esperado 1 arquivo de áudio após yt-dlp, encontrados: {audio_files}", file=sys.stderr)
        sys.exit(1)
    audio = audio_files[0]
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
    itunes_image_href: str,
) -> None:
    item = ET.SubElement(channel, "item")
    t = ET.SubElement(item, "title")
    t.text = title
    ET.SubElement(item, f"{{{ITUNES_NS}}}image", {"href": itunes_image_href})
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
    ET.register_namespace("itunes", ITUNES_NS)
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

    info = fetch_youtube_metadata(source)
    episode_title = (info.get("fulltitle") or info.get("title") or "").strip() or f"Episódio {guid}"
    thumb_urls = ordered_thumbnail_urls(info)
    if not thumb_urls:
        print("Metadados do YouTube não incluem miniatura; o feed exige itunes:image.", file=sys.stderr)
        sys.exit(1)

    client = r2_client()

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        thumb_bytes, thumb_ext, thumb_ct = download_thumbnail_bytes(thumb_urls)
        thumb_path = work / f"thumb.{thumb_ext}"
        thumb_path.write_bytes(thumb_bytes)
        audio_path, _stem = download_audio(source, work)
        size = audio_path.stat().st_size
        ext = audio_path.suffix.lower().lstrip(".") or "mp3"
        audio_key = f"episodes/{guid}.{ext}"
        thumb_key = f"episodes/{guid}-thumb.{thumb_ext}"
        a_ct = "audio/mpeg" if ext == "mp3" else "application/octet-stream"
        upload_file(client, bucket, audio_key, audio_path, a_ct)
        upload_file(client, bucket, thumb_key, thumb_path, thumb_ct)

    public_audio = f"{base}/{audio_key}"
    public_thumb = f"{base}/{thumb_key}"
    append_item(
        channel,
        guid=guid,
        title=episode_title,
        enclosure_url=public_audio,
        length_bytes=size,
        itunes_image_href=public_thumb,
    )
    write_feed(tree)
    upload_feed_to_r2(client, bucket)
    print(f"Publicado: {public_audio} (capa: {public_thumb})")


if __name__ == "__main__":
    main()
