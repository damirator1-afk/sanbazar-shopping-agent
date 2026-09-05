# Copyright 2026 SanBazar
# SPDX-License-Identifier: Apache-2.0

"""StorefrontBackend поверх ЖИВОГО каталога SanBazar в Sanity (GROQ, читается
через httpx). Поиск/карточка товара — реальные данные, обновляются с коротким
TTL-кэшем (не бьём Sanity на каждый вызов инструмента внутри одного хода).
Корзина — рабочая, но только в памяти процесса (см. README на будущее про
несколько воркеров). Заказы/политики/доставка — честные заглушки, потому что
структурированных данных для них пока нет ни в Sanity, ни где-либо ещё.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from urllib.parse import quote

import httpx
from shopping_agent import (
    Cart,
    CartItem,
    Order,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    StorefrontBackend,
    Unavailable,
    UserPreferences,
    FulfillmentOption,
)

_WORD = re.compile(r"[a-zA-Zа-яёА-ЯЁ0-9]+")
_SEARCH_WEIGHTS = {"title": 3.0, "brand": 2.0, "category": 2.0, "subcategory": 2.5, "description": 1.0}
_CATALOG_TTL_SECONDS = 60

_GROQ_QUERY = (
    '*[_type=="product" && hidden != true]'
    '{_id, article, title, brand, category, subcategory, price, inStock, description, specs, '
    '"imageUrl": images[0].asset->url}'
)


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _score(fields: dict[str, str], query_tokens: list[str]) -> float:
    field_tokens = {name: set(_tokens(text)) for name, text in fields.items()}
    score = 0.0
    for token in query_tokens:
        score += max(
            (weight for name, weight in _SEARCH_WEIGHTS.items() if token in field_tokens.get(name, set())),
            default=0.0,
        )
    return score


class _SessionCarts:
    def __init__(self) -> None:
        self._lines: dict[str, dict[str, CartItem]] = {}

    def lines(self, session_id: str) -> dict[str, CartItem]:
        return self._lines.setdefault(session_id, {})

    def cart(self, session_id: str) -> Cart:
        return Cart(items=list(self.lines(session_id).values()), currency="KZT")

    def put(self, session_id: str, product: ProductDetails, quantity: int) -> Cart:
        self.lines(session_id)[product.product_id] = CartItem(
            product_id=product.product_id,
            title=product.title,
            price=product.price,
            quantity=quantity,
            image_url=product.image_url,
        )
        return self.cart(session_id)

    def set_quantity(self, session_id: str, product_id: str, quantity: int) -> Cart:
        lines = self.lines(session_id)
        if product_id in lines:
            lines[product_id] = lines[product_id].model_copy(update={"quantity": quantity})
        return self.cart(session_id)

    def remove(self, session_id: str, product_id: str) -> Cart:
        self.lines(session_id).pop(product_id, None)
        return self.cart(session_id)

    def reset(self, session_id: str) -> None:
        self._lines.pop(session_id, None)


# TODO: заменить настоящими текстами, когда/если появятся в структурированном
# виде — сейчас нигде не оформлены официально, это ручная сводка по памяти.
_POLICIES = [
    Policy(
        policy_id="wholesale",
        title="Оптовые цены",
        category="pricing",
        content=(
            "SanBazar — оптовый склад сантехники в Актобе. Цены на сайте — оптовые. "
            "Для конкретного объёма и точной цены свяжитесь с менеджером."
        ),
    ),
    Policy(
        policy_id="delivery",
        title="Доставка и самовывоз",
        category="fulfillment",
        content=(
            "Самовывоз со склада в Актобе. Доставка обсуждается индивидуально при "
            "оформлении заказа — точные условия и стоимость сообщает менеджер."
        ),
    ),
    Policy(
        policy_id="ordering",
        title="Как оформить заказ",
        category="orders",
        content=(
            "Заказ оформляется через Telegram-бота или через форму заявки для "
            "организаций на сайте — на сайте нет отдельного личного кабинета с историей заказов."
        ),
    ),
]


class SanityUnavailable(Exception):
    """Sanity недоступен или переменные окружения не заданы."""


class SanBazarBackend(StorefrontBackend):
    store_name = "SanBazar"

    def __init__(self) -> None:
        self.project_id = os.environ["SANITY_PROJECT_ID"]
        self.dataset = os.environ["SANITY_DATASET"]
        self.token = os.environ["SANITY_READ_TOKEN"]
        self.products: dict[str, ProductDetails] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self._carts = _SessionCarts()

    async def _ensure_fresh(self) -> None:
        if time.monotonic() - self._loaded_at < _CATALOG_TTL_SECONDS:
            return
        async with self._lock:
            if time.monotonic() - self._loaded_at < _CATALOG_TTL_SECONDS:
                return  # другой вызов уже обновил, пока мы ждали лок
            url = (
                f"https://{self.project_id}.api.sanity.io/v2024-01-01/data/query/"
                f"{self.dataset}?query={quote(_GROQ_QUERY)}"
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {self.token}"})
            if resp.status_code != 200:
                if not self.products:
                    raise SanityUnavailable(f"Sanity вернул {resp.status_code}")
                return  # каталог уже был загружен раньше — работаем со старым, не роняем сервис
            rows = resp.json().get("result", [])
            fresh: dict[str, ProductDetails] = {}
            for row in rows:
                article = row.get("article") or row["_id"]
                if not row.get("title") or not row.get("price"):
                    continue
                fresh[article] = ProductDetails(
                    product_id=article,
                    title=row["title"],
                    brand=row.get("brand"),
                    price=float(row.get("price") or 0),
                    currency="KZT",
                    category=row.get("category"),
                    attributes={"subcategory": row.get("subcategory") or ""},
                    in_stock=bool(row.get("inStock", True)),
                    image_url=row.get("imageUrl"),
                    short_description=row.get("description") or "",
                    long_description=row.get("description") or "",
                    specs={"details": row.get("specs")} if row.get("specs") else {},
                )
            self.products = fresh
            self._loaded_at = time.monotonic()

    def product(self, product_id: str) -> ProductDetails | None:
        return self.products.get(product_id)

    def reset_session(self, session_id: str) -> None:
        self._carts.reset(session_id)

    def recent_orders(self, limit: int = 6) -> list[Order]:
        del limit
        return []  # нет сквозной истории заказов ни у одного пользователя

    # -- Catalog ------------------------------------------------------------------

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        del session
        await self._ensure_fresh()
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        scored = []
        for p in self.products.values():
            fields = {
                "title": p.title,
                "brand": p.brand or "",
                "category": p.category or "",
                "subcategory": p.attributes.get("subcategory", ""),
                "description": p.short_description or "",
            }
            s = _score(fields, query_tokens)
            if s > 0:
                scored.append((s, p))
        if filters and filters.category:
            narrowed = [(s, p) for s, p in scored if filters.category.lower() in (p.category or "").lower()]
            scored = narrowed or scored
        scored.sort(key=lambda pair: -pair[0])
        return [
            Product.model_validate(p.model_dump(exclude={"long_description", "specs", "variants"}))
            for _, p in scored[:limit]
        ]

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        del session
        await self._ensure_fresh()
        return self.products.get(product_id)

    # -- Cart (рабочая, только в памяти процесса) ----------------------------------

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        return self._carts.cart(session.session_id)

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        product = self.products.get(product_id)
        if product is None:
            raise KeyError(product_id)
        if not product.in_stock:
            raise Unavailable(f"{product_id} нет в наличии")
        existing = self._carts.lines(session.session_id).get(product_id)
        quantity += existing.quantity if existing else 0
        return self._carts.put(session.session_id, product, quantity)

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        return self._carts.set_quantity(session.session_id, product_id, quantity)

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        return self._carts.remove(session.session_id, product_id)

    # -- Клиент, заказы, политики, доставка (честные заглушки) --------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        return UserPreferences(user_id=session.user_id, display_name="Гость")

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        del session, limit
        return []

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        del session, order_id
        return None

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        del session
        terms = set(_tokens(query))
        if not terms:
            return []
        scored = [(len(terms & set(_tokens(f"{p.title} {p.content}"))), p) for p in _POLICIES]
        scored = [(s, p) for s, p in scored if s > 0]
        scored.sort(key=lambda pair: -pair[0])
        return [p for _, p in scored] or _POLICIES

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        del session, product_ids
        return [
            FulfillmentOption(method="pickup", eta="в день заказа", fee=0.0, location="склад SanBazar, Актобе"),
            FulfillmentOption(method="delivery", eta="по согласованию с менеджером", fee=0.0),
        ]
