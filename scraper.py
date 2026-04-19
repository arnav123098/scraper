import httpx
import asyncio
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from collections import deque
from playwright.async_api import async_playwright
import trafilatura

class Scraper:
    ad_domains = [
        'doubleclick.net', 'googlesyndication.com', 'googleadservices.com',
        'adservice.google.com', 'amazon-adsystem.com', 'facebook.com/pagead'
    ]
    
    def __init__(
            self,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            min_para_len=0,
            max_robot_cache=400,
            request_timeout=3,
            max_concurrent=5
        ):
        self.user_agent = user_agent
        self.obey_robots = True
        self.max_robot_cache = max_robot_cache
        self.request_timeout = request_timeout
        self.min_para_len = min_para_len
        self.semaphore = asyncio.Semaphore(max_concurrent)

        self.headers = {'User-Agent': user_agent}

        # js rendering
        self.p = None
        self.browser = None
        self.context = None

        # robots cache
        self.robot_parsers = {}
        self.robot_cache_order = deque()

    async def start_browser(self):
        if not self.p:
            self.p = await async_playwright().start()
        
        self.browser = await self.p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--mute-audio"
            ]
        )

        self.context = await self.browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True
        )

        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
            window.chrome = { runtime: {} };
        """)

    async def stop_browser(self):
        if self.browser:
            await self.browser.close()
        if self.p:
            await self.p.stop()
            
    async def is_crawl_allowed(self, url:str, client:httpx.AsyncClient) -> bool:
        parsed_url = urlparse(url)
        netloc = parsed_url.netloc

        if netloc not in self.robot_parsers:
            if len(self.robot_parsers) >= self.max_robot_cache:
                oldest = self.robot_cache_order.popleft()
                del self.robot_parsers[oldest]
            rp = RobotFileParser()
            robots_url = parsed_url.scheme + '://' + netloc + '/robots.txt'
            rp.set_url(robots_url)

            try:
                response = await client.get(robots_url, timeout=self.request_timeout, follow_redirects=True)
                if response.status_code == 200:
                    rp.parse(response.text.splitlines())
            except Exception: pass

            self.robot_parsers[netloc] = rp
            self.robot_cache_order.append(netloc)
        else:
            self.robot_cache_order.remove(netloc)
            self.robot_cache_order.append(netloc)

        return self.robot_parsers[netloc].can_fetch(self.user_agent, url)
    
    async def get_html(self, url:str, client:httpx.AsyncClient) -> str:
        try:
            response = await client.get(url=url, timeout=self.request_timeout, follow_redirects=True)
            if response.status_code == 200: return True, response.text
        except Exception as e: return False, e
        
    async def _get_html_r(self, page, url:str) -> str:
        try:
            await page.route('**/*', Scraper.abort_ads)
            await page.goto(url, timeout=15000, wait_until='domcontentloaded')
            await page.wait_for_selector('body', timeout=15000)
            await page.wait_for_timeout(500)

            try:
                await page.locator("button:has-text('Reject All')").click(timeout=2000)
            except: pass

            html = await page.content()

            return True, html
        except Exception as e: return False, e
        
    async def get_rendered(self, url:str) -> str:
        try:
            page = await self.context.new_page()
            await page.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
            })

            return await self._get_html_r(page, url)
        finally: await page.close()
        
    @staticmethod
    async def abort_ads(route):
        if any(domain in route.request.url for domain in Scraper.ad_domains):
            await route.abort()
        else:
            await route.continue_()

    @staticmethod
    def filter_ads(html:str):
        clean_html = trafilatura.extract(html, output_format='html')

        if clean_html is None:
            clean_html = trafilatura.extract(html, output_format='html', include_comments=False)
        
        return clean_html

    @staticmethod
    def get_absolute_url(base_url:str, link:str) -> str:
        abs_link = urljoin(base_url, link)
        parsed_link = urlparse(abs_link)
        if not parsed_link.netloc or not parsed_link.scheme:
            return ''

        if parsed_link.scheme not in ('http', 'https'):
            return ''

        return parsed_link.scheme + '://' + parsed_link.netloc + parsed_link.path
    
    @staticmethod
    def is_external_link(base_url:str, link:str) -> bool:
        link_netloc = urlparse(link).netloc
        return bool(link_netloc and link_netloc != urlparse(base_url).netloc)
    
    def scrape_links_and_paragraphs(self, html:str, base_url:str, include_links:bool=False) -> dict:
        clean_html = Scraper.filter_ads(html)
        if not clean_html: return

        soup = BeautifulSoup(clean_html, 'html.parser')
        title = soup.title.get_text().strip() if soup.title else None
        
        if include_links:
            links = [a['href'] for a in soup.find_all('a', href=True) if a['href']]

            unique_links = []
            seen_links = set()
            for link in links:
                abs_link = Scraper.get_absolute_url(base_url, link)
                if abs_link and abs_link != base_url and abs_link not in seen_links:
                    unique_links.append(abs_link)
                    seen_links.add(abs_link)

        else:
            unique_links = []

        for noise in soup(['nav', 'footer', 'script', 'style']):
            noise.decompose()

        targets = [t for t in soup.find_all(['p', 'div', 'span', 'li', 'article', 'section']) if len(t.get_text(strip=True)) > self.min_para_len and not t.find(['div', 'section'])]

        paragraphs = [' '.join(t.get_text().strip().split()) for t in targets]

        final_paragraphs = []
        seen_paragraphs = set()
        for p in paragraphs:
            if p not in seen_paragraphs:
                final_paragraphs.append(p)
                seen_paragraphs.add(p)

        data = {
            'title': title,
            'paragraphs': final_paragraphs,
        }

        if include_links: data['links'] = unique_links

        return data
    
    async def process_url(self, url:str, render_js:bool, client:httpx.AsyncClient):
        async with self.semaphore:
            if not self.obey_robots or await self.is_crawl_allowed(url, client):
                if render_js:
                    status, content = await self.get_rendered(url)
                else:
                    status, content = await self.get_html(url, client)
                
                if status:
                    return self.scrape_links_and_paragraphs(content, url)
                else:
                    return {'error': content}
            else:
                return {'error': 'crawl disallowed'}

    async def __call__(self, urls:list, render_js:bool=False) -> list[dict]:
        if render_js and not self.browser: await self.start_browser()

        async with httpx.AsyncClient(headers=self.headers) as client:
            tasks = [self.process_url(url, render_js, client) for url in urls]
            return await asyncio.gather(*tasks)

import json
async def main():
    s = Scraper()
    s.obey_robots = False
    u = 'https://www.calculatorsoup.com/calculators/discretemathematics/factorials.php'
    v = 'https://quotes.toscrape.com/js'

    try:
        results = await s([u, v], render_js=True)
        print(f'scraped pages: {json.dumps(results, indent=2)}')
    finally:
        await s.stop_browser()

if __name__ == "__main__":
    asyncio.run(main())
