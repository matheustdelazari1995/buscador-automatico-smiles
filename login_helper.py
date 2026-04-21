"""
Helper script para fazer login em uma conta AwardTool.
Uso:
  ./venv/bin/python login_helper.py conta1
  ./venv/bin/python login_helper.py .browser-profile-conta1

Abre o Chrome com o perfil especificado e da 2 minutos pra voce logar.
IMPORTANTE: o servidor precisa estar PARADO, senao o perfil fica travado.
"""

import asyncio
import sys
import os
from playwright.async_api import async_playwright


async def main():
    if len(sys.argv) < 2:
        print("Uso: python login_helper.py <profile_dir_or_account_id>")
        print("Exemplo: python login_helper.py conta1")
        sys.exit(1)

    arg = sys.argv[1]
    # If user passed an account id, turn into profile dir name
    if arg.startswith(".browser-profile") or os.path.sep in arg:
        profile_dir = arg
    else:
        profile_dir = f".browser-profile-{arg}"

    abs_path = os.path.abspath(profile_dir) if not os.path.isabs(profile_dir) else profile_dir
    os.makedirs(abs_path, exist_ok=True)

    print(f"[Login Helper] Abrindo Chrome com perfil: {abs_path}")
    print("[Login Helper] Voce tem 2 MINUTOS pra fazer login no AwardTool.")
    print("[Login Helper] Navegue para https://www.awardtool.com e faca login.")
    print("[Login Helper] Depois de logar, FECHE o navegador ou aguarde o timeout.")

    pw = await async_playwright().__aenter__()
    ctx = await pw.chromium.launch_persistent_context(
        abs_path,
        channel="chrome",
        headless=False,
        viewport={"width": 1920, "height": 1080},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try:
        await page.goto("https://www.awardtool.com")
    except Exception:
        pass

    print("[Login Helper] Janela aberta. Aguardando 2 minutos...")
    try:
        await asyncio.sleep(120)
    except KeyboardInterrupt:
        pass

    print("[Login Helper] Tempo esgotado. Fechando Chrome e salvando sessao...")
    await ctx.close()
    print("[Login Helper] Pronto! Perfil salvo. A conta esta logada.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Login Helper] Cancelado pelo usuario.")
