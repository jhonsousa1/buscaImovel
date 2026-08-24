#!/usr/bin/env python3
"""Monitor de imoveis SQS 308 - versao standalone para EC2.

Diferencas da versao GitHub Actions:
  - Historico gravado em arquivo local (sem API do GitHub).
  - Segredos vem de variaveis de ambiente (/etc/imoveis-monitor.env).
  - Alerta por e-mail quando o scraping falha varias vezes seguidas,
    para que "silencio" nao seja confundido com "sem novidades".
"""
import csv, io, json, os, re, smtplib, sys, hashlib, tempfile
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

URL_ALVO = os.environ.get(
    'URL_ALVO',
    'https://www.dfimoveis.com.br/aluguel/df/todos/imoveis?palavrachave=sqs-308&vagasdegaragem=1'
)
DATA_DIR = os.environ.get('DATA_DIR', '/var/lib/imoveis-monitor')
CSV_PATH = os.path.join(DATA_DIR, 'imoveis_historico.csv')
STATE_PATH = os.path.join(DATA_DIR, 'estado.json')

GMAIL_USER = os.environ.get('GMAIL_USER', 'jhonathan.sousa1@gmail.com')
GMAIL_PASSWORD = os.environ.get('GMAIL_PASSWORD', '')
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL', 'jhonathan.sousa1@gmail.com')
CC_EMAIL = os.environ.get('CC_EMAIL', 'mestter21@gmail.com')
CHROMIUM_PATH = os.environ.get('CHROMIUM_PATH', '')
# Quantas execucoes seguidas sem resultado antes de mandar alerta de falha.
FALHAS_PARA_ALERTA = int(os.environ.get('FALHAS_PARA_ALERTA', '3'))

CSV_COLS = ['id', 'bloco', 'aluguel', 'valor_m2', 'area', 'quartos',
            'suites', 'vagas', 'link', 'data_descoberta']
now_str = datetime.now().strftime('%Y-%m-%d %H:%M')


def log(msg):
    print(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}', flush=True)


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------
def scrape():
    """Retorna a lista de anuncios. Levanta excecao se a pagina nao carregar."""
    launch_args = {'headless': True,
                   'args': ['--no-sandbox', '--disable-dev-shm-usage']}
    if CHROMIUM_PATH:
        launch_args['executable_path'] = CHROMIUM_PATH

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args)
        page = browser.new_page(
            user_agent=('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        )
        try:
            page.goto(URL_ALVO, timeout=45000, wait_until='domcontentloaded')
            try:
                page.wait_for_selector('.imovel-card', timeout=25000)
            except Exception as e:
                log(f'[AVISO] wait_for_selector: {e}')
            html = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(html, 'html.parser')
    cards = soup.select('.imovel-card')
    log(f'{len(cards)} cards encontrados no HTML renderizado.')

    imoveis = []
    for card in cards:
        anchor = card.find_parent('a', href=True) or card.find('a', href=True)
        href = anchor.get('href', '') if anchor else ''
        link = 'https://www.dfimoveis.com.br' + href if href.startswith('/') else href

        raw = [t.strip() for t in card.get_text('\n').split('\n') if t.strip()]

        # Mescla tokens separados: ["R$", "3.500"] -> ["R$ 3.500"]
        texts, i = [], 0
        while i < len(raw):
            if raw[i] == 'R$' and i + 1 < len(raw) and re.match(r'^[\d.,]+$', raw[i + 1]):
                texts.append(f'R$ {raw[i + 1]}')
                i += 2
            else:
                texts.append(raw[i])
                i += 1

        bloco = card.find('h2').get_text(strip=True) if card.find('h2') else ''
        aluguel = next((t for t in texts if t.startswith('R$') and 'Valor' not in t), '')

        valor_m2 = ''
        for j, t in enumerate(texts):
            if 'Valor m' in t:
                if re.search(r'R\$', t):
                    valor_m2 = t
                elif j + 1 < len(texts) and texts[j + 1].startswith('R$'):
                    valor_m2 = f'{t} {texts[j + 1]}'
                else:
                    valor_m2 = t
                break

        area = ''
        for t in texts:
            if 'm²' in t and 'Valor' not in t:
                m = re.search(r'(\d+)\s*m²', t)
                area = f'{m.group(1)} m²' if m else t
                break

        quartos = next((t for t in texts if 'Quarto' in t), '')
        suites = next((t for t in texts if 'Suíte' in t or 'Suite' in t), '')
        vagas = next((t for t in texts if 'Vaga' in t), '')

        # ID do link; fallback deterministico pelos campos principais.
        id_match = re.search(r'-(\d+)(?:[/?]|$)', href)
        imovel_id = id_match.group(1) if id_match else hashlib.md5(
            f'{bloco}|{area}|{quartos}|{aluguel}'.encode()).hexdigest()[:12]

        imoveis.append({
            'id': imovel_id, 'bloco': bloco, 'aluguel': aluguel,
            'valor_m2': valor_m2, 'area': area, 'quartos': quartos,
            'suites': suites, 'vagas': vagas, 'link': link,
            'data_descoberta': now_str,
        })
    return imoveis


# --------------------------------------------------------------------------
# Historico local
# --------------------------------------------------------------------------
def ler_historico():
    if not os.path.exists(CSV_PATH):
        return set(), []
    with open(CSV_PATH, newline='', encoding='utf-8') as f:
        historico = list(csv.DictReader(f))
    return {row['id'] for row in historico if row.get('id')}, historico


def salvar_historico(historico, novos):
    """Grava de forma atomica: escreve em arquivo temporario e renomeia."""
    todos = historico + novos
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix='.hist-', suffix='.csv')
    try:
        with os.fdopen(fd, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(todos)
        os.replace(tmp, CSV_PATH)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    log(f'Historico atualizado: +{len(novos)} novo(s), {len(todos)} no total.')


def ler_estado():
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'falhas_seguidas': 0, 'alerta_enviado': False}


def salvar_estado(estado):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(estado, f)


# --------------------------------------------------------------------------
# E-mail
# --------------------------------------------------------------------------
def _enviar(subject, html):
    if not GMAIL_PASSWORD:
        raise RuntimeError('GMAIL_PASSWORD nao definido no ambiente.')
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = GMAIL_USER
    msg['To'] = NOTIFY_EMAIL
    destinos = [NOTIFY_EMAIL]
    if CC_EMAIL:
        msg['Cc'] = CC_EMAIL
        destinos.append(CC_EMAIL)
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    with smtplib.SMTP('smtp.gmail.com', 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, destinos, msg.as_string())
    log(f'E-mail enviado para {", ".join(destinos)}.')


def enviar_email_novos(novos):
    subject = (f'\U0001f3e0 [Alerta Imóveis] {len(novos)} novo(s) '
               f'disponível(is) — SQS 308, Asa Sul')
    cards_html = ''
    for im in novos:
        botao = (
            f'<a href="{im["link"]}" style="display:inline-block;background:#1a3a5c;'
            f'color:#fff;text-decoration:none;padding:10px 22px;border-radius:6px;'
            f'font-size:14px;font-weight:600;">Ver Anúncio →</a>'
        ) if im['link'] else (
            f'<span style="color:#888;font-size:13px;">(link indisponível — '
            f'busque por "{im["bloco"]}" no site)</span>'
        )
        cards_html += (
            f'<div style="background:#fff;border:1px solid #e0e6ed;border-radius:10px;'
            f'padding:20px;margin-bottom:16px;">'
            f'<div style="font-size:17px;font-weight:700;color:#1a3a5c;margin-bottom:8px;">{im["bloco"]}</div>'
            f'<div style="font-size:26px;font-weight:800;color:#1a8a4a;margin-bottom:10px;">{im["aluguel"]}</div>'
            f'<div style="color:#555;font-size:14px;margin-bottom:12px;">'
            f'\U0001f4d0 {im["area"]} · \U0001f6cf {im["quartos"]} · \U0001f6c1 {im["suites"]} · '
            f'\U0001f697 {im["vagas"]} · <span style="color:#888;">{im["valor_m2"]}</span></div>'
            f'{botao}</div>'
        )
    linhas = ''.join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #eee;">{im["bloco"]}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#1a8a4a;font-weight:700;">{im["aluguel"]}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{im["area"]}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{im["quartos"]}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{im["valor_m2"]}</td></tr>'
        for im in novos
    ) if len(novos) > 1 else ''
    tabela = (
        f'<div style="margin-top:24px;"><div style="font-size:15px;font-weight:700;'
        f'color:#1a3a5c;margin-bottom:10px;">Resumo Comparativo</div>'
        f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        f'<thead><tr style="background:#1a3a5c;color:#fff;">'
        f'<th style="padding:10px 12px;text-align:left;">Endereço</th>'
        f'<th style="padding:10px 12px;text-align:left;">Aluguel</th>'
        f'<th style="padding:10px 12px;text-align:left;">&Aacute;rea</th>'
        f'<th style="padding:10px 12px;text-align:left;">Quartos</th>'
        f'<th style="padding:10px 12px;text-align:left;">R$/m²</th>'
        f'</tr></thead><tbody>{linhas}</tbody></table></div>'
    ) if len(novos) > 1 else ''
    html = (
        f'<div style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;'
        f'background:#f4f6f9;padding:20px;">'
        f'<div style="background:#1a3a5c;border-radius:10px 10px 0 0;padding:28px 24px;text-align:center;">'
        f'<div style="font-size:28px;margin-bottom:6px;">\U0001f3e0</div>'
        f'<div style="color:#fff;font-size:22px;font-weight:800;">Alerta de Novos Imóveis</div>'
        f'<div style="color:#a8c4e0;font-size:14px;margin-top:6px;">SQS 308 · Asa Sul · Brasília/DF</div></div>'
        f'<div style="background:#fff;padding:24px;border-left:1px solid #e0e6ed;border-right:1px solid #e0e6ed;">'
        f'<div style="color:#555;font-size:13px;margin-bottom:20px;padding:10px 14px;background:#f0f7ff;'
        f'border-radius:6px;border-left:4px solid #1a3a5c;">'
        f'\U0001f4c5 Verificado em {now_str} · {len(novos)} novo(s) imóvel(is) encontrado(s)</div>'
        f'{cards_html}{tabela}</div>'
        f'<div style="background:#e8edf2;border-radius:0 0 10px 10px;padding:14px 24px;text-align:center;'
        f'color:#888;font-size:12px;">Monitoramento automático · DFImóveis · SQS 308 · '
        f'Execução a cada hora (EC2)</div></div>'
    )
    _enviar(subject, html)


def enviar_email_falha(falhas, detalhe):
    subject = f'⚠️ [Monitor SQS 308] Sem resultados há {falhas} execuções seguidas'
    html = (
        f'<div style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:20px;">'
        f'<h2 style="color:#b8860b;">Monitor pode estar quebrado</h2>'
        f'<p>As últimas <b>{falhas}</b> execuções não retornaram nenhum anúncio. '
        f'Isso pode significar que o site mudou o layout, está bloqueando a EC2, '
        f'ou que realmente não há imóveis com esse filtro.</p>'
        f'<p style="color:#555;font-size:13px;"><b>Último detalhe:</b><br>'
        f'<code>{detalhe}</code></p>'
        f'<p style="color:#555;font-size:13px;">Para investigar na EC2:<br>'
        f'<code>sudo journalctl -u imoveis-monitor -n 100 --no-pager</code></p>'
        f'<p style="color:#888;font-size:12px;">Verificado em {now_str}</p></div>'
    )
    _enviar(subject, html)


def enviar_email_recuperado(qtd):
    subject = '✅ [Monitor SQS 308] Voltou a funcionar'
    html = (
        f'<div style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:20px;">'
        f'<h2 style="color:#1a8a4a;">Monitor normalizado</h2>'
        f'<p>A execução de {now_str} encontrou <b>{qtd}</b> anúncio(s) no site. '
        f'O monitoramento voltou ao normal.</p></div>'
    )
    _enviar(subject, html)


# --------------------------------------------------------------------------
# Fluxo principal
# --------------------------------------------------------------------------
def main():
    estado = ler_estado()
    falha_detalhe = None

    try:
        imoveis_atuais = scrape()
    except Exception as e:
        imoveis_atuais = []
        falha_detalhe = f'{type(e).__name__}: {e}'
        log(f'[ERRO] scraping falhou — {falha_detalhe}')

    log(f'{len(imoveis_atuais)} anuncios coletados.')

    if not imoveis_atuais:
        estado['falhas_seguidas'] = estado.get('falhas_seguidas', 0) + 1
        log(f'Execucao sem resultados ({estado["falhas_seguidas"]} seguida(s)).')
        if (estado['falhas_seguidas'] >= FALHAS_PARA_ALERTA
                and not estado.get('alerta_enviado')):
            try:
                enviar_email_falha(estado['falhas_seguidas'],
                                   falha_detalhe or 'pagina carregou, mas 0 cards')
                estado['alerta_enviado'] = True
            except Exception as e:
                log(f'[ERRO] nao consegui enviar alerta de falha: {e}')
        salvar_estado(estado)
        return 1

    # Houve resultado: se vinhamos de falha alertada, avisa a recuperacao.
    if estado.get('alerta_enviado'):
        try:
            enviar_email_recuperado(len(imoveis_atuais))
        except Exception as e:
            log(f'[ERRO] nao consegui enviar aviso de recuperacao: {e}')
    estado['falhas_seguidas'] = 0
    estado['alerta_enviado'] = False
    salvar_estado(estado)

    ids_existentes, historico = ler_historico()
    log(f'Historico: {len(ids_existentes)} imoveis registrados.')

    novos = [im for im in imoveis_atuais if im['id'] not in ids_existentes]
    log(f'Novos imoveis detectados: {len(novos)}')

    if not novos:
        log(f'Sem novidades. Historico: {len(ids_existentes)} imoveis.')
        return 0

    # Grava antes de enviar: se o e-mail falhar, um retry manual nao duplica
    # o CSV; e se o envio falhar de vez, o log registra quais eram.
    salvar_historico(historico, novos)
    try:
        enviar_email_novos(novos)
    except Exception as e:
        log(f'[ERRO] falha ao enviar e-mail dos novos imoveis: {e}')
        for im in novos:
            log(f'  NOVO: {im["bloco"]} — {im["aluguel"]} — {im["link"]}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
