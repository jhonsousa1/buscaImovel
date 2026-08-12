#!/usr/bin/env python3
"""Monitor de imoveis para aluguel no DFImoveis.

Roda no GitHub Actions. Compara os anuncios encontrados na URL alvo com o
historico em imoveis_historico.csv, envia email para os novos e grava o CSV
atualizado (o commit e feito pelo workflow).

Configuracao via variaveis de ambiente:
  URL_ALVO             URL de busca (tem default)
  CSV_FILE             caminho do historico (default: imoveis_historico.csv)
  GMAIL_USER           remetente
  GMAIL_APP_PASSWORD   senha de app do Gmail (secret)
  NOTIFY_EMAIL         destinatario (default: GMAIL_USER)
  DRY_RUN=1            so imprime o resultado: nao grava CSV nem envia email

Falha com codigo != 0 -- em vez de reportar "sem novidades" -- quando o
scraping nao encontra nenhum anuncio ou quando falta configuracao de email,
para que o problema apareca como falha no Actions.
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

# Siglas de endereco de Brasilia. As presentes em links.txt (sqn, sqs, shigs,
# shcgn, sqsw, eqs, sqnw, cln, ccsw) mais as equivalentes previsiveis.
QUADRAS = frozenset((
    'sqs', 'sqn', 'sqsw', 'sqnw', 'sqi',
    'shigs', 'shin', 'shis', 'shcgn', 'shcgs', 'shtn',
    'cln', 'cls', 'clnw', 'clsw', 'ccsw',
    'eqs', 'eqn', 'scrn', 'scrs', 'sca', 'srtvs', 'smdb',
    'qi', 'qe', 'ql', 'qmsw',
))
# Ordenado do mais longo para o mais curto: sem isso a alternancia casaria
# 'sqs' dentro de 'sqsw' e o endereco sairia errado.
_ALT = '|'.join(sorted(QUADRAS, key=len, reverse=True))
QUADRA_NO_SLUG = re.compile(rf'(?:^|-)((?:{_ALT})-[\w-]*)$', re.IGNORECASE)
QUADRA_NO_TEXTO = re.compile(rf'\b(?:{_ALT})\b', re.IGNORECASE)

ID_NO_FIM = re.compile(r'-(\d+)$')
PRECO = re.compile(r'R\$\s*[\d.]+(?:,\d{2})?')
# Rotulos que aparecem antes de um valor que NAO e o aluguel.
NAO_ALUGUEL = re.compile(
    r'(?:condom[íi]nio|cond\.|iptu|taxa|seguro|valor\s*m²?|/\s*m²)'
    r'\s*:?\s*(?:de\s*)?$',
    re.IGNORECASE,
)

now_str = datetime.now().strftime('%Y-%m-%d %H:%M')


def limpar_href(href):
    """Remove query string, fragmento e barra final."""
    return (href or '').split('?')[0].split('#')[0].rstrip('/')


def id_de(href):
    """Id numerico no fim da URL do anuncio, ou '' se nao houver."""
    m = ID_NO_FIM.search(limpar_href(href))
    return m.group(1) if m else ''


def bloco_from_slug(href):
    """Extrai um endereco legivel do slug da URL.

    .../apartamento-3-quartos-aluguel-asa-sul-brasilia-df-sqs-308-bloco-g-727057
    -> 'SQS 308 Bloco G'
    """
    slug = re.sub(r'-\d+$', '', limpar_href(href).split('/')[-1])
    m = QUADRA_NO_SLUG.search(slug)
    trecho = m.group(1) if m else slug

    palavras = []
    for p in trecho.split('-'):
        if p.lower() in QUADRAS or p.isdigit() or len(p) <= 2:
            palavras.append(p.upper())
        else:
            palavras.append(p.title())
    return ' '.join(palavras)


def first(pattern, texto, grupo=0):
    m = re.search(pattern, texto, re.IGNORECASE)
    return m.group(grupo).strip() if m else ''


def preco_aluguel(texto):
    """Primeiro valor em reais que nao esteja rotulado como condominio, IPTU
    ou valor por metro quadrado."""
    for m in PRECO.finditer(texto):
        if not NAO_ALUGUEL.search(texto[max(0, m.start() - 30):m.start()]):
            return m.group(0).strip()
    return ''


def ids_em(no):
    """Ids de anuncio distintos encontrados dentro de um elemento."""
    return {i for i in (id_de(a.get('href', ''))
                        for a in no.select('a[href*="/imovel/"]')) if i}


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
    href = limpar_href(href)
    cont = container_de(anchor)
    texto = re.sub(r'\s+', ' ', cont.get_text(' ', strip=True))

    # O valor por m² e removido do texto antes de procurar aluguel e area,
    # senao 'R$ 49,16/m²' seria lido como preco ou como metragem.
    valor_m2 = first(r'Valor\s*m²?[^R]*R\$\s*[\d.,]+', texto)
    resto = texto.replace(valor_m2, ' ') if valor_m2 else texto

    # Um titulo que nao cita a quadra ('Apartamento', 'Oportunidade') informa
    # menos que o endereco embutido no slug da URL.
    h = cont.find(['h2', 'h3'])
    titulo = h.get_text(' ', strip=True) if h else ''
    endereco = bloco_from_slug(href)
    bloco = titulo if QUADRA_NO_TEXTO.search(titulo) else (endereco or titulo)

    return {
        'id': id_de(href),
        'bloco': bloco,
        'aluguel': preco_aluguel(resto),
        'valor_m2': valor_m2,
        'area': first(r'(\d+(?:[.,]\d+)?)\s*m²', resto, 1),
        'quartos': first(r'(\d+)\s*(?:Quarto|Dorm)', resto, 1),
        'suites': first(r'(\d+)\s*Su[íi]te', resto, 1),
        'vagas': first(r'(\d+)\s*Vaga', resto, 1),
        'link': href if href.startswith('http') else BASE + href,
        'data_descoberta': now_str,
    }


def parse_pagina(html):
    """Extrai os anuncios de uma pagina de busca.

    Nao depende de nenhuma classe CSS: ancora nos links /imovel/...-<id>, que
    e a parte mais estavel do HTML do site.
    """
    soup = BeautifulSoup(html, 'html.parser')
    imoveis, vistos = [], set()
    for a in soup.select('a[href*="/imovel/"]'):
        href = limpar_href(a.get('href', ''))
        ident = id_de(href)
        if not ident or ident in vistos:
            continue
        vistos.add(ident)
        imoveis.append(extrair(a, href))
    return imoveis


def scrape():
    ultimo_erro = None
    for i, ua in enumerate(USER_AGENTS):
        try:
            resp = requests.get(
                URL_ALVO,
                headers={'User-Agent': ua, 'Accept-Language': 'pt-BR,pt;q=0.9'},
                timeout=30,
            )
            resp.raise_for_status()
            imoveis = parse_pagina(resp.text)
            if imoveis:
                return imoveis
            ultimo_erro = (f'nenhum link /imovel/ no HTML '
                           f'({len(resp.text)} bytes recebidos)')
        except Exception as e:  # noqa: BLE001 - tentar o proximo User-Agent
            ultimo_erro = e
        print(f'[AVISO] tentativa {i + 1} sem resultado: {ultimo_erro}')
        if i + 1 < len(USER_AGENTS):
            time.sleep(10)

    raise SystemExit(
        f'[FALHA] scraping nao retornou anuncios. Ultimo motivo: {ultimo_erro}. '
        'Verifique se o layout do site mudou, se a busca ficou sem resultados '
        'ou se ha bloqueio de bot.'
    )


def validar_config():
    faltando = [nome for nome, valor in (
        ('GMAIL_USER (variable)', GMAIL_USER),
        ('GMAIL_APP_PASSWORD (secret)', GMAIL_APP_PASSWORD),
        ('NOTIFY_EMAIL (variable)', NOTIFY_EMAIL),
    ) if not valor]
    if faltando:
        raise SystemExit(
            '[FALHA] configuracao de email ausente: ' + ', '.join(faltando) +
            '. Configure no repositorio (veja o README) ou rode com DRY_RUN=1. '
            'Abortando ANTES de gravar o historico: gravar sem conseguir avisar '
            'marcaria os imoveis como vistos e o alerta seria perdido.'
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
        detalhes = ' · '.join(p for p in (
            f'📐 {im["area"]} m²' if im['area'] else '',
            f'🛏 {im["quartos"]} quartos' if im['quartos'] else '',
            f'🛁 {im["suites"]} suítes' if im['suites'] else '',
            f'🚗 {im["vagas"]} vagas' if im['vagas'] else '',
        ) if p)
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
    dry_run = os.environ.get('DRY_RUN') == '1'
    if not dry_run:
        validar_config()

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

    if dry_run:
        print('[DRY_RUN] nada gravado, nenhum email enviado.')
        return

    # Email primeiro: se o envio falhar, o historico nao e gravado e o imovel
    # volta a ser detectado como novo na proxima execucao.
    enviar_email(novos)
    gravar_historico(historico, novos)


if __name__ == '__main__':
    sys.exit(main())
