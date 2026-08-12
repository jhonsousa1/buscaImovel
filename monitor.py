#!/usr/bin/env python3
"""Monitor de imoveis para aluguel no DFImoveis.

Roda no GitHub Actions. Compara os anuncios encontrados na URL alvo com o
historico em imoveis_historico.csv, envia email para os novos e grava o CSV
atualizado (o commit e feito pelo workflow).

Configuracao via variaveis de ambiente:
  URL_ALVO             URL de busca (tem default)
  GMAIL_USER           remetente
  GMAIL_APP_PASSWORD   senha de app do Gmail (secret)
  NOTIFY_EMAIL         destinatario (default: GMAIL_USER)

Sai com codigo != 0 quando o scraping nao encontra nenhum anuncio, para que
uma quebra de seletor apareca como falha no Actions em vez de virar um
silencioso "sem novidades".
"""

import csv
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

BASE = 'https://www.dfimoveis.com.br'
URL_ALVO = os.environ.get(
    'URL_ALVO',
    f'{BASE}/aluguel/df/todos/imoveis?palavrachave=sqs-308&vagasdegaragem=1',
)
CSV_FILE = os.environ.get('CSV_FILE', 'imoveis_historico.csv')
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL') or GMAIL_USER

CSV_COLS = ['id', 'bloco', 'aluguel', 'valor_m2', 'area', 'quartos',
            'suites', 'vagas', 'link', 'data_descoberta']

USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
]

now_str = datetime.now().strftime('%Y-%m-%d %H:%M')


def bloco_from_slug(href):
    """Extrai um endereco legivel do slug da URL.

    .../apartamento-3-quartos-aluguel-asa-sul-brasilia-df-sqs-308-bloco-g-727057
    -> 'SQS 308 Bloco G'
    """
    slug = href.rstrip('/').split('/')[-1]
    slug = re.sub(r'-\d+$', '', slug)
    m = re.search(
        r'((?:sqs|sqn|sqsw|sqnw|shigs|shin|sqi|qi|qe|cln|cls|sca|srtvs)'
        r'-[\w-]*)$',
        slug,
    )
    if not m:
        return slug.replace('-', ' ').title()
    partes = m.group(1).split('-')
    out = []
    for p in partes:
        out.append(p.upper() if len(p) <= 2 or p.isdigit() else p.title())
    texto = ' '.join(out)
    return re.sub(r'\b(Sqs|Sqn|Shigs|Shin|Sqi|Cln|Cls|Sca|Srtvs)\b',
                  lambda mm: mm.group(1).upper(), texto)


def first(pattern, texto, grupo=0):
    m = re.search(pattern, texto, re.IGNORECASE)
    return m.group(grupo).strip() if m else ''


def ids_em(no):
    """Ids de anuncio distintos encontrados dentro de um elemento."""
    achados = set()
    for a in no.select('a[href*="/imovel/"]'):
        m = re.search(r'-(\d+)$', a.get('href', '').rstrip('/'))
        if m:
            achados.add(m.group(1))
    return achados


def container_de(anchor):
    """Menor elemento que contem os dados do anuncio.

    Comeca no proprio <a> (no site o card costuma ficar dentro do link) e sobe
    apenas enquanto o elemento continuar descrevendo um unico anuncio: um
    container com mais de um link /imovel/ e a lista, nao o card -- subir ate
    ele faria todos os anuncios lerem os mesmos dados.
    """
    melhor = anchor
    no = anchor
    for _ in range(4):
        if 'R$' in melhor.get_text(' ', strip=True):
            break
        no = no.parent
        if no is None or no.name in ('body', 'html', '[document]'):
            break
        if len(ids_em(no)) > 1:
            break
        melhor = no
    return melhor


def extrair(anchor, href):
    cont = container_de(anchor)
    texto = re.sub(r'\s+', ' ', cont.get_text(' ', strip=True))

    h = cont.find(['h2', 'h3'])
    bloco = h.get_text(strip=True) if h else ''
    if not bloco or len(bloco) < 4:
        bloco = bloco_from_slug(href)

    valor_m2 = first(r'Valor\s*m²?[^R]*R\$\s*[\d.,]+', texto)
    aluguel = ''
    for m in re.finditer(r'R\$\s*[\d.]+(?:,\d{2})?', texto):
        if m.group(0) not in valor_m2:
            aluguel = m.group(0).strip()
            break

    return {
        'id': re.search(r'-(\d+)$', href).group(1),
        'bloco': bloco,
        'aluguel': aluguel,
        'valor_m2': valor_m2,
        'area': first(r'(\d+(?:[.,]\d+)?)\s*m²', texto, 1),
        'quartos': first(r'(\d+)\s*(?:Quarto|Dorm)', texto, 1),
        'suites': first(r'(\d+)\s*Su[íi]te', texto, 1),
        'vagas': first(r'(\d+)\s*Vaga', texto, 1),
        'link': href if href.startswith('http') else BASE + href,
        'data_descoberta': now_str,
    }


def scrape():
    """Coleta os anuncios da pagina de busca.

    Nao depende de uma classe CSS especifica: ancora nos links /imovel/...-<id>,
    que e a parte mais estavel do HTML do site.
    """
    ultimo_erro = None
    for i, ua in enumerate(USER_AGENTS):
        try:
            resp = requests.get(
                URL_ALVO,
                headers={
                    'User-Agent': ua,
                    'Accept-Language': 'pt-BR,pt;q=0.9',
                },
                timeout=30,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')

            imoveis, vistos = [], set()
            for a in soup.select('a[href*="/imovel/"]'):
                href = a.get('href', '')
                if not re.search(r'-(\d+)$', href.rstrip('/')):
                    continue
                href = href.rstrip('/')
                dados = extrair(a, href)
                if dados['id'] in vistos:
                    continue
                vistos.add(dados['id'])
                imoveis.append(dados)

            if imoveis:
                return imoveis
            ultimo_erro = 'nenhum link /imovel/ encontrado no HTML'
            print(f'[AVISO] tentativa {i + 1}: {ultimo_erro} '
                  f'({len(resp.text)} bytes recebidos)')
        except Exception as e:  # noqa: BLE001 - queremos tentar o proximo UA
            ultimo_erro = e
            print(f'[ERRO] tentativa {i + 1}: {e}')
        if i + 1 < len(USER_AGENTS):
            time.sleep(10)

    raise SystemExit(
        f'[FALHA] scraping nao retornou anuncios. Ultimo motivo: {ultimo_erro}. '
        'Verifique se o layout do site mudou ou se ha bloqueio de bot.'
    )


def ler_historico():
    if not os.path.exists(CSV_FILE):
        return set(), []
    with open(CSV_FILE, newline='', encoding='utf-8') as fh:
        historico = list(csv.DictReader(fh))
    return {row['id'] for row in historico if row.get('id')}, historico


def gravar_historico(historico, novos):
    with open(CSV_FILE, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLS)
        writer.writeheader()
        for row in historico + novos:
            writer.writerow({c: row.get(c, '') for c in CSV_COLS})
    print(f'[OK] {CSV_FILE} atualizado (+{len(novos)}).')


def render_email(novos):
    cards = ''
    for im in novos:
        detalhes = ' · '.join(
            p for p in (
                f'📐 {im["area"]} m²' if im['area'] else '',
                f'🛏 {im["quartos"]} quartos' if im['quartos'] else '',
                f'🛁 {im["suites"]} suítes' if im['suites'] else '',
                f'🚗 {im["vagas"]} vagas' if im['vagas'] else '',
            ) if p
        )
        cards += (
            '<div style="background:#fff;border:1px solid #e0e6ed;'
            'border-radius:10px;padding:20px;margin-bottom:16px;">'
            f'<div style="font-size:17px;font-weight:700;color:#1a3a5c;'
            f'margin-bottom:8px;">{im["bloco"]}</div>'
            f'<div style="font-size:26px;font-weight:800;color:#1a8a4a;'
            f'margin-bottom:10px;">{im["aluguel"] or "Valor não informado"}</div>'
            f'<div style="color:#555;font-size:14px;margin-bottom:12px;">'
            f'{detalhes} <span style="color:#888;">{im["valor_m2"]}</span></div>'
            f'<a href="{im["link"]}" style="display:inline-block;'
            'background:#1a3a5c;color:#fff;text-decoration:none;'
            'padding:10px 22px;border-radius:6px;font-size:14px;'
            'font-weight:600;">Ver Anúncio →</a></div>'
        )

    return (
        '<div style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;'
        'background:#f4f6f9;padding:20px;">'
        '<div style="background:#1a3a5c;border-radius:10px 10px 0 0;'
        'padding:28px 24px;text-align:center;">'
        '<div style="font-size:28px;margin-bottom:6px;">🏠</div>'
        '<div style="color:#fff;font-size:22px;font-weight:800;">'
        'Alerta de Novos Imóveis</div>'
        '<div style="color:#a8c4e0;font-size:14px;margin-top:6px;">'
        'SQS 308 · Asa Sul · Brasília/DF</div></div>'
        '<div style="background:#fff;padding:24px;border-left:1px solid #e0e6ed;'
        'border-right:1px solid #e0e6ed;">'
        '<div style="color:#555;font-size:13px;margin-bottom:20px;'
        'padding:10px 14px;background:#f0f7ff;border-radius:6px;'
        'border-left:4px solid #1a3a5c;">'
        f'📅 Verificado em {now_str} · {len(novos)} novo(s) imóvel(is)</div>'
        f'{cards}</div>'
        '<div style="background:#e8edf2;border-radius:0 0 10px 10px;'
        'padding:14px 24px;text-align:center;color:#888;font-size:12px;">'
        'Monitoramento automático · DFImóveis · SQS 308</div></div>'
    )


def enviar_email(novos):
    if not GMAIL_APP_PASSWORD:
        print('[AVISO] GMAIL_APP_PASSWORD ausente - email nao enviado.')
        return
    msg = MIMEMultipart('alternative')
    msg['Subject'] = (f'🏠 [Alerta Imóveis] {len(novos)} novo(s) '
                      'disponível(is) — SQS 308, Asa Sul')
    msg['From'] = GMAIL_USER
    msg['To'] = NOTIFY_EMAIL
    msg.attach(MIMEText(render_email(novos), 'html', 'utf-8'))
    with smtplib.SMTP('smtp.gmail.com', 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)
    print(f'[OK] Email enviado para {NOTIFY_EMAIL}.')


def main():
    atuais = scrape()
    print(f'[INFO] {len(atuais)} anuncios encontrados.')

    ids, historico = ler_historico()
    print(f'[INFO] Historico: {len(ids)} imoveis registrados.')

    novos = [im for im in atuais if im['id'] not in ids]
    print(f'[INFO] Novos: {len(novos)}')

    if not novos:
        print(f'[MONITOR SQS-308] {now_str} - sem novidades.')
        return

    for im in novos:
        print(f'  + {im["id"]} {im["bloco"]} {im["aluguel"]}')
    gravar_historico(historico, novos)
    enviar_email(novos)


if __name__ == '__main__':
    sys.exit(main())
