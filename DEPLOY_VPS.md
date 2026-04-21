# Deploy em VPS Hetzner - Guia passo a passo

Este guia tem **2 etapas**:
- **Etapa 1 (~30 min)**: subir rapido, acessar pelo IP, sem HTTPS. Ideal pra testar.
- **Etapa 2 (opcional, ~15 min)**: adicionar dominio + HTTPS + basic auth quando quiser.

Stack: Docker + Xvfb (Chrome virtual) + noVNC (Chrome acessivel via browser).

---

# ETAPA 1 — Subir rapido (sem dominio)

## 1.1 Criar o servidor no Hetzner (~10 min)

1. Ir em https://www.hetzner.com/cloud e criar conta
2. Adicionar metodo de pagamento (cartao ou PayPal)
3. Criar novo projeto (ex: "Smiles")
4. Clicar em **"Add server"**:
   - **Location**: Nuremberg ou Falkenstein (Alemanha)
   - **Image**: Ubuntu 22.04
   - **Type**: **CPX21** (3 vCPU, 4GB RAM, 80GB SSD) = EUR 7.05/mes
     - Alternativa barata: CPX11 (2 vCPU, 2GB) = EUR 4.15/mes (aguenta 2 contas)
   - **SSH Keys**: IMPORTANTE — adicionar sua chave SSH publica antes
     - Se nao tem: no Mac rode `ssh-keygen -t ed25519` e cole o conteudo de `~/.ssh/id_ed25519.pub`
   - **Firewall**: criar com regras:
     - TCP 22 (SSH) - qualquer IP
     - TCP 8001 - qualquer IP (dashboard)
     - TCP 6080 - qualquer IP (noVNC)
   - **Name**: `smiles-vps`
5. Criar e anotar o **IP publico** (ex: `157.90.xxx.xxx`)

## 1.2 Acessar via SSH

No Mac:
```bash
ssh root@157.90.xxx.xxx
```

## 1.3 Instalar Docker no servidor

Dentro da VPS:
```bash
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
apt install -y docker-compose-plugin rsync
```

## 1.4 Enviar o codigo do Mac pro VPS

**No seu Mac**:
```bash
cd "/Users/matheusdelazari/Desktop/Claude & MTD/buscador-automatico-smiles"

# Cria um tar.gz sem venv/profiles/dados locais
tar --exclude='venv' --exclude='profiles' --exclude='.browser-profile*' \
    --exclude='routes.json' --exclude='results.json' --exclude='accounts.json' \
    --exclude='system_state.json' --exclude='__pycache__' \
    -czvf /tmp/smiles-deploy.tar.gz .

# Envia pro VPS (troca pelo seu IP)
scp /tmp/smiles-deploy.tar.gz root@157.90.xxx.xxx:/root/
```

**No VPS**:
```bash
mkdir -p /opt/smiles
cd /opt/smiles
tar xzf /root/smiles-deploy.tar.gz
ls
```

Tem que aparecer server.py, Dockerfile, etc.

## 1.5 Criar o config.json

No VPS:
```bash
cd /opt/smiles
cp config.example.json config.json
nano config.json
```

Editar com sua API key do Evolution. Salvar com Ctrl+O, Enter, Ctrl+X.

## 1.6 Subir a aplicacao

```bash
cd /opt/smiles
docker compose -f docker-compose.simple.yml up -d --build
```

Primeira vez demora ~5 minutos (baixa imagens, instala Chrome). Pegue um cafe.

Quando terminar, verificar:
```bash
docker compose -f docker-compose.simple.yml ps
docker compose -f docker-compose.simple.yml logs app | tail -20
```

## 1.7 Acessar o dashboard

No seu browser do Mac, acessar:

### http://157.90.xxx.xxx:8001

(trocando pelo IP da sua VPS)

O Chrome vai avisar "Nao seguro" — normal pois nao tem HTTPS ainda. Clique em "Avancado" > "Ir para ... (inseguro)".

## 1.8 Criar conta + login via noVNC

1. No dashboard, aba **Contas** — adicionar ID=`conta1`, Nome=`Conta Principal`. Isso ja dispara um Chrome no VPS.
2. Abrir em outra aba do seu browser: **http://157.90.xxx.xxx:6080/vnc.html?autoconnect=true**
3. Vai aparecer o desktop do VPS com o Chrome aberto.
4. Nesse Chrome, navegar pra https://www.awardtool.com e fazer login
5. Fechar o Chrome (ou so a aba) — a sessao fica salva no perfil
6. Voltar no dashboard, cadastrar uma rota de teste e clicar em "Iniciar pendentes"

Pronto! O sistema esta rodando no VPS.

---

# ETAPA 2 — Adicionar dominio + HTTPS (opcional)

Faca quando quiser "formalizar" o acesso.

## 2.1 Registrar um dominio

- **Registro.br** (.com.br): ~R$ 40/ano — https://registro.br
- **Namecheap** (.com, .io, etc): ~$10/ano — https://namecheap.com

Nao precisa ser algo sofisticado. Exemplo: `tripse-smiles.com.br`.

## 2.2 Apontar DNS pro VPS

No painel do seu provedor, criar um registro **A**:
- Nome: `smiles` (vai virar `smiles.tripse-smiles.com.br`)
  - Ou `@` pra usar o dominio raiz
- Tipo: A
- Valor: IP do VPS (157.90.xxx.xxx)
- TTL: 300

Testar no Mac (espera 5-30 min depois de criar):
```bash
nslookup smiles.tripse-smiles.com.br
```

Tem que retornar o IP do VPS.

## 2.3 Preparar arquivos no VPS

Dentro da VPS:
```bash
cd /opt/smiles

# Parar a versao simples
docker compose -f docker-compose.simple.yml down

# Instalar apache2-utils (pra htpasswd)
apt install -y apache2-utils

# Criar usuario/senha do Basic Auth
mkdir -p deploy
htpasswd -c deploy/htpasswd matheus
# Vai pedir uma senha — escolha uma forte

# Trocar dominio no nginx.conf
sed -i 's/SEU_DOMINIO.com/smiles.tripse-smiles.com.br/g' deploy/nginx.conf
```

## 2.4 Abrir portas 80 e 443 no firewall Hetzner

Painel do Hetzner > Firewall do servidor > adicionar regras:
- TCP 80 - qualquer IP
- TCP 443 - qualquer IP

Pode tirar as regras 8001 e 6080 (nao sao mais usadas direto).

## 2.5 Obter certificado HTTPS (Let's Encrypt)

```bash
mkdir -p certbot/conf certbot/www

# Troca pelo seu dominio e email
docker run -it --rm \
  -v "$(pwd)/certbot/conf:/etc/letsencrypt" \
  -v "$(pwd)/certbot/www:/var/www/certbot" \
  -p 80:80 \
  certbot/certbot certonly --standalone \
    -d smiles.tripse-smiles.com.br \
    --email seu-email@gmail.com \
    --agree-tos --no-eff-email
```

Se deu certo: "Successfully received certificate".

## 2.6 Subir a versao com HTTPS

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

## 2.7 Acessar

**https://smiles.tripse-smiles.com.br** — dashboard (pede login basic auth)

**https://smiles.tripse-smiles.com.br/vnc/** — Chrome remoto (pede login basic auth)

---

# OPERACAO

## Comandos uteis no VPS

```bash
cd /opt/smiles

# Ver status
docker compose -f docker-compose.simple.yml ps
# (ou .prod.yml dependendo de qual voce subiu)

# Ver logs
docker compose -f docker-compose.simple.yml logs -f app

# Reiniciar app (sem derrubar nginx)
docker compose -f docker-compose.simple.yml restart app

# Parar tudo
docker compose -f docker-compose.simple.yml down

# Subir tudo
docker compose -f docker-compose.simple.yml up -d
```

## Atualizar codigo

No Mac:
```bash
cd "/Users/matheusdelazari/Desktop/Claude & MTD/buscador-automatico-smiles"
tar --exclude='venv' --exclude='profiles' --exclude='.browser-profile*' \
    --exclude='routes.json' --exclude='results.json' --exclude='accounts.json' \
    --exclude='system_state.json' --exclude='__pycache__' \
    -czvf /tmp/smiles-deploy.tar.gz .
scp /tmp/smiles-deploy.tar.gz root@157.90.xxx.xxx:/root/
```

No VPS:
```bash
cd /opt/smiles

# Fazer backup dos dados primeiro!
tar czf /root/smiles-backup-$(date +%Y%m%d-%H%M).tar.gz data profiles config.json

# Atualizar codigo (preserva data/, profiles/, config.json pois estao no tar fonte excluidos)
tar xzf /root/smiles-deploy.tar.gz

# Rebuild e sobe
docker compose -f docker-compose.simple.yml up -d --build
```

## Backup

```bash
# No VPS
tar czf /root/smiles-backup-$(date +%Y%m%d).tar.gz \
  /opt/smiles/data /opt/smiles/profiles /opt/smiles/config.json
```

Baixar pro Mac:
```bash
scp root@157.90.xxx.xxx:/root/smiles-backup-*.tar.gz ~/Desktop/
```

---

# TROUBLESHOOTING

### Dashboard nao carrega
```bash
docker compose -f docker-compose.simple.yml logs app | tail -50
```

Se Chrome travou:
```bash
docker compose -f docker-compose.simple.yml restart app
```

### Conta bloqueada pelo AwardTool
VPS tem IP de datacenter, mais facil de bloquear. Se acontecer muito:
- Usar proxies residenciais (IPRoyal, SmartProxy, ~$50/mes)
- Ou limitar a 1-2 contas ativas simultaneas

### Performance ruim
Upgrade do servidor no painel Hetzner:
- CPX21 → CPX31 (EUR 13/mes, 4 vCPU / 8 GB) — aguenta 8-10 contas

---

# CUSTOS

| Item | Custo |
|------|-------|
| Hetzner CPX21 | EUR 7.05/mes (~R$ 40) |
| Dominio .com.br (opcional) | R$ 40/ano (~R$ 3.40/mes) |
| HTTPS Let's Encrypt | Gratis |
| **Total** | **~R$ 40-43/mes** |

Bom deploy!
