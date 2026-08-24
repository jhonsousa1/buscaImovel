import base64, csv, io, smtplib, re, os, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
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
CC_EMAIL = 'mestter21@gmail.com'
CSV_COLS = ['id','bloco','aluguel','valor_m2','area','quartos','suites','vagas','link','data_descoberta']
now_str = datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%Y-%m-%d %H:%M')

# Execucoes fora da main rodam sem efeito colateral: nao envia email nem grava CSV.
DRY_RUN = os.environ.get('DRY_RUN', '') not in ('', '0', 'false')
DEBUG_CARDS = os.environ.get('DEBUG_CARDS', '') not in ('', '0', 'false')
BASE_URL = 'https://www.dfimoveis.com.br'


def _urlize(href):
    """Normaliza um href relativo ou absoluto para URL completa."""
    href = (href or '').strip()
    if not href:
        return ''
    if href.startswith('http'):
        return href
    return BASE_URL + ('' if href.startswith('/') else '/') + href


def extrair_href(card):
    """Procura o link do anuncio em varias posicoes possiveis do DOM.

    O layout ja mudou uma vez e quebrou a extracao (a ancora nao era nem pai
    nem filha do card), entao aqui juntamos todos os candidatos e preferimos
    os que apontam para /imovel/.
    """
    candidatos = []

    if card.name == 'a' and card.get('href'):
        candidatos.append(card['href'])

    pai_ancora = card.find_parent('a', href=True)
    if pai_ancora:
        candidatos.append(pai_ancora['href'])

    candidatos.extend(a['href'] for a in card.find_all('a', href=True))

    # Sobe a arvore procurando ancoras irmas/proximas.
    no = card
    for _ in range(5):
        no = getattr(no, 'parent', None)
        if no is None or not getattr(no, 'name', None) or no.name == '[document]':
            break
        if no.name == 'a' and no.get('href'):
            candidatos.append(no['href'])
        candidatos.extend(a['href'] for a in no.find_all('a', href=True, limit=10))

    # Alguns layouts guardam a URL em data-* em vez de <a href>.
    for el in [card] + card.find_parents(limit=3):
        for chave, valor in (getattr(el, 'attrs', {}) or {}).items():
            if chave.startswith('data-') and isinstance(valor, str) and '/imovel/' in valor:
                candidatos.append(valor)

    preferidos = [h for h in candidatos if '/imovel/' in h]
    return (preferidos or candidatos or [''])[0]

def scrape():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        try:
            page.goto(URL_ALVO, timeout=30000, wait_until='domcontentloaded')
            page.wait_for_selector('.imovel-card', timeout=20000)
        except Exception as e:
            print(f'[AVISO] wait_for_selector: {e}')
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, 'html.parser')
    cards = soup.select('.imovel-card')
    print(f'[DEBUG] {len(cards)} cards encontrados no HTML renderizado.')

    imoveis = []
    for indice, card in enumerate(cards):
        href = extrair_href(card)
        link = _urlize(href)

        raw = [t.strip() for t in card.get_text('\n').split('\n') if t.strip()]

        if DEBUG_CARDS and indice < 2:
            print(f'[DIAG] --- card {indice} ---')
            print(f'[DIAG] tag={card.name} attrs={dict(card.attrs)}')
            print(f'[DIAG] pais={[p.name for p in card.find_parents(limit=4)]}')
            print(f'[DIAG] href escolhido={href!r}')
            print(f'[DIAG] tokens brutos={raw}')

        # Mescla tokens separados: ["R$", "3.500"] -> ["R$ 3.500"]
        texts = []
        i = 0
        while i < len(raw):
            if raw[i] == 'R$' and i + 1 < len(raw) and re.match(r'^[\d.,]+$', raw[i + 1]):
                texts.append(f'R$ {raw[i + 1]}')
                i += 2
            else:
                texts.append(raw[i])
                i += 1

        bloco = card.find('h2').get_text(strip=True) if card.find('h2') else ''
        aluguel = next((t for t in texts if t.startswith('R$') and 'Valor' not in t), '')

        # O site quebra o valor por m2 em dois tokens: 'Valor m² R$' e '50'.
        # O 'R$' vem colado no rotulo, entao a mesclagem acima nao pega o numero.
        valor_m2 = ''
        for j, t in enumerate(texts):
            if 'Valor m' in t:
                prox = texts[j + 1] if j + 1 < len(texts) else ''
                if re.search(r'R\$\s*[\d.,]+', t):
                    valor_m2 = t
                elif re.match(r'^[\d.,]+$', prox) or prox.startswith('R$'):
                    valor_m2 = f'{t} {prox}'
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

        # ID do anuncio, estavel mesmo se o preco mudar; hash so como fallback.
        id_legado = hashlib.md5(f'{bloco}|{area}|{quartos}|{aluguel}'.encode()).hexdigest()[:12]
        id_match = re.search(r'-(\d+)(?:[/?]|$)', href)
        imovel_id = id_match.group(1) if id_match else id_legado

        imoveis.append({
            'id': imovel_id, 'bloco': bloco, 'aluguel': aluguel, 'valor_m2': valor_m2,
            'area': area, 'quartos': quartos, 'suites': suites, 'vagas': vagas,
            'link': link, 'data_descoberta': now_str, 'id_legado': id_legado
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
    writer = csv.DictWriter(output, fieldnames=CSV_COLS, extrasaction='ignore')
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
    msg['Cc'] = CC_EMAIL
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.send_message(msg)
    print(f'[OK] Email enviado para {NOTIFY_EMAIL} e copia para {CC_EMAIL}.')

try:
    imoveis_atuais = scrape()
    print(f'[INFO] {len(imoveis_atuais)} anúncios encontrados no site.')
    ids_existentes, historico, sha = ler_historico()
    print(f'[INFO] Histórico: {len(ids_existentes)} imóveis registrados.')
    # Compara tambem pelo hash antigo: as linhas gravadas antes da correcao do
    # link usam esse formato de ID, e sem isso elas seriam realertadas como novas.
    novos = [im for im in imoveis_atuais
             if im['id'] not in ids_existentes and im['id_legado'] not in ids_existentes]
    print(f'[INFO] Novos imóveis detectados: {len(novos)}')
    if novos and DRY_RUN:
        print('[DRY_RUN] Nada foi gravado nem enviado. Novos que seriam notificados:')
        for im in novos:
            print(f"[DRY_RUN]   {im['bloco']} | {im['aluguel']} | {im['area']} "
                  f"| valor_m2={im['valor_m2']!r} | id={im['id']} | link={im['link'] or '(VAZIO)'}")
    elif novos:
        atualizar_csv(historico, novos, sha)
        enviar_email(novos)
    else:
        print(f'[MONITOR SQS-308] {now_str} — Sem novidades. Histórico: {len(ids_existentes)} imóveis registrados.')
except Exception as e:
    print(f'[ERRO CRÍTICO] {datetime.now().strftime("%Y-%m-%d %H:%M")} — {e}')
    raise
