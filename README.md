# Podcast Ranking — automação (R2 + GitHub Actions)

Baixa vídeos da playlist YouTube **Feed RSS - Spotify** (URL configurada), gera MP3 + capa, envia ao Cloudflare R2 e atualiza o RSS. Opcionalmente o workflow faz commit do `feed.xml` no repositório.

## Regra de automação

1. Novos vídeos entram na playlist **Feed RSS - Spotify** no YouTube (mantenha a ordem da playlist como quiser que apareça no processamento: o script usa `playlist_index` crescente).
2. **Agendado (grátis):** o workflow roda em UTC nos horários abaixo, que correspondem a:
   - **Domingo ~22:00 BRT** — `cron: 0 1 * * 1` (segunda-feira 01:00 UTC; no Brasil UTC−3 isso é domingo 22:00).
   - **Segunda-feira 12:00 BRT** — `cron: 0 15 * * 1` (segunda-feira 15:00 UTC = 12:00 BRT).
3. O script lista a playlist, ignora o que já está no `feed.xml` (por ID do vídeo), **pula** lives, agendadas, pós-live ainda em processamento (`live_status` do yt-dlp) e vídeos com **idade inferior** a `MIN_VIDEO_AGE_SECONDS` (padrão **10800** = 3 horas desde `release_timestamp` / `timestamp` / início do dia UTC de `upload_date`). Depois processa os elegíveis (áudio, miniatura, R2, `<item>` com título, `itunes:image`, `pubDate`).

### Rodar agora (manual)

Em **Actions → Podcast bot → Run workflow** (branch `main`, sem inputs): o GitHub executa **o mesmo fluxo** do agendamento — cookies, secrets R2, `YOUTUBE_PLAYLIST_URL`, `python main.py`. Ou seja: **verifica na playlist o que ainda não entrou no feed**, aplica as mesmas regras (live / idade mínima) e **publica MP3 + capa + XML** para o que estiver faltando. Use quando o cron ainda não tiver corrido, tiver falhado, ou quiser forçar uma checagem imediata (sem esperar o próximo horário).

**Limitação:** o atraso de 3h é calculado a partir dos metadados do vídeo no YouTube, **não** a partir do “momento em que o item entrou na playlist” (isso exigiria YouTube Data API). Na prática cobre “só depois que o VOD existe há tempo suficiente”.

**GitHub:** execuções agendadas podem atrasar alguns minutos no plano gratuito. O passo **Run publisher** tenta até **3 vezes** com **15 minutos** entre falhas (rede / YouTube).

**YouTube / EJS:** o yt-dlp instalado via `pip` precisa do extra **`[default]`** (inclui o pacote `yt-dlp-ejs` para desafios **n** / assinatura). O workflow instala `yt-dlp[default]` e **Deno 2.x** no `PATH` (recomendado na [wiki EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS)). Sem isso podem aparecer erros do tipo *n challenge solving failed* ou *Only images are available*.

Muitos vídeos novos de uma vez podem deixar o job longo ou sujeito a limites do GitHub Actions / rate limit do YouTube.

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
| `YOUTUBE_COOKIES` | Conteúdo completo de um `cookies.txt` no formato Netscape (exportado com o yt-dlp); o workflow grava `cookies.txt` antes de rodar o script. |
| `MIN_VIDEO_AGE_SECONDS` | (Opcional) Segundos mínimos após a data de publicação conhecida antes de processar; padrão no código é **10800** (3h) se o secret estiver vazio. |

**Variáveis de ambiente do runner (opcional):** em *Actions → Variables*, `YTDLP_EXTRACTOR_ARGS` substitui o `--extractor-args` por completo. Se **não** definir: com cookies o script usa **`youtube:player_client=web`** (cliente que aceita cookies no yt-dlp). IPs de datacenter (GitHub) podem ainda devolver *Sign in to confirm you're not a bot* — veja [PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) e o [export de cookies](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).

Opcional para commit automático do feed no repo (já habilitado no workflow): não é necessário secret extra — usa `GITHUB_TOKEN`.

## Variáveis de ambiente (local)

Copie `.env.example` para `.env` e preencha. O `main.py` lê as mesmas chaves R2 e `YOUTUBE_PLAYLIST_URL` que o workflow injeta. Para o YouTube sem bloqueio de bot, coloque um `cookies.txt` (Netscape) na raiz do repositório ou defina `YOUTUBE_COOKIES_PATH` com o caminho absoluto do arquivo.

- **`YTDLP_EXTRACTOR_ARGS`:** se definida no `.env`, substitui o `--extractor-args` do yt-dlp. Se **omitida** e existir `cookies.txt` / `YOUTUBE_COOKIES_PATH`, o script usa `youtube:player_client=web` por defeito.

### Exportar cookies para o CI (recomendado)

Seguindo a [wiki do yt-dlp — Exporting YouTube cookies](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies):

1. Abra uma **janela anónima / incógnito**.
2. Inicie sessão no YouTube com a conta que deve “assinar” os pedidos do bot.
3. Na **mesma** janela, abra `https://www.youtube.com/robots.txt` (mantém a sessão estável para export).
4. Com uma extensão ou ferramenta compatível com **Netscape cookies**, exporte cookies para **`youtube.com`**.
5. Feche a janela anónima; não reutilize essa sessão para navegação diária.
6. Cole o ficheiro completo no secret **`YOUTUBE_COOKIES`** (o workflow valida tamanho, domínio e presença de nomes típicos de cookie, **sem** imprimir o conteúdo nos logs).

Evite exportar a partir de muitas abas normais do YouTube em paralelo — os cookies podem rodar e invalidar o ficheiro rapidamente.

Se no GitHub Actions aparecer **Sign in to confirm you're not a bot** mesmo com cookies recentes, o IP do runner pode ser o factor: além de renovar o secret, consulte a [wiki PO Token](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) do yt-dlp ou um runner com IP menos “marcado”.

## Rodar localmente

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
pip install -U --pre "yt-dlp[default]"
python main.py
```

Para o YouTube resolver desafios JavaScript localmente, instale também um runtime suportado (ex.: **Deno ≥ 2** ou Node ≥ 20 com `--js-runtimes node` no yt-dlp); ver a [wiki EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

## Credenciais

Se algo falhar por falta de permissão no remoto da organização, é preciso PAT/SSH de uma conta com push no repositório — isso é configurado na sua máquina (Git Credential Manager), não no código. Avise a equipe se precisar de um token só para CI na org.
