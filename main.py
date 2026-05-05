"""
Lê a playlist YouTube (secret YOUTUBE_PLAYLIST_URL), processa vídeos ainda não
presentes no feed.xml, baixa áudio + miniatura (yt-dlp), envia ao R2 e atualiza o RSS.
Ignora lives/agendadas e VOD com idade inferior a MIN_VIDEO_AGE_SECONDS (padrão 3h).
URLs públicas vêm de R2_PUBLIC_URL (sem barra final).
Autenticação YouTube: ficheiro Netscape — variável **YOUTUBE_COOKIES_PATH** ou ``cookies.txt``
na raiz do projecto; repassados ao yt-dlp como ``--cookies <caminho>``. Opcionalmente
``python main.py --cookies-from-browser ...`` (prioridade sobre o ficheiro).
Com qualquer cookie activo, usa-se por defeito ``youtube:player_client=mweb,web`` (salvo YTDLP_EXTRACTOR_ARGS).
O yt-dlp usa ``--user-agent`` fixo (Safari iOS) e já não passa ``--force-ipv4``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
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

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
DEFAULT_MIN_VIDEO_AGE_SECONDS = 10800  # 3 horas
# live_status do yt-dlp: ainda não é VOD estável para o feed
SKIP_LIVE_STATUSES = frozenset(
    {
        "is_live",
        "is_upcoming",
        "is_post_live",
    }
)

# Metadados oficiais do programa (Spotify / iTunes)
SHOW_TITLE = "Ranking Podcast"
SHOW_DESCRIPTION = "Podcast oficial do Ranking dos Políticos."
SHOW_AUTHOR = "Ranking dos Políticos"
OWNER_NAME = "Ranking dos Políticos"
OWNER_EMAIL = "comunicacao@politicos.org.br"
ITUNES_CATEGORY = "Government"

YT_DLP_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
# Definido em main() via CLI: --cookies-from-browser (prioridade sobre cookies.txt)
_COOKIES_FROM_BROWSER: str | None = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Podcast: playlist YouTube → R2 + feed.xml")
    p.add_argument(
        "--cookies-from-browser",
        choices=(
            "brave",
            "chrome",
            "chromium",
            "edge",
            "firefox",
            "opera",
            "safari",
            "vivaldi",
            "whale",
        ),
        default=None,
        help="Repassa --cookies-from-browser ao yt-dlp (sessão local do navegador).",
    )
    return p.parse_args()


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


def youtube_video_id(source_url: str) -> str:
    """ID estável (11 caracteres no YouTube) para chaves de objeto no R2."""
    m = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", source_url)
    if m:
        return m.group(1)
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:32]


def youtube_watch_canonical(source_url: str) -> str | None:
    """URL canônica https://www.youtube.com/watch?v=ID para <guid> e <link>."""
    m = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", source_url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", source_url.strip()):
        return f"https://www.youtube.com/watch?v={source_url.strip()}"
    return None


def episode_guid_and_link(source_url: str) -> tuple[str, str]:
    """(guid, link) alinhados à URL do YouTube; fallback para URLs não-YouTube."""
    c = youtube_watch_canonical(source_url)
    if c:
        return c, c
    h = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:32]
    return h, source_url


def normalize_guid_from_feed(raw: str) -> str:
    """Compatível com episódios antigos cujo <guid> era só o ID de 11 caracteres."""
    raw = raw.strip()
    c = youtube_watch_canonical(raw)
    if c:
        return c
    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", raw):
        return f"https://www.youtube.com/watch?v={raw}"
    return raw


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
            out.add(normalize_guid_from_feed(g.text))
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


def sync_channel_metadata(channel: ET.Element, base: str) -> None:
    """Metadados oficiais do podcast no <channel> (Spotify / Apple Podcasts)."""
    def set_child(tag: str, text: str) -> None:
        el = channel.find(tag)
        if el is None:
            el = ET.SubElement(channel, tag)
        el.text = text

    set_child("title", SHOW_TITLE)
    set_child("description", SHOW_DESCRIPTION)
    set_child("link", base)

    au = channel.find(f"{{{ITUNES_NS}}}author")
    if au is None:
        au = ET.SubElement(channel, f"{{{ITUNES_NS}}}author")
    au.text = SHOW_AUTHOR

    ex = channel.find(f"{{{ITUNES_NS}}}explicit")
    if ex is None:
        ex = ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit")
    ex.text = "false"

    for o in list(channel.findall(f"{{{ITUNES_NS}}}owner")):
        channel.remove(o)
    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text = OWNER_NAME
    ET.SubElement(owner, f"{{{ITUNES_NS}}}email").text = OWNER_EMAIL

    for c in list(channel.findall(f"{{{ITUNES_NS}}}category")):
        channel.remove(c)
    ET.SubElement(channel, f"{{{ITUNES_NS}}}category", {"text": ITUNES_CATEGORY})


def format_pub_date() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def pub_date_from_video_info(info: dict) -> str:
    """RFC 822 / RSS pubDate em GMT a partir dos metadados do vídeo."""
    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts is not None:
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
        except (OSError, ValueError, OverflowError):
            pass
    ud = info.get("upload_date")
    if isinstance(ud, str) and len(ud) == 8 and ud.isdigit():
        try:
            dt = datetime(
                int(ud[:4]),
                int(ud[4:6]),
                int(ud[6:8]),
                12,
                0,
                0,
                tzinfo=timezone.utc,
            )
            return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
        except ValueError:
            pass
    return format_pub_date()


def _cookies_cli() -> list[str]:
    """Repassa sessão ao yt-dlp: --cookies-from-browser (CLI) ou --cookies <ficheiro>.

    Com ficheiro: usa YOUTUBE_COOKIES_PATH se definido, senão ROOT/cookies.txt.
    O caminho é sempre convertido para absoluto (``path.resolve()``), útil no Windows.
    """
    if _COOKIES_FROM_BROWSER:
        return ["--cookies-from-browser", _COOKIES_FROM_BROWSER]
    raw = os.environ.get("YOUTUBE_COOKIES_PATH", "").strip()
    path = Path(raw) if raw else (ROOT / "cookies.txt")
    try:
        if path.is_file() and path.stat().st_size > 0:
            return ["--cookies", str(path.resolve())]
    except OSError:
        pass
    return []


def _yt_extractor_args_cli() -> list[str]:
    """YTDLP_EXTRACTOR_ARGS tem prioridade; com cookies usa mweb,web."""
    override = os.environ.get("YTDLP_EXTRACTOR_ARGS", "").strip()
    if override:
        return ["--extractor-args", override]
    if _cookies_cli():
        return ["--extractor-args", "youtube:player_client=mweb,web"]
    return []


def _sanitize_yt_dlp_cmd_for_log(cmd: list[str]) -> str:
    """Comando para log: anonymiza caminhos de cookies (não imprime segredos)."""
    parts: list[str] = []
    i = 0
    while i < len(cmd):
        if cmd[i] == "--cookies" and i + 1 < len(cmd):
            parts.extend(["--cookies", "<cookies.txt>"])
            i += 2
        elif cmd[i] == "--cookies-from-browser" and i + 1 < len(cmd):
            parts.extend(["--cookies-from-browser", cmd[i + 1]])
            i += 2
        else:
            parts.append(cmd[i])
            i += 1
    return " ".join(parts)


def run_yt_dlp(cmd: list[str], *, capture_json: bool) -> subprocess.CompletedProcess[str]:
    """
    Executa yt-dlp; em falha imprime stderr/stdout no log (sem conteúdo de cookies).
    capture_json=True mantém stdout para parse JSON; download usa False.
    """
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        print(
            f"yt-dlp exit {proc.returncode}. Comando: {_sanitize_yt_dlp_cmd_for_log(cmd)}",
            file=sys.stderr,
        )
        if capture_json or (proc.stdout and proc.stdout.strip()):
            print("--- yt-dlp stdout ---", file=sys.stderr)
            print(proc.stdout or "", file=sys.stderr, end="")
            if proc.stdout and not proc.stdout.endswith("\n"):
                print(file=sys.stderr)
        print("--- yt-dlp stderr ---", file=sys.stderr)
        print(proc.stderr or "", file=sys.stderr, end="")
        if proc.stderr and not proc.stderr.endswith("\n"):
            print(file=sys.stderr)
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
        )
    return proc


def _yt_dlp_common() -> list[str]:
    """Prefixo yt-dlp + UA iPhone + rede + cookies + extractor-args (mweb,web com cookies). Sem --no-playlist."""
    return [
        "yt-dlp",
        "--user-agent",
        YT_DLP_USER_AGENT,
        "--sleep-requests",
        "5",
        *_cookies_cli(),
        *_yt_extractor_args_cli(),
    ]


def _yt_dlp_single_video_cmd() -> list[str]:
    """Comando base para um único vídeo."""
    return _yt_dlp_common() + ["--no-playlist"]


def fetch_playlist_entries(playlist_url: str) -> list[dict]:
    """Entradas da playlist (--flat-playlist), ordenadas por playlist_index crescente."""
    cmd = _yt_dlp_common() + ["--flat-playlist", "--dump-single-json", playlist_url]
    proc = run_yt_dlp(cmd, capture_json=True)
    data = json.loads(proc.stdout)
    raw = [e for e in (data.get("entries") or []) if isinstance(e, dict) and e.get("id")]

    def playlist_index_key(e: dict) -> int:
        i = e.get("playlist_index")
        if isinstance(i, int):
            return i
        return 999_999_999

    raw.sort(key=playlist_index_key)
    return raw


def playlist_watch_urls(entries: list[dict]) -> list[str]:
    return [f"https://www.youtube.com/watch?v={e['id']}" for e in entries]


def fetch_youtube_metadata(source_url: str) -> dict:
    cmd = _yt_dlp_single_video_cmd() + ["--dump-single-json", source_url]
    proc = run_yt_dlp(cmd, capture_json=True)
    return json.loads(proc.stdout)


def min_video_age_seconds() -> int:
    raw = os.environ.get("MIN_VIDEO_AGE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MIN_VIDEO_AGE_SECONDS
    try:
        n = int(raw)
        return max(0, n)
    except ValueError:
        return DEFAULT_MIN_VIDEO_AGE_SECONDS


def earliest_publish_unix(info: dict) -> int | None:
    """Instante Unix (UTC) mais cedo confiável para 'quando o conteúdo existiu'."""
    for key in ("release_timestamp", "timestamp"):
        ts = info.get(key)
        if ts is not None:
            try:
                return int(ts)
            except (TypeError, ValueError, OverflowError):
                pass
    ud = info.get("upload_date")
    if isinstance(ud, str) and len(ud) == 8 and ud.isdigit():
        try:
            dt = datetime(
                int(ud[:4]),
                int(ud[4:6]),
                int(ud[6:8]),
                0,
                0,
                0,
                tzinfo=timezone.utc,
            )
            return int(dt.timestamp())
        except ValueError:
            pass
    return None


def should_skip_video(info: dict) -> tuple[bool, str]:
    """
    Não processar live/agendada nem VOD com idade inferior ao mínimo (pós-live).
    """
    if info.get("is_live") is True:
        return True, "transmissão ao vivo (is_live)"

    ls = info.get("live_status")
    if isinstance(ls, str) and ls in SKIP_LIVE_STATUSES:
        return True, f"live_status={ls}"

    av = info.get("availability")
    if av == "private":
        return True, "availability=private"

    min_age = min_video_age_seconds()
    earliest = earliest_publish_unix(info)
    if earliest is None:
        return True, "sem release_timestamp/timestamp/upload_date para calcular idade mínima"

    age = time.time() - earliest
    if age < min_age:
        return True, f"aguardando idade mínima ({min_age}s; faltam ~{int(min_age - age)}s)"

    return False, ""


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
    cmd = _yt_dlp_single_video_cmd() + [
        "-f",
        "ba/b",
        "-x",
        "--audio-format",
        "mp3",
        "-o",
        pattern,
        source_url,
    ]
    run_yt_dlp(cmd, capture_json=False)
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
    episode_link: str,
    title: str,
    enclosure_url: str,
    length_bytes: int,
    itunes_image_href: str,
    pub_date: str | None = None,
) -> None:
    item = ET.SubElement(channel, "item")
    t = ET.SubElement(item, "title")
    t.text = title
    lk = ET.SubElement(item, "link")
    lk.text = episode_link
    is_permalink = "true" if guid.startswith("http") else "false"
    g = ET.SubElement(item, "guid", {"isPermaLink": is_permalink})
    g.text = guid
    pd = ET.SubElement(item, "pubDate")
    pd.text = pub_date if pub_date else format_pub_date()
    ET.SubElement(item, f"{{{ITUNES_NS}}}image", {"href": itunes_image_href})
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


def process_one_episode(
    source_url: str,
    channel: ET.Element,
    client,
    bucket: str,
    base: str,
    *,
    info: dict | None = None,
) -> None:
    vid = youtube_video_id(source_url)
    guid_str, link_str = episode_guid_and_link(source_url)
    if info is None:
        info = fetch_youtube_metadata(source_url)
    episode_title = (info.get("fulltitle") or info.get("title") or "").strip() or f"Episódio {vid}"
    pub_date = pub_date_from_video_info(info)
    thumb_urls = ordered_thumbnail_urls(info)
    if not thumb_urls:
        print("Metadados do YouTube não incluem miniatura; o feed exige itunes:image.", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        thumb_bytes, thumb_ext, thumb_ct = download_thumbnail_bytes(thumb_urls)
        thumb_path = work / f"thumb.{thumb_ext}"
        thumb_path.write_bytes(thumb_bytes)
        audio_path, _stem = download_audio(source_url, work)
        size = audio_path.stat().st_size
        ext = audio_path.suffix.lower().lstrip(".") or "mp3"
        audio_key = f"episodes/{vid}.{ext}"
        thumb_key = f"episodes/{vid}-thumb.{thumb_ext}"
        a_ct = "audio/mpeg" if ext == "mp3" else "application/octet-stream"
        upload_file(client, bucket, audio_key, audio_path, a_ct)
        upload_file(client, bucket, thumb_key, thumb_path, thumb_ct)

    public_audio = f"{base}/{audio_key}"
    public_thumb = f"{base}/{thumb_key}"
    append_item(
        channel,
        guid=guid_str,
        episode_link=link_str,
        title=episode_title,
        enclosure_url=public_audio,
        length_bytes=size,
        itunes_image_href=public_thumb,
        pub_date=pub_date,
    )
    print(f"Publicado: {public_audio} (capa: {public_thumb})")


def main() -> None:
    global _COOKIES_FROM_BROWSER
    args = parse_args()
    _COOKIES_FROM_BROWSER = args.cookies_from_browser
    load_dotenv_file()
    bucket = require_env("R2_BUCKET_NAME")
    base = require_env("R2_PUBLIC_URL").rstrip("/")
    playlist_url = require_env("YOUTUBE_PLAYLIST_URL")

    tree, channel = parse_feed()
    sync_public_urls(channel, base)
    sync_channel_metadata(channel, base)
    guids_done = existing_guids(channel)

    entries = fetch_playlist_entries(playlist_url)
    urls = playlist_watch_urls(entries)
    if not urls:
        print("Playlist vazia ou sem entradas válidas; sincronizando feed no R2.")
        write_feed(tree)
        upload_feed_to_r2(r2_client(), bucket)
        return

    client = r2_client()
    new_count = 0
    skipped = 0
    for url in urls:
        episode_key, _ = episode_guid_and_link(url)
        if episode_key in guids_done:
            continue
        info = fetch_youtube_metadata(url)
        skip, reason = should_skip_video(info)
        if skip:
            print(f"Pulando {episode_key}: {reason}")
            skipped += 1
            continue
        process_one_episode(url, channel, client, bucket, base, info=info)
        guids_done.add(episode_key)
        new_count += 1

    write_feed(tree)
    upload_feed_to_r2(client, bucket)
    if new_count == 0:
        msg = "Nenhum episódio novo publicado nesta execução; feed sincronizado no R2."
        if skipped:
            msg += f" ({skipped} vídeo(s) ignorados: live, idade < mínimo ou sem data.)"
        print(msg)
    else:
        print(f"Concluído: {new_count} episódio(s) novo(s) adicionados ao feed.")
        if skipped:
            print(f"({skipped} vídeo(s) ainda não elegíveis nesta execução.)")


if __name__ == "__main__":
    main()
