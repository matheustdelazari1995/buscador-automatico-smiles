# AwardTool Scraper

Sistema de busca automatizada de disponibilidade de milhas aereas via AwardTool.com.

## Stack
- **Backend**: Python 3.11 + FastAPI + WebSocket
- **Scraping**: Playwright com Chrome real
- **Frontend**: HTML/CSS/JS (single page)
- **Notificacoes**: Evolution API (WhatsApp)

## Funcionalidades
- Busca disponibilidade mes a mes de rotas aereas
- Filtros: Origem, Destino, Programa, Classe, Max Milhas, Direcao, Meses
- Dashboard web com progresso em tempo real
- Envio automatico de resultados via WhatsApp
- Suporte a 3 classes: Economica, Executiva, Primeira Classe
- Suporte a 3 direcoes: Ida e volta, So ida, So volta

## Setup

```bash
# Clonar repositorio
git clone <url-do-repo>
cd awardtool-scraper

# Instalar dependencias
./setup.sh

# Copiar e editar config
cp config.example.json config.json
# Edite config.json com sua API key do Evolution

# Rodar servidor
source venv/bin/activate
python server.py
```

Acesse: http://localhost:8000

## Login no AwardTool
O AwardTool requer login. Na primeira execucao, rode o script de login (ver `DOCUMENTACAO_COMPLETA.md`) para autenticar no perfil do Playwright.

## Documentacao completa
Ver `DOCUMENTACAO_COMPLETA.md` para arquitetura, API, deploy em VPS e estrategia multi-conta.

## Estrutura
```
awardtool-scraper/
|-- server.py              # FastAPI backend
|-- search_engine.py       # Motor de scraping Playwright
|-- static/index.html      # Dashboard frontend
|-- config.example.json    # Modelo de configuracao
|-- requirements.txt       # Deps Python
|-- Dockerfile             # Para deploy
|-- docker-compose.yml     # Orquestracao
`-- DOCUMENTACAO_COMPLETA.md
```
