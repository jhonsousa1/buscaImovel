# Monitor SQS 308 — versão EC2

Roda o monitoramento na sua própria EC2, via `systemd timer` de hora em hora.
Sem GitHub Actions e sem API do GitHub — o histórico fica num CSV local na instância.

## O que muda em relação à versão do GitHub

| | GitHub Actions | EC2 |
|---|---|---|
| Agendamento | `cron` do workflow | `systemd timer` (`OnCalendar=hourly`) |
| Histórico | commit no `imoveis_historico.csv` via API | `/var/lib/imoveis-monitor/imoveis_historico.csv` |
| Segredos | `secrets` do repositório | `/etc/imoveis-monitor.env` (permissão 640) |
| Logs | aba Actions | `journalctl -u imoveis-monitor` |
| Falha silenciosa | nenhum aviso | e-mail de alerta após 3 execuções sem resultado |

## Requisitos da instância

- Amazon Linux 2023, Ubuntu ou Debian.
- **Mínimo t3.small (2 GB).** Em t2/t3.micro (1 GB) o Chromium headless morre por
  falta de memória — o instalador cria automaticamente um swap de 2 GB nesse caso,
  o que resolve, mas deixa a execução mais lenta.
- Saída liberada no security group para **443** (site) e **587** (SMTP do Gmail).
  A AWS bloqueia a porta 25 por padrão, mas **não** a 587 — então o envio funciona.
- ~2 GB de disco livres (o Chromium do Playwright ocupa cerca de 450 MB).

## Instalação

Copie a pasta para a instância e rode o instalador:

```bash
# da sua máquina
scp -r ec2/ imoveis_historico.csv ec2-user@SEU_IP:/tmp/monitor-setup/

# na EC2
sudo bash /tmp/monitor-setup/ec2/install.sh
```

Ou, se preferir clonar direto na instância:

```bash
git clone https://github.com/jhonsousa1/buscaImovel.git
cd buscaImovel
cp imoveis_historico.csv ec2/          # semeia o histórico já conhecido
sudo bash ec2/install.sh
```

Depois:

```bash
sudo nano /etc/imoveis-monitor.env        # 1) coloque a senha de app do Gmail
sudo systemctl start imoveis-monitor      # 2) roda uma vez agora, para testar
sudo journalctl -u imoveis-monitor -n 50 --no-pager   # 3) confere o resultado
```

Uma execução saudável imprime algo como:

```
[...] 4 cards encontrados no HTML renderizado.
[...] 4 anuncios coletados.
[...] Historico: 4 imoveis registrados.
[...] Novos imoveis detectados: 0
[...] Sem novidades. Historico: 4 imoveis.
```

## Operação do dia a dia

```bash
systemctl list-timers imoveis-monitor.timer     # quando roda a próxima vez
journalctl -u imoveis-monitor --since today     # log de hoje
journalctl -u imoveis-monitor -f                # acompanhar ao vivo
cat /var/lib/imoveis-monitor/imoveis_historico.csv
sudo systemctl stop imoveis-monitor.timer       # pausar
sudo systemctl start imoveis-monitor.timer      # retomar
```

Mudar a frequência (ex.: a cada 30 min):

```bash
sudo systemctl edit --full imoveis-monitor.timer   # OnCalendar=*:0/30
sudo systemctl daemon-reload && sudo systemctl restart imoveis-monitor.timer
```

## Configuração (`/etc/imoveis-monitor.env`)

| Variável | Padrão | Para que serve |
|---|---|---|
| `GMAIL_PASSWORD` | — | senha de app de 16 caracteres, sem espaços (obrigatória) |
| `GMAIL_USER` | `jhonathan.sousa1@gmail.com` | conta que envia |
| `NOTIFY_EMAIL` | `jhonathan.sousa1@gmail.com` | destinatário |
| `CC_EMAIL` | `mestter21@gmail.com` | cópia (deixe vazio para não copiar ninguém) |
| `URL_ALVO` | busca SQS 308 com 1 vaga | trocar aqui muda o filtro monitorado |
| `FALHAS_PARA_ALERTA` | `3` | execuções seguidas sem resultado antes do e-mail de alerta |

## Aviso de quebra

Como agora o "silêncio" é o comportamento normal (nenhum e-mail = nada novo), o
script distingue os dois casos: depois de `FALHAS_PARA_ALERTA` execuções seguidas
sem nenhum anúncio — site fora do ar, layout alterado, bloqueio de IP — ele manda
um e-mail de aviso, **uma vez só**, e outro quando voltar ao normal. Sem isso, o
monitor podia ficar quebrado por semanas parecendo apenas "sem novidades".

## Depois de migrar

1. Desative o workflow antigo para não consumir minutos do GitHub:
   `git rm .github/workflows/monitor.yml` (ou desabilite pela aba Actions).
2. Remova a rotina agendada do Claude que rodava esse mesmo script.
3. **Revogue o PAT do GitHub** usado pelo monitor antigo — ele não é mais
   necessário por nada aqui.
4. Troque também a senha de app do Gmail, pela mesma razão, e coloque a nova
   apenas no `/etc/imoveis-monitor.env`.

## Problemas comuns

**`Host system is missing dependencies to run browsers`** — faltou biblioteca do
Chromium. No Ubuntu: `sudo /opt/imoveis-monitor/venv/bin/playwright install --with-deps chromium`.
No Amazon Linux, reveja o `dnf install` do `install.sh`.

**O processo é morto sem mensagem (`Killed`)** — falta de RAM. Confirme com
`dmesg | grep -i oom` e verifique se o swap está ativo (`swapon --show`).

**`Username and Password not accepted`** — a senha de app precisa estar sem os
espaços que o Google mostra, e a verificação em duas etapas precisa estar ligada
na conta.

**0 cards encontrados, mas o site abre no navegador** — o DFImóveis pode estar
bloqueando o IP da EC2. Teste na instância:
`sudo -u imoveis PLAYWRIGHT_BROWSERS_PATH=/opt/imoveis-monitor/browsers /opt/imoveis-monitor/venv/bin/python /opt/imoveis-monitor/monitor.py`.
Se for bloqueio, aumentar o intervalo entre execuções costuma resolver.
