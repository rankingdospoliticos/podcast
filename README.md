# Podcast Ranking — automação (R2 + GitHub Actions)

Baixa vídeos da playlist YouTube **Feed RSS - Spotify** (URL configurada), gera MP3 + capa, envia ao Cloudflare R2 e atualiza o RSS. Opcionalmente o workflow faz commit do `feed.xml` no repositório.

## Regra de automação

1. Novos vídeos entram na playlist **Feed RSS - Spotify** no YouTube (mantenha a ordem da playlist como quiser que apareça no processamento: o script usa `playlist_index` crescente).
2. Alguém dispara **Actions → Podcast bot → Run workflow** (sem colar URL de vídeo).
3. O script lista a playlist, ignora o que já está no `feed.xml` (por ID do vídeo) e **processa todos os faltantes** numa única execução (áudio, miniatura, uploads R2, `<item>` com título da live, `itunes:image` e `pubDate` a partir dos metadados do YouTube).

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

Opcional para commit automático do feed no repo (já habilitado no workflow): não é necessário secret extra — usa `GITHUB_TOKEN`.

## Variáveis de ambiente (local)

Copie `.env.example` para `.env` e preencha. O `main.py` lê as mesmas chaves R2 e `YOUTUBE_PLAYLIST_URL` que o workflow injeta. Para o YouTube sem bloqueio de bot, coloque um `cookies.txt` (Netscape) na raiz do repositório ou defina `YOUTUBE_COOKIES_PATH` com o caminho absoluto do arquivo.

## Rodar localmente

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
pip install -U --pre yt-dlp
python main.py
```

## Credenciais

Se algo falhar por falta de permissão no remoto da organização, é preciso PAT/SSH de uma conta com push no repositório — isso é configurado na sua máquina (Git Credential Manager), não no código. Avise a equipe se precisar de um token só para CI na org.
