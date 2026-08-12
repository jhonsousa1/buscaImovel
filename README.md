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

Sem o secret o monitor ainda roda e atualiza o CSV, mas registra
`[AVISO] GMAIL_APP_PASSWORD ausente` em vez de enviar o e-mail.

## Execução manual

Em `Actions → Monitor Imoveis SQS 308 → Run workflow`. Também dá para rodar local:

```bash
pip install requests beautifulsoup4
GMAIL_USER=... GMAIL_APP_PASSWORD=... python monitor.py
```

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
