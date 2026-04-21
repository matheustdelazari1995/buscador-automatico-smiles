#!/bin/bash
echo "🔧 Configurando AwardTool Scraper..."

# Cria virtual environment
python3 -m venv venv
source venv/bin/activate

# Instala dependências
pip install -r requirements.txt

# Instala navegador Chromium para Playwright
playwright install chromium

echo ""
echo "✅ Setup concluído!"
echo ""
echo "Para rodar:"
echo "  source venv/bin/activate"
echo "  python scraper.py"
