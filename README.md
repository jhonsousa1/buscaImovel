# buscaImovel

Monitor automático de imóveis para aluguel no [DFImóveis](https://www.dfimoveis.com.br),
filtrando a quadra **SQS 308 (Asa Sul, Brasília/DF)** com pelo menos uma vaga de garagem.

A cada execução o monitor compara os anúncios da página de busca com o histórico
em `imoveis_historico.csv`. Se encontrar algum imóvel ainda não registrado, envia
um e-mail com os detalhes e grava o novo registro no CSV.

## Arquivos

| Arquivo | Função |
| --- | --- |
| `monitor.py` | Coleta, comparação com o histórico e envio do e-mail |
| `test_monitor.py` | Testes do parser (sem rede), executados na CI |
| `.github/workflows/monitor-imoveis.yml` | Agendamento (a cada hora) e commit do histórico |
| `imoveis_historico.csv` | Histórico de imóveis já vistos (chave: `id` do anúncio) |
| `links.txt` | Coleta anterior, usada para semear o histórico |

## Configuração

O workflow usa o `GITHUB_TOKEN` nativo do Actions para commitar o histórico —
**não é necessário nenhum Personal Access Token**.

Só é preciso configurar o envio de e-mail, em
`Settings → Secrets and variables → Actions`:

**Secret** (aba *Secrets*):

| Nome | Valor |
| --- | --- |
| `GMAIL_APP_PASSWORD` | Senha de app do Gmail (16 caracteres, gerada em [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)) |

**Variables** (aba *Variables*):

| Nome | Valor |
| --- | --- |
| `GMAIL_USER` | Conta Gmail remetente |
| `NOTIFY_EMAIL` | Destinatário do alerta (se vazio, usa `GMAIL_USER`) |

Sem essa configuração o monitor **aborta antes de gravar o histórico**. Isso é
deliberado: gravar sem conseguir enviar o e-mail marcaria os imóveis como já
vistos e o alerta seria perdido para sempre.

## Execução manual

Em `Actions → Monitor Imoveis SQS 308 → Run workflow`. Também dá para rodar local:

```bash
pip install requests beautifulsoup4

# Só mostra o que encontrou: não grava o CSV nem envia e-mail.
DRY_RUN=1 python monitor.py

# Execução real.
GMAIL_USER=... GMAIL_APP_PASSWORD=... NOTIFY_EMAIL=... python monitor.py
```

## Testes

```bash
python -m unittest discover -p 'test_*.py' -v
```

Não acessam a rede — o parser é exercitado sobre HTML de fixture, e há uma
regressão que confere a extração de ID e de endereço contra as 456 URLs reais
de `links.txt`. A CI roda a suíte antes de cada coleta, para que uma quebra no
parser não grave dados errados no histórico.

## Alterando a busca

A URL de busca vem da variável de ambiente `URL_ALVO` (o default está em
`monitor.py`). Para monitorar outra quadra, defina `URL_ALVO` como variável do
repositório e adicione-a ao passo *Executar monitor* do workflow.

## Quando o site muda de layout

O scraper não depende de nenhuma classe CSS: ele localiza os anúncios pelos
links `/imovel/...-<id>`, que é a parte mais estável do HTML. Ainda assim, se a
coleta não retornar nenhum anúncio o script **falha com código de erro** em vez
de reportar "sem novidades" — assim uma quebra aparece como falha no Actions e
gera notificação, em vez de passar meses silenciosamente sem alertas.
