# Podcast Ranking — automação (R2 + GitHub Actions)

Baixa a fonte de áudio (ex.: live gravada), envia para o Cloudflare R2 (API S3), atualiza o RSS e opcionalmente faz commit do `feed.xml` no repositório.

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

Não commite credenciais. Configure estes nomes no GitHub (valores reais só lá), alinhados ao que você já usa no servidor:

| Secret | Descrição |
|--------|-----------|
| `R2_ACCOUNT_ID` | ID da conta Cloudflare (subdomínio do endpoint S3). |
| `R2_ACCESS_KEY` | Access Key do token R2. |
| `R2_SECRET_KEY` | Secret do token R2. |
| `R2_BUCKET_NAME` | Nome do bucket R2. |
| `R2_PUBLIC_URL` | URL pública estável do feed/arquivos (prefixo de `feed.xml` e de `episodes/…`), **sem barra no final**. |
| `YOUTUBE_COOKIES` | Conteúdo completo de um `cookies.txt` no formato Netscape (exportado com o yt-dlp); o workflow grava `cookies.txt` antes de rodar o script. |

O link do YouTube **não** é secret: em **Actions → Podcast bot → Run workflow** preencha o campo obrigatório **youtube_url**; o workflow repassa para o `main.py` como `YOUTUBE_URL`.

Opcional para commit automático do feed no repo (já habilitado no workflow): não é necessário secret extra — usa `GITHUB_TOKEN`.

## Variáveis de ambiente (local)

Copie `.env.example` para `.env` e preencha. O `main.py` lê as mesmas chaves R2 que o workflow injeta. Para o YouTube sem bloqueio de bot, coloque um `cookies.txt` (Netscape) na raiz do repositório ou defina `YOUTUBE_COOKIES_PATH` com o caminho absoluto do arquivo.

## Rodar localmente

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python main.py
```

## Credenciais

Se algo falhar por falta de permissão no remoto da organização, é preciso PAT/SSH de uma conta com push no repositório — isso é configurado na sua máquina (Git Credential Manager), não no código. Avise a equipe se precisar de um token só para CI na org.
