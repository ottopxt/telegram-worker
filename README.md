# Telegram Worker — Deploy na VPS (Ubuntu 22)

## 1. Conectar na VPS
```bash
ssh root@200.234.218.133
```

## 2. Instalar dependencias do sistema
```bash
apt update && apt install -y python3 python3-venv python3-pip git
```

## 3. Clonar o repositorio
```bash
cd /root
git clone https://github.com/ottopxt/telegram-worker.git
cd telegram-worker/worker
```

## 4. Criar ambiente Python
```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 5. Configurar variaveis (.env)
```bash
cp .env.example .env
nano .env
```
Preencha:
- `SUPABASE_SERVICE_ROLE_KEY` — pega no Lovable Cloud > Settings > Secrets (campo `SUPABASE_SERVICE_ROLE_KEY`)
- `TELEGRAM_API_ID` e `TELEGRAM_API_HASH` — pega em https://my.telegram.org

Salva: `Ctrl+O`, `Enter`, `Ctrl+X`.

## 6. Testar manualmente (opcional)
```bash
./venv/bin/python worker.py
```
Deve aparecer `Worker iniciado`. `Ctrl+C` pra parar.

## 7. Instalar como servico (roda 24/7)
```bash
cp worker.service /etc/systemd/system/telegram-worker.service
systemctl daemon-reload
systemctl enable telegram-worker
systemctl start telegram-worker
```

## 8. Verificar status
```bash
systemctl status telegram-worker
tail -f /var/log/telegram-worker.log
```

## Atualizar depois (quando mudar codigo no GitHub)
```bash
cd /root/telegram-worker
git pull
systemctl restart telegram-worker
```
