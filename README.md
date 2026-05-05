# Podcast Ranking — automação (R2 + GitHub Actions)

Baixa vídeos da playlist YouTube **Feed RSS - Spotify** (URL configurada), gera MP3 + capa, envia ao Cloudflare R2 e atualiza o RSS. Opcionalmente o workflow faz commit do `feed.xml` no repositório.

## Regra de automação

1. Novos vídeos entram na playlist **Feed RSS - Spotify** no YouTube (mantenha a ordem da playlist como quiser que apareça no processamento: o script usa `playlist_index` crescente).
2. **Agendado (grátis):** o workflow roda em UTC nos horários abaixo, que correspondem a:
   - **Domingo ~22:00 BRT** — `cron: 0 1 * * 1` (segunda-feira 01:00 UTC; no Brasil UTC−3 isso é domingo 22:00).
   - **Segunda-feira 12:00 BRT** — `cron: 0 15 * * 1` (segunda-feira 15:00 UTC = 12:00 BRT).
3. O script lista a playlist, ignora o que já está no `feed.xml` (por ID do vídeo), **pula** lives, agendadas, pós-live ainda em processamento (`live_status` do yt-dlp) e vídeos com **idade inferior** a `MIN_VIDEO_AGE_SECONDS` (padrão **10800** = 3 horas desde `release_timestamp` / `timestamp` / início do dia UTC de `upload_date`). Depois processa os elegíveis (áudio, miniatura, R2, `<item>` com título, `itunes:image`, `pubDate`).

### Rodar agora (manual)

Em **Actions → Podcast bot → Run workflow** (branch `main`): secrets R2, `YOUTUBE_PLAYLIST_URL`, e `python main.py --cookies-from-browser chrome` no runner **self-hosted** (sessão YouTube no Chrome da mesma máquina).

**Limitação:** o atraso de 3h é calculado a partir dos metadados do vídeo no YouTube, **não** a partir do “momento em que o item entrou na playlist” (isso exigiria YouTube Data API). Na prática cobre “só depois que o VOD existe há tempo suficiente”.

**GitHub:** com runner na sua máquina, o agendamento só corre quando o PC e o serviço do runner estão activos. O passo **Run publisher** tenta até **3 vezes** com **15 minutos** entre falhas.

**YouTube / sessão:** não se usa o secret `YOUTUBE_COOKIES`. O workflow chama `python main.py --cookies-from-browser chrome`; o yt-dlp lê os cookies do perfil do Chrome neste Windows (esteja com sessão iniciada no YouTube no browser). Para **Edge**, altere o comando no [`podcast_bot.yml`](.github/workflows/podcast_bot.yml) para `--cookies-from-browser edge`.

Instale dependências Python completas do yt-dlp localmente (ex.: `pip install "yt-dlp[default]"`) e **Deno** ou **Node** conforme a [wiki EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS) se aparecer erro de desafio **n**.

## Git: evitar históricos não relacionados

**Opção A (recomendada):** clonar o remoto e colar/copiar seus arquivos por cima.

```bash
git clone https://github.com/rankingdospoliticos/podcast.git
cd podcast
# copie main.py, feed.xml, etc. para esta pasta
git add .
git commit -m "Sistema de automação de podcast via R2 e Actions"
git push origin main
```

**Opção B:** já tem pasta com arquivos e o GitHub já tem README/commits:

```bash
git init
git add .
git commit -m "Import inicial"
git remote add origin https://github.com/rankingdospoliticos/podcast.git
git fetch origin
git pull origin main --no-edit --allow-unrelated-histories
# resolva conflitos se aparecerem
git push -u origin main
```

Confira no GitHub se a branch padrão é `main` ou `master` e ajuste o nome nos comandos.

## Secrets do repositório (Settings → Secrets → Actions)

Não commite credenciais. Configure estes nomes no GitHub (valores reais só lá):

| Secret | Descrição |
|--------|-----------|
| `R2_ACCOUNT_ID` | ID da conta Cloudflare (subdomínio do endpoint S3). |
| `R2_ACCESS_KEY` | Access Key do token R2. |
| `R2_SECRET_KEY` | Secret do token R2. |
| `R2_BUCKET_NAME` | Nome do bucket R2. |
| `R2_PUBLIC_URL` | URL pública estável do feed/arquivos (prefixo de `feed.xml` e de `episodes/…`), **sem barra no final**. |
| `YOUTUBE_PLAYLIST_URL` | URL da playlist (ex.: `https://www.youtube.com/playlist?list=PL…`) da **Feed RSS - Spotify**. No YouTube: Biblioteca → playlist → partilhar → copiar link. |
| `MIN_VIDEO_AGE_SECONDS` | (Opcional) Segundos mínimos após a data de publicação conhecida antes de processar; padrão no código é **10800** (3h) se o secret estiver vazio. |

A autenticação YouTube no runner **self-hosted** usa **`--cookies-from-browser`** (Chrome/Edge no próprio PC), não o secret `YOUTUBE_COOKIES`.

**Variáveis de ambiente do runner (opcional):** `YTDLP_EXTRACTOR_ARGS` substitui por completo o `--extractor-args`. Se **não** definir e existir cookies (ficheiro `cookies.txt` **ou** `--cookies-from-browser`), o `main.py` usa **`youtube:player_client=web`** por defeito.

Opcional para commit automático do feed no repo (já habilitado no workflow): não é necessário secret extra — usa `GITHUB_TOKEN`.

## Variáveis de ambiente (local)

Copie `.env.example` para `.env` e preencha. O `main.py` lê as mesmas chaves R2 e `YOUTUBE_PLAYLIST_URL` que o workflow injeta.

**Cookies YouTube (uma das opções):**

- **Runner self-hosted / local com browser:** `python main.py --cookies-from-browser chrome` ou `edge` (mesma máquina com sessão YouTube no perfil do navegador; ver [FAQ yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)).
- **Ficheiro Netscape:** coloque `cookies.txt` na raiz ou defina `YOUTUBE_COOKIES_PATH` (sem `--cookies-from-browser`, o script usa o ficheiro se existir).

- **`YTDLP_EXTRACTOR_ARGS`:** se definida no `.env`, substitui o `--extractor-args`. Sem isto, com cookies activos (browser ou ficheiro), o script usa `youtube:player_client=web` por defeito.

### Cookies exportados manualmente (alternativa ao browser)

Seguindo a [wiki do yt-dlp — Exporting YouTube cookies](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies): janela anónima, login, abrir `https://www.youtube.com/robots.txt`, export Netscape para `youtube.com`, gravar como `cookies.txt` na pasta do projeto.

Evite exportar a partir de muitas abas normais do YouTube em paralelo — os cookies podem rodar rapidamente.

## Rodar localmente

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
pip install -U --pre "yt-dlp[default]"
python main.py --cookies-from-browser chrome
```

Para o YouTube resolver desafios JavaScript localmente, instale também um runtime suportado (ex.: **Deno ≥ 2** ou Node ≥ 20 com `--js-runtimes node` no yt-dlp); ver a [wiki EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

## Credenciais

Se algo falhar por falta de permissão no remoto da organização, é preciso PAT/SSH de uma conta com push no repositório — isso é configurado na sua máquina (Git Credential Manager), não no código. Avise a equipe se precisar de um token só para CI na org.
