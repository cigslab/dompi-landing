"""Run: python3 -m unittest discover -s tests -v (standard library only).
Static serving uses Python's HTTP handler in memory, not a Vercel emulator.
"""
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler
from io import BytesIO
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://www.dompiapp.id'


class HTML(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.meta, self.canonical, self.links = {}, [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta':
            key = a.get('name', a.get('property', '')).lower()
            self.meta.setdefault(key, []).append(a.get('content', ''))
        if tag == 'link' and a.get('rel') == 'canonical':
            self.canonical.append(a['href'])
        if tag == 'a':
            self.links.append(a.get('href', ''))


class RequestSocket:
    def __init__(self, request):
        self.input = BytesIO(request)
        self.output = BytesIO()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class StaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def static_get(path, method='GET'):
    # Real stdlib handler/file lookup, no network privileges or server process.
    socket = RequestSocket(f'{method} {path} HTTP/1.0\r\nHost: localhost\r\n\r\n'.encode())
    StaticHandler(socket, ('127.0.0.1', 1), object(), directory=str(ROOT))
    headers, body = socket.output.getvalue().split(b'\r\n\r\n', 1)
    return int(headers.split()[1]), headers.decode().lower(), body


class SEOTests(unittest.TestCase):
    def setUp(self):
        self.home = (ROOT / 'index.html').read_text()
        self.html = HTML(self.home)

    def test_sitemap_namespace_and_only_canonical_homepage(self):
        data = (ROOT / 'sitemap.xml').read_bytes()
        self.assertTrue(data.startswith(b'<?xml version="1.0" encoding="UTF-8"?>'))
        root = ET.fromstring(data)
        ns = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
        self.assertEqual(root.tag, ns + 'urlset')
        self.assertEqual([node.tag for node in root], [ns + 'url'])
        self.assertEqual([node.tag for node in root[0]], [ns + 'loc'])
        self.assertEqual([node.text for node in root.iter(ns + 'loc')], [ORIGIN + '/'])

    def test_all_sitemap_targets_resolve_to_indexable_static_html(self):
        for loc in ET.parse(ROOT / 'sitemap.xml').iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc'):
            parsed = urlsplit(loc.text)
            self.assertEqual(parsed.scheme + '://' + parsed.netloc, ORIGIN)
            self.assertFalse(parsed.query or parsed.fragment)
            status, headers, body = static_get(parsed.path)
            self.assertEqual(status, 200)
            self.assertIn('content-type: text/html', headers)
            self.assertNotIn('noindex', headers)
            html = HTML(body.decode())
            self.assertEqual(html.canonical, [loc.text])
            self.assertFalse(any('noindex' in v.lower() for key in ('robots', 'googlebot') for v in html.meta.get(key, [])))

    def test_robots_sitemap_and_crawl_access(self):
        robots = RobotFileParser()
        robots.parse((ROOT / 'robots.txt').read_text().splitlines())
        self.assertEqual(robots.site_maps(), [ORIGIN + '/sitemap.xml'])
        for agent in ('Googlebot', 'Bingbot', '*'):
            for path in ('/', '/?utm_source=test', '/style.css', '/light.css', '/index.html', '/light.html'):
                self.assertTrue(robots.can_fetch(agent, ORIGIN + path))

    def test_metadata_consistency(self):
        self.assertEqual(self.html.canonical, [ORIGIN + '/'])
        description = self.html.meta['description']
        self.assertEqual(len(description), 1)
        self.assertTrue(description[0].strip())
        self.assertEqual(self.html.meta['og:description'], description)
        self.assertEqual(self.html.meta['og:url'], self.html.canonical)
        self.assertEqual(self.html.meta['og:type'], ['website'])
        self.assertEqual(self.html.meta['og:site_name'], ['Dompi'])
        self.assertEqual(self.html.meta['og:locale'], ['id_ID'])
        self.assertEqual(self.html.meta['og:title'], ['Dompi — Catat Uang Tanpa Ribet'])

    def test_homepage_body_and_ctas_preserved(self):
        # light.html is the unchanged former duplicate, retained as a content fixture.
        baseline = (ROOT / 'light.html').read_text()
        self.assertEqual(self.home.split('<body>', 1)[1], baseline.split('<body>', 1)[1])
        self.assertEqual(self.html.links, HTML(baseline).links)
        for link in self.html.links:
            self.assertNotIn(urlsplit(link).path, ('/index.html', '/light.html', 'index.html', 'light.html'))

    def test_redirects_are_narrow_and_permanent(self):
        config = json.loads((ROOT / 'vercel.json').read_text())
        self.assertEqual(set(config), {'$schema', 'redirects'})
        self.assertEqual(config['redirects'], [
            {'source': '/index.html', 'destination': '/', 'permanent': True},
            {'source': '/light.html', 'destination': '/', 'permanent': True}])
        # No catch-all, no rewrites, no noindex headers, no build/runtime override.
        self.assertNotIn('/', [r['source'] for r in config['redirects']])

    def test_static_discovery_files_and_assets_served_without_build(self):
        for path, mime in (('/sitemap.xml', 'application/xml'), ('/robots.txt', 'text/plain'),
                           ('/style.css', 'text/css'), ('/light.css', 'text/css')):
            with self.subTest(path=path):
                status, headers, body = static_get(path)
                self.assertEqual(status, 200)
                if path.endswith('.xml'):
                    self.assertTrue('application/xml' in headers or 'text/xml' in headers)
                    ET.fromstring(body)
                else:
                    self.assertIn('content-type: ' + mime, headers)
                self.assertEqual(body, (ROOT / path[1:]).read_bytes())
                status, _, body = static_get(path, 'HEAD')
                self.assertEqual(status, 200)
                self.assertEqual(body, b'')

    def test_unknown_and_unpublished_routes_remain_404(self):
        for path in ('/help', '/privacy', '/terms', '/upgrade', '/not-a-page'):
            self.assertEqual(static_get(path)[0], 404)

    def test_query_does_not_change_canonical(self):
        status, _, body = static_get('/?utm_source=seo-test')
        self.assertEqual(status, 200)
        self.assertEqual(HTML(body.decode()).canonical, [ORIGIN + '/'])


if __name__ == '__main__':
    unittest.main()
