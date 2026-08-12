import base64, csv, io, smtplib, re, os
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import requests

URL_ALVO = 'https://www.dfimoveis.com.br/aluguel/df/todos/imoveis?palavrachave=sqs-308&vagasdegaragem=1'
GITHUB_REPO = 'jhonsousa1/buscaImovel'
GITHUB_PAT = os.environ['GITHUB_TOKEN']
CSV_FILE = 'imoveis_historico.csv'
GMAIL_USER = 'jhonathan.sousa1@gmail.com'
GMAIL_PASSWORD = os.environ['GMAIL_PASSWORD']
NOTIFY_EMAIL = 'jhonathan.sousa1@gmail.com'
CSV_COLS = ['id','bloco','aluguel','valor_m2','area','quartos','suites','vagas','link','data_descoberta']
now_str = datetime.now().strftime('%Y-%m-%d %H:%M')

def scrape():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        try:
            page.goto(URL_ALVO, timeout=30000, wait_until='domcontentloaded')
            # aguarda os cards carregarem via JS
            page.wait_for_selector('.imovel-card', timeout=20000)
        except Exception as e:
            print(f'[AVISO] wait_for_selector: {e}')
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, 'html.parser')
    cards = soup.select('.imovel-card')
    print(f'[DEBUG] {len(cards)} cards encontrados no HTML renderizado.')

    imoveis = []
    for card in cards:
        parent_a = card.find_parent('a', href=True)
        href = parent_a['href'] if parent_a else ''
        link = 'https://www.dfimoveis.com.br' + href if href else ''
        id_match = re.search(r'-(\d+)$', href)
        imovel_id = id_match.group(1) if id_match else href
        texts = [t.strip() for t in card.get_text('\n').split('\n') if t.strip()]
        bloco = card.find('h2').get_text(strip=True) if card.find('h2') else ''
        aluguel = next((t for t in texts if t.startswith('R$') and 'Valor' not in t), '')
        valor_m2 = next((t for t in texts if 'Valor m' in t), '')
        area = next((t for t in texts if 'm²' in t and 'Valor' not in t), '')
        quartos = next((t for t in texts if 'Quarto' in t), '')
        suites = next((t for t in texts if 'Suíte' in t or 'Suite' in t), '')
        vagas = next((t for t in texts if 'Vaga' in t), '')
        imoveis.append({
            'id': imovel_id, 'bloco': bloco, 'aluguel': aluguel, 'valor_m2': valor_m2,
            'area': area, 'quartos': quartos, 'suites': suites, 'vagas': vagas,
            'link': link, 'data_descoberta': now_str
        })
    return imoveis

def ler_historico():
    url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{CSV_FILE}'
    headers = {'Authorization': f'token {GITHUB_PAT}', 'Accept': 'application/vnd.github.v3+json'}
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        conteudo = base64.b64decode(data['content']).decode('utf-8')
        sha = data['sha']
        reader = csv.DictReader(io.StringIO(conteudo))
        historico = list(reader)
        ids_existentes = {row['id'] for row in historico}
        return ids_existentes, historico, sha
    elif resp.status_code == 404:
        return set(), [], None
    else:
        raise Exception(f'Erro GitHub GET: {resp.status_code} - {resp.text[:200]}')

def atualizar_csv(historico, novos, sha):
    todos = historico + novos
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLS)
    writer.writeheader()
    writer.writerows(todos)
    csv_b64 = base64.b64encode(output.getvalue().encode('utf-8')).decode('utf-8')
    body = {'message': f'Monitor: {len(novos)} novo(s) imovel(is) - {now_str}', 'content': csv_b64}
    if sha:
        body['sha'] = sha
    url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{CSV_FILE}'
    headers = {'Authorization': f'token {GITHUB_PAT}', 'Accept': 'application/vnd.github.v3+json'}
    resp = requests.put(url, json=body, headers=headers)
    if resp.status_code not in (200, 201):
        raise Exception(f'Erro GitHub PUT: {resp.status_code} - {resp.text[:200]}')
    print(f'[OK] CSV atualizado com {len(novos)} novo(s).')

def enviar_email(novos):
    subject = f'\U0001f3e0 [Alerta Imóveis] {len(novos)} novo(s) disponível(is) — SQS 308, Asa Sul'
    cards_html = ''
    for im in novos:
        cards_html += (
            f'<div style="background:#fff;border:1px solid #e0e6ed;border-radius:10px;padding:20px;margin-bottom:16px;">'
            f'<div style="font-size:17px;font-weight:700;color:#1a3a5c;margin-bottom:8px;">{im["bloco"]}</div>'
            f'<div style="font-size:26px;font-weight:800;color:#1a8a4a;margin-bottom:10px;">{im["aluguel"]}</div>'
            f'<div style="color:#555;font-size:14px;margin-bottom:12px;">'
            f'\U0001f4d0 {im["area"]} · \U0001f6cf {im["quartos"]} · \U0001f6c1 {im["suites"]} · \U0001f697 {im["vagas"]} · '
            f'<span style="color:#888;">{im["valor_m2"]}</span></div>'
            f'<a href="{im["link"]}" style="display:inline-block;background:#1a3a5c;color:#fff;text-decoration:none;'
            f'padding:10px 22px;border-radius:6px;font-size:14px;font-weight:600;">Ver Anúncio →</a></div>'
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
        f'<div style="margin-top:24px;"><div style="font-size:15px;font-weight:700;color:#1a3a5c;margin-bottom:10px;">'
        f'Resumo Comparativo</div><table style="width:100%;border-collapse:collapse;font-size:13px;">'
        f'<thead><tr style="background:#1a3a5c;color:#fff;">'
        f'<th style="padding:10px 12px;text-align:left;">Endereço</th>'
        f'<th style="padding:10px 12px;text-align:left;">Aluguel</th>'
        f'<th style="padding:10px 12px;text-align:left;">&Aacute;rea</th>'
        f'<th style="padding:10px 12px;text-align:left;">Quartos</th>'
        f'<th style="padding:10px 12px;text-align:left;">R$/m²</th>'
        f'</tr></thead><tbody>{linhas}</tbody></table></div>'
    ) if len(novos) > 1 else ''
    html = (
        f'<div style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;background:#f4f6f9;padding:20px;">'
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
        f'color:#888;font-size:12px;">Monitoramento automático · DFImóveis · SQS 308 · Execução a cada hora</div></div>'
    )
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = GMAIL_USER
    msg['To'] = NOTIFY_EMAIL
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.send_message(msg)
    print(f'[OK] Email enviado para {NOTIFY_EMAIL}.')

try:
    imoveis_atuais = scrape()
    print(f'[INFO] {len(imoveis_atuais)} anúncios encontrados no site.')
    ids_existentes, historico, sha = ler_historico()
    print(f'[INFO] Histórico: {len(ids_existentes)} imóveis registrados.')
    novos = [im for im in imoveis_atuais if im['id'] not in ids_existentes]
    print(f'[INFO] Novos imóveis detectados: {len(novos)}')
    if novos:
        atualizar_csv(historico, novos, sha)
        enviar_email(novos)
    else:
        print(f'[MONITOR SQS-308] {now_str} — Sem novidades. Histórico: {len(ids_existentes)} imóveis registrados.')
except Exception as e:
    print(f'[ERRO CRÍTICO] {datetime.now().strftime("%Y-%m-%d %H:%M")} — {e}')
    raise
