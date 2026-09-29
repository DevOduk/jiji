from fastapi import FastAPI, HTTPException
from typing import List, Optional
import urllib.parse
import re
import math
import time
import random
import requests

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed


# Initialize the FastAPI app
app = FastAPI(
    title="Jiji Car Scraper API",
    version="1.0",
)


# Jiji configuration
BASE_URL = "https://jiji.co.ke/api_web/v1/listing"
Jiji_BASE_URL = "https://jiji.co.ke"

PAGE_SIZE = 20
IMAGE_WORKERS = 3

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Referer": "https://jiji.co.ke/cars",
}

session = requests.Session()
session.headers.update(headers)


# Mock database / storage for your scraped cars
CAR_DATABASE = []


@app.get("/")
def read_root():
    return {
        "message": (
            "Welcome to the Jiji Scraper API! "
            "Visit /docs for interactive documentation."
        )
    }


# ---------------------------------------------------------------------------
# Jiji listing scraper
# ---------------------------------------------------------------------------

def scrape_cars(
    make: str,
    model: Optional[str] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    max_pages: int = 20,
):
    cars = []

    page = 1
    total = None
    total_pages = None

    while page <= max_pages:
        params = {
            "filter_attr_1_make": make,
            "slug": "cars",
            "init_page": "true",
            "page": page,
            "webp": "true",
        }

        if model:
            params["filter_attr_2_model"] = model

        if min_price is not None:
            params["price_min"] = min_price

        if max_price is not None:
            params["price_max"] = max_price

        if min_year is not None:
            params["filter_attr_119_year_of_manufacture__min"] = min_year

        if max_year is not None:
            params["filter_attr_119_year_of_manufacture__max"] = max_year

        try:
            response = session.get(
                BASE_URL,
                params=params,
                timeout=20,
            )

            response.raise_for_status()

            data = response.json()

        except requests.RequestException as error:
            raise HTTPException(
                status_code=502,
                detail=f"Jiji request failed: {error}",
            )

        except ValueError:
            raise HTTPException(
                status_code=502,
                detail="Jiji returned invalid JSON.",
            )

        adverts_list = data.get("adverts_list", {})
        adverts = adverts_list.get("adverts", [])

        if total is None:
            total = adverts_list.get("count", 0)

            total_pages = math.ceil(total / PAGE_SIZE)

            total_pages = min(
                total_pages,
                max_pages,
            )

        if not adverts:
            break

        for car in adverts:
            attributes = {
                attr["name"]: attr["value"]
                for attr in car.get("attrs", [])
                if "name" in attr and "value" in attr
            }

            cars.append(
                {
                    "id": car.get("id"),
                    "guid": car.get("guid"),
                    "title": car.get(
                        "fb_view_content_data",
                        {},
                    ).get("content_name"),
                    "details": car.get("details"),
                    "price": car.get(
                        "price_obj",
                        {},
                    ).get("value"),
                    "location": car.get("region_item_text"),
                    "price_display": car.get(
                        "price_obj",
                        {},
                    ).get("view"),
                    "image": car.get(
                        "image_obj",
                        {},
                    ).get("url"),
                    "images": [],
                    "images_count": car.get("images_count"),
                    "url": car.get("url"),
                    "attributes": attributes,
                }
            )

        if page >= total_pages:
            break

        page += 1

    return cars


# ---------------------------------------------------------------------------
# Product image scraper
# ---------------------------------------------------------------------------

def get_product_images(url: str):
    if not url:
        return []

    product_url = urljoin(
        Jiji_BASE_URL,
        url,
    )

    for attempt in range(4):
        try:
            # Random delay helps avoid hammering the site
            time.sleep(
                random.uniform(0.5, 1.5)
            )

            response = requests.get(
                product_url,
                headers=headers,
                timeout=15,
            )

            if response.status_code == 429:
                wait = 3 * (attempt + 1)

                print(
                    f"429 rate limited: {product_url} "
                    f"| retrying in {wait}s"
                )

                time.sleep(wait)
                continue

            if response.status_code != 200:
                print(
                    f"Status {response.status_code} "
                    f"for {product_url}"
                )

                continue

            html_text = response.text

            images = []

            # Strategy 1: Search raw HTML / Nuxt payload
            pattern = (
                r'https://pictures-kenya\.jijistatic\.com/'
                r'[^\s"\'<>]+'
            )

            found_urls = re.findall(
                pattern,
                html_text,
            )

            for src in found_urls:
                src = src.replace(
                    r"\u002F",
                    "/",
                )

                src = (
                    src.split('"')[0]
                    .split("'")[0]
                    .split("\\")[0]
                )

                if (
                    src not in images
                    and (
                        "_" in src
                        or "webp" in src
                        or "jpg" in src
                    )
                ):
                    images.append(src)

            if images:
                return images

            # Strategy 2: BeautifulSoup carousel
            soup = BeautifulSoup(
                html_text,
                "html.parser",
            )

            carousel = (
                soup.select_one(".VueCarousel-inner")
                or soup.select_one(
                    ".b-slider-image__wrapper"
                )
            )

            if carousel:
                for img in carousel.select("img"):
                    src = (
                        img.get("src")
                        or img.get("data-src")
                    )

                    if (
                        src
                        and src not in images
                        and "jijistatic.com" in src
                    ):
                        images.append(src)

            # Strategy 3: All Jiji images
            if not images:
                for img in soup.find_all("img"):
                    src = (
                        img.get("src")
                        or img.get("data-src")
                    )

                    if (
                        src
                        and "jijistatic.com" in src
                        and src not in images
                    ):
                        if (
                            "avatar" not in src
                            and "icon" not in src
                        ):
                            images.append(src)

            return images

        except requests.RequestException as error:
            print(
                f"Request error: {error}"
            )

            if attempt < 3:
                time.sleep(
                    2 * (attempt + 1)
                )
            else:
                return []

        except Exception as error:
            print(
                f"Image scraping error: {error}"
            )

            return []

    return []


# ---------------------------------------------------------------------------
# Home / listing endpoint
# ---------------------------------------------------------------------------

@app.get(
    "/api/cars",
    response_model=List[dict],
)
def get_cars(
    make: str = "Nissan",
    model: Optional[str] = "Note",
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    max_pages: int = 20,
    limit: Optional[int] = 10,
):
    """
    Scrapes Jiji listing metadata.

    The listing endpoint does NOT scrape individual product pages.
    This keeps the home page relatively fast.
    """

    if min_price is not None and max_price is not None:
        if min_price > max_price:
            raise HTTPException(
                status_code=400,
                detail="min_price cannot be greater than max_price",
            )

    if min_year is not None and max_year is not None:
        if min_year > max_year:
            raise HTTPException(
                status_code=400,
                detail="min_year cannot be greater than max_year",
            )

    cars = scrape_cars(
        make=make,
        model=model,
        min_price=min_price,
        max_price=max_price,
        min_year=min_year,
        max_year=max_year,
        max_pages=max_pages,
    )

    # Update the in-memory database
    CAR_DATABASE.clear()
    CAR_DATABASE.extend(cars)

    if limit is not None:
        return cars[:limit]

    return cars


# ---------------------------------------------------------------------------
# Lazy-loaded product images
# ---------------------------------------------------------------------------

@app.get("/api/car/images")
def get_car_images(
    url: str,
):
    """
    Scrapes images from an individual Jiji product page.

    Example:

    /api/car/images?url=/parklands-highridge/cars/nissan-note-2015.html
    """

    if not url:
        raise HTTPException(
            status_code=400,
            detail="URL parameter is required",
        )

    # Decode URL safely
    decoded_url = urllib.parse.unquote(url)

    # Scrape the product page
    images = get_product_images(
        decoded_url
    )

    return {
        "url": decoded_url,
        "images_count": len(images),
        "images": images,
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "cars_cached": len(CAR_DATABASE),
    }
