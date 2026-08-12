#!/usr/bin/env python3
"""Testes do monitor. Rode com: python -m unittest -v test_monitor

Nao acessam a rede: o parsing e exercitado sobre HTML de fixture. Servem para
pegar regressao no parser, que e a parte mais fragil (o layout do site muda).
"""

import csv
import os
import tempfile
import unittest

from bs4 import BeautifulSoup

import monitor

SLUG = 'apartamento-3-quartos-aluguel-asa-sul-brasilia-df-sqs-308-bloco-g-727057'
URL = f'/imovel/{SLUG}'


def card(html):
    """Extrai um anuncio a partir de um trecho de HTML com um unico <a>."""
    a = BeautifulSoup(html, 'html.parser').find('a')
    return monitor.extrair(a, a['href'])


class TestIdDe(unittest.TestCase):
    def test_url_simples(self):
        self.assertEqual(monitor.id_de(URL), '727057')

    def test_ignora_query_string(self):
        self.assertEqual(monitor.id_de(URL + '?utm_source=email'), '727057')

    def test_ignora_fragmento(self):
        self.assertEqual(monitor.id_de(URL + '#fotos'), '727057')

    def test_ignora_barra_final(self):
        self.assertEqual(monitor.id_de(URL + '/'), '727057')

    def test_url_absoluta(self):
        self.assertEqual(monitor.id_de(monitor.BASE + URL), '727057')

    def test_sem_id(self):
        self.assertEqual(monitor.id_de('/imovel/pagina-sem-id'), '')


class TestBlocoFromSlug(unittest.TestCase):
    def test_quadra_com_bloco(self):
        self.assertEqual(monitor.bloco_from_slug(URL), 'SQS 308 Bloco G')

    def test_quadra_sem_bloco(self):
        self.assertEqual(
            monitor.bloco_from_slug('/imovel/apto-aluguel-df-sqs-308-1032819'),
            'SQS 308')

    def test_siglas_com_w_ficam_maiusculas(self):
        # 'sqsw'/'sqnw' precisam vir antes de 'sqs'/'sqn' na alternancia.
        self.assertEqual(
            monitor.bloco_from_slug('/imovel/apto-df-sqsw-305-bloco-a-1'),
            'SQSW 305 Bloco A')
        self.assertEqual(
            monitor.bloco_from_slug('/imovel/apto-df-sqnw-107-bloco-b-2'),
            'SQNW 107 Bloco B')

    def test_siglas_presentes_em_links_txt(self):
        casos = {
            '/imovel/casa-df-shigs-706-bloco-c-1': 'SHIGS 706 Bloco C',
            '/imovel/casa-df-shcgn-712-bloco-a-2': 'SHCGN 712 Bloco A',
            '/imovel/apto-df-eqs-114-115-3': 'EQS 114 115',
            '/imovel/apto-df-ccsw-4-4': 'CCSW 4',
        }
        for url, esperado in casos.items():
            self.assertEqual(monitor.bloco_from_slug(url), esperado, url)

    def test_conjunto(self):
        self.assertEqual(
            monitor.bloco_from_slug('/imovel/casa-df-qi-9-conjunto-3-1'),
            'QI 9 Conjunto 3')

    def test_sem_quadra_reconhecida(self):
        # Nao deve estourar; devolve o slug legivel.
        self.assertEqual(
            monitor.bloco_from_slug('/imovel/loja-comercial-taguatinga-99'),
            'Loja Comercial Taguatinga')


class TestPrecoAluguel(unittest.TestCase):
    def test_valor_simples(self):
        self.assertEqual(monitor.preco_aluguel('R$ 5.900,00'), 'R$ 5.900,00')

    def test_ignora_condominio(self):
        self.assertEqual(
            monitor.preco_aluguel('Condomínio R$ 1.200 Aluguel R$ 5.900,00'),
            'R$ 5.900,00')

    def test_ignora_iptu(self):
        self.assertEqual(
            monitor.preco_aluguel('IPTU: R$ 300 R$ 4.000'), 'R$ 4.000')

    def test_ignora_valor_m2(self):
        self.assertEqual(
            monitor.preco_aluguel('Valor m²: R$ 49,16 R$ 5.900'), 'R$ 5.900')

    def test_sem_valor(self):
        self.assertEqual(monitor.preco_aluguel('Consulte-nos'), '')


class TestExtrair(unittest.TestCase):
    def test_campos_completos(self):
        d = card(f'<a href="{URL}"><div><h2>SQS 308 Bloco G - Asa Sul</h2>'
                 '<div>R$ 5.900,00</div>'
                 '<div>120 m² · 3 Quartos · 1 Suíte · 1 Vaga</div>'
                 '<div>Valor m²: R$ 49,16</div></div></a>')
        self.assertEqual(d['id'], '727057')
        self.assertEqual(d['bloco'], 'SQS 308 Bloco G - Asa Sul')
        self.assertEqual(d['aluguel'], 'R$ 5.900,00')
        self.assertEqual(d['area'], '120')
        self.assertEqual(d['quartos'], '3')
        self.assertEqual(d['suites'], '1')
        self.assertEqual(d['vagas'], '1')
        self.assertEqual(d['valor_m2'], 'Valor m²: R$ 49,16')
        self.assertEqual(d['link'], monitor.BASE + URL)

    def test_titulo_generico_usa_endereco_da_url(self):
        d = card(f'<a href="{URL}"><div><h2>Apartamento</h2>'
                 '<div>R$ 5.900,00</div></div></a>')
        self.assertEqual(d['bloco'], 'SQS 308 Bloco G')

    def test_sem_titulo_usa_endereco_da_url(self):
        d = card(f'<a href="{URL}"><div>R$ 7.000 · 90 m² · 2 Quartos</div></a>')
        self.assertEqual(d['bloco'], 'SQS 308 Bloco G')
        self.assertEqual(d['aluguel'], 'R$ 7.000')
        self.assertEqual(d['area'], '90')

    def test_valor_por_m2_nao_virou_area(self):
        d = card(f'<a href="{URL}"><div><h2>SQS 308 Bloco G</h2>'
                 '<div>R$ 5.900,00</div><div>Valor m² R$ 49,16</div></div></a>')
        self.assertEqual(d['aluguel'], 'R$ 5.900,00')
        self.assertNotEqual(d['area'], '49,16')

    def test_condominio_nao_virou_aluguel(self):
        d = card(f'<a href="{URL}"><div><h2>SQS 308 Bloco G</h2>'
                 '<div>Condomínio R$ 1.200</div>'
                 '<div>Aluguel R$ 5.900,00</div></div></a>')
        self.assertEqual(d['aluguel'], 'R$ 5.900,00')

    def test_link_sem_query_string(self):
        d = card(f'<a href="{URL}?utm_source=email"><div>R$ 5.900</div></a>')
        self.assertEqual(d['link'], monitor.BASE + URL)


class TestParsePagina(unittest.TestCase):
    """O bug mais grave que este arquivo protege: quando o container escolhido
    e a lista em vez do card, todos os anuncios saem com os dados do primeiro.
    """

    LAYOUT_A = """<div class="lista">
      <a href="/imovel/apto-aluguel-asa-sul-df-sqs-308-bloco-g-727057">
        <div class="card"><h2>SQS 308 Bloco G</h2><div>R$ 5.900,00</div>
        <div>120 m² · 3 Quartos · 1 Vaga</div></div></a>
      <a href="/imovel/apto-aluguel-asa-sul-df-sqs-308-bloco-a-718800/">
        <div class="card"><h2>SQS 308 Bloco A</h2><div>R$ 12.000</div>
        <div>200 m² · 4 Quartos · 2 Vagas</div></div></a>
    </div>"""

    LAYOUT_B = """<div class="lista">
      <div class="card"><h2><a href="/imovel/apto-df-sqs-308-bloco-c-977948">
        SQS 308 Bloco C</a></h2>
        <div>R$ 7.000,00</div><div>90 m² · 2 Quartos · 1 Vaga</div></div>
      <div class="card"><h2><a href="/imovel/apto-df-sqs-308-bloco-i-959199">
        SQS 308 Bloco I</a></h2>
        <div>R$ 4.500,00</div><div>75 m² · 2 Quartos</div></div>
    </div>"""

    def test_layout_a_cada_anuncio_com_seus_dados(self):
        r = monitor.parse_pagina(self.LAYOUT_A)
        self.assertEqual([x['id'] for x in r], ['727057', '718800'])
        self.assertEqual([x['aluguel'] for x in r], ['R$ 5.900,00', 'R$ 12.000'])
        self.assertEqual([x['area'] for x in r], ['120', '200'])

    def test_layout_b_dados_irmaos_do_link(self):
        r = monitor.parse_pagina(self.LAYOUT_B)
        self.assertEqual([x['id'] for x in r], ['977948', '959199'])
        self.assertEqual([x['aluguel'] for x in r], ['R$ 7.000,00', 'R$ 4.500,00'])
        self.assertEqual([x['area'] for x in r], ['90', '75'])

    def test_deduplica_e_ignora_links_que_nao_sao_anuncio(self):
        html = self.LAYOUT_A + (
            f'<a href="{URL}">1</a><a href="{URL}?x=1">dup</a>'
            '<a href="/imovel/sem-id">x</a><a href="/sobre">x</a>')
        ids = [x['id'] for x in monitor.parse_pagina(html)]
        self.assertEqual(ids, ['727057', '718800'])

    def test_anuncio_com_query_string_nao_e_perdido(self):
        html = f'<div><a href="{URL}?utm_source=email"><div><h2>SQS 308</h2>' \
               '<div>R$ 5.900</div></div></a></div>'
        self.assertEqual([x['id'] for x in monitor.parse_pagina(html)],
                         ['727057'])

    def test_pagina_vazia(self):
        self.assertEqual(monitor.parse_pagina('<html><body></body></html>'), [])


class TestHistorico(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self._orig = monitor.CSV_FILE
        monitor.CSV_FILE = os.path.join(self.dir.name, 'h.csv')

    def tearDown(self):
        monitor.CSV_FILE = self._orig

    @staticmethod
    def _imovel(ident):
        return {'id': ident, 'bloco': f'SQS 308 {ident}', 'aluguel': 'R$ 1',
                'valor_m2': '', 'area': '', 'quartos': '', 'suites': '',
                'vagas': '', 'link': 'x', 'data_descoberta': 'hoje'}

    def test_historico_inexistente(self):
        self.assertEqual(monitor.ler_historico(), (set(), []))

    def test_grava_le_e_deduplica(self):
        monitor.gravar_historico([], [self._imovel('111')])
        ids, hist = monitor.ler_historico()
        self.assertEqual(ids, {'111'})

        atuais = [self._imovel('111'), self._imovel('222')]
        novos = [i for i in atuais if i['id'] not in ids]
        self.assertEqual([n['id'] for n in novos], ['222'])

        monitor.gravar_historico(hist, novos)
        ids2, hist2 = monitor.ler_historico()
        self.assertEqual(ids2, {'111', '222'})
        self.assertEqual(len(hist2), 2)

    def test_colunas_preservadas(self):
        monitor.gravar_historico([], [self._imovel('111')])
        with open(monitor.CSV_FILE, encoding='utf-8') as fh:
            self.assertEqual(next(csv.reader(fh)), monitor.CSV_COLS)

    def test_campo_ausente_nao_estoura(self):
        monitor.gravar_historico([], [{'id': '333'}])
        ids, _ = monitor.ler_historico()
        self.assertEqual(ids, {'333'})


class TestValidarConfig(unittest.TestCase):
    """Sem poder avisar, o monitor nao deve gravar o historico -- gravar
    marcaria os imoveis como vistos e o alerta seria perdido para sempre.
    """

    def setUp(self):
        self._orig = (monitor.GMAIL_USER, monitor.GMAIL_APP_PASSWORD,
                      monitor.NOTIFY_EMAIL)

    def tearDown(self):
        (monitor.GMAIL_USER, monitor.GMAIL_APP_PASSWORD,
         monitor.NOTIFY_EMAIL) = self._orig

    def test_falta_senha(self):
        monitor.GMAIL_USER = monitor.NOTIFY_EMAIL = 'a@b.com'
        monitor.GMAIL_APP_PASSWORD = ''
        with self.assertRaises(SystemExit) as ctx:
            monitor.validar_config()
        self.assertIn('GMAIL_APP_PASSWORD', str(ctx.exception))

    def test_config_completa(self):
        monitor.GMAIL_USER = monitor.NOTIFY_EMAIL = 'a@b.com'
        monitor.GMAIL_APP_PASSWORD = 'senha'
        monitor.validar_config()


class TestRenderEmail(unittest.TestCase):
    def test_campos_vazios_nao_estouram(self):
        vazio = {c: '' for c in monitor.CSV_COLS}
        html = monitor.render_email([vazio])
        self.assertIn('Valor não informado', html)

    def test_inclui_link_e_valor(self):
        im = dict.fromkeys(monitor.CSV_COLS, '')
        im.update(aluguel='R$ 5.900', bloco='SQS 308 Bloco G',
                  link='https://x/y', area='120')
        html = monitor.render_email([im])
        self.assertIn('https://x/y', html)
        self.assertIn('R$ 5.900', html)
        self.assertIn('120 m²', html)


class TestLinksReais(unittest.TestCase):
    """Regressao contra as URLs realmente coletadas do site."""

    def test_extrai_id_de_todas_as_urls_de_links_txt(self):
        if not os.path.exists('links.txt'):
            self.skipTest('links.txt ausente')
        with open('links.txt', encoding='utf-8') as fh:
            urls = [l.strip() for l in fh if l.strip()]
        self.assertGreater(len(urls), 400)
        sem_id = [u for u in urls if not monitor.id_de(u)]
        self.assertEqual(sem_id, [], f'{len(sem_id)} URLs sem id')

    def test_endereco_das_urls_sqs_308(self):
        if not os.path.exists('links.txt'):
            self.skipTest('links.txt ausente')
        with open('links.txt', encoding='utf-8') as fh:
            for url in (l.strip() for l in fh):
                if 'sqs-308' in url:
                    self.assertTrue(
                        monitor.bloco_from_slug(url).startswith('SQS 308'),
                        f'{url} -> {monitor.bloco_from_slug(url)}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
