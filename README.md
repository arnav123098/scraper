# scraper
a weekend project. i dissected an old web scraper i made months ago and added stuff like js rendering, ad filters, concurrency et cetera.
a great learning time for sure.

it uses beautiful soup for scraping, playwright for running browser and trafilatura to filter ads from the fetched page. 
returns a dictionary for a url with the sanitized page content organized into paragraphs. i'll refine it more and add stuff to make it a 'good' scraper for llm web search tools.
