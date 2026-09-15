"""Расчёт сметы: количество в упаковках или в единицах (кг, л, м, м²), доставка по
магазинам и точный перебор комбинаций магазинов.

Единицы количества: "уп" — целая упаковка/штука, остальное — потребность в единицах;
тогда покупается целое число упаковок (весовой товар — дробно), считается остаток.
"""
import itertools
import math

from app.db import get_session
from app.matching import material_signature, sold_by_weight
from app.models import Store


def purchase(product, item, discounts):
    from app.main import _product_to_dict
    if item.qty_unit == "уп":
        packs = math.ceil(item.qty)
        supplied = packs
    else:
        if product.pack_unit != item.qty_unit or not product.pack_value:
            return None
        # Explicit weight goods can be purchased fractionally; other goods need whole packs.
        packs = item.qty / product.pack_value if sold_by_weight(product.name) else math.ceil(round(item.qty / product.pack_value, 10))
        supplied = packs * product.pack_value
    data = _product_to_dict(product, discounts)
    return {**data, "packages": packs, "supplied": round(supplied, 4),
            "surplus": round(supplied - item.qty, 4), "qty_unit": item.qty_unit,
            "line_total": round(data["effective_price"] * packs, 2)}


def calculate(req):
    from app.main import (  # локальный импорт: main импортирует этот модуль
        _clean_discounts, _pinned_product, _relevance_tier, dominant_pack_unit, find_matches, typical_pack_value,
    )
    discounts = _clean_discounts(req.discounts)
    delivery = {slug: min(max(value, 0), 1_000_000) for slug, value in req.delivery.items() if math.isfinite(value)}
    session = get_session()
    try:
        names = {s.slug: s.name for s in session.query(Store).all()}
        lines = []
        for index, item in enumerate(req.items):
            line = {"index": index, "query": item.query, "qty": item.qty, "qty_unit": item.qty_unit,
                    "status": "not_found", "pinned": bool(item.url), "best": None, "by_store": {},
                    "relaxed": False, "needs_review": False}
            lines.append(line)
            if item.url:
                product = _pinned_product(session, item.url)
                if product is None:
                    line["status"] = "unavailable"
                else:
                    line["best"] = purchase(product, item, discounts)
                    line["status"] = "ok" if line["best"] else "unit_mismatch"
                continue
            matches = find_matches(session, item.query)
            if not matches.products:
                continue
            # Автоподбор делается всегда, а сомнения — флаг needs_review, не отказ:
            # человек с готовой сметой хочет цифру, а не список вопросов. Сомнения:
            # совпали не все слова запроса, или у кандидатов разные названия (марки, бренды).
            if matches.relaxed:
                line["relaxed"] = True
                line["needs_review"] = True
            products = [p for p in matches.products if _relevance_tier(p, matches.keywords) == 0] or matches.products
            if len({material_signature(p) for p in products}) > 1:
                line["needs_review"] = True

            unit = dominant_pack_unit(products)
            typical = typical_pack_value(products, unit)
            if item.qty_unit == "уп" and typical:
                # Количество в упаковках: "40 мешков" — это мешки обычного размера, а не биг-бэги
                # и не пакетики; сравниваем только товары с типичной фасовкой.
                sized = [p for p in products if p.pack_unit == unit and p.pack_value == typical]
                if sized:
                    products = sized

            def option_key(option):
                # Сначала то, что есть в Бийске; при сопоставимой фасовке — цена за единицу,
                # иначе — стоимость строки (2 кг за 154 ₽ не должны выигрывать у 50 кг за 580 ₽)
                comparable = bool(unit and option["pack_unit"] == unit and option["unit_price"])
                return (option["in_stock"] is False, 0 if comparable else 1,
                        option["unit_price"] if comparable else option["line_total"])

            for product in products:
                option = purchase(product, item, discounts)
                if option is None:
                    continue
                current = line["by_store"].get(product.store.slug)
                if current is None or option_key(option) < option_key(current):
                    line["by_store"][product.store.slug] = option
            if line["by_store"]:
                line["best"] = min(line["by_store"].values(), key=lambda p: (p["in_stock"] is False, p["line_total"]))
                line["status"] = "ok"
            else:
                line["status"] = "unit_mismatch"

        resolved = [line for line in lines if line["best"]]
        auto = [line for line in resolved if not line["pinned"]]
        pinned = [line for line in resolved if line["pinned"]]
        fixed_stores = {line["best"]["store_slug"] for line in pinned}
        fixed_total = round(sum(line["best"]["line_total"] for line in pinned), 2)
        slugs = sorted({slug for line in auto for slug in line["by_store"]})
        # Exclude on-order alternatives when the same line is available elsewhere.
        for line in auto:
            if any(v["in_stock"] is not False for v in line["by_store"].values()):
                line["by_store"] = {k: v for k, v in line["by_store"].items() if v["in_stock"] is not False}

        def scenario(selected):
            selected = set(selected)
            choices, missing = [], []
            for line in auto:
                options = [v for k, v in line["by_store"].items() if k in selected]
                if not options:
                    missing.append(line["query"])
                else:
                    choices.append(min(options, key=lambda v: v["line_total"]))
            used = fixed_stores | {p["store_slug"] for p in choices}
            goods = round(fixed_total + sum(p["line_total"] for p in choices), 2)
            shipping = round(sum(delivery.get(slug, 0) for slug in used), 2)
            return {"goods_total": goods, "delivery_total": shipping, "total": round(goods + shipping, 2),
                    "complete": not missing, "missing": missing, "choices": choices,
                    "stores": sorted(used)}

        options = []
        for slug in slugs:
            option = scenario([slug])
            options.append({**{k:v for k,v in option.items() if k != "choices"},
                            "store": names.get(slug, slug), "store_slug": slug,
                            "discount_percent": discounts.get(slug, 0)})
        options.sort(key=lambda o: (not o["complete"], o["total"]))
        candidates = [scenario(slugs)]
        # Current catalog has two stores. Enumerate subsets exactly for up to twelve.
        if len(slugs) <= 12:
            candidates = [scenario(combo) for count in range(len(slugs)+1)
                          for combo in itertools.combinations(slugs, count)]
        complete = [c for c in candidates if c["complete"]]
        chosen = min(complete, key=lambda c: (c["total"], len(c["stores"]))) if complete else scenario(slugs)
        for line, choice in zip(auto, chosen["choices"]):
            line["best"] = choice
        single = next((o for o in options if o["complete"]), None)
        return {"lines": lines, "unresolved": [l["query"] for l in lines if l["status"] != "ok"],
                "optimal_total": chosen["total"], "goods_total": chosen["goods_total"],
                "delivery_total": chosen["delivery_total"], "pinned_total": fixed_total,
                "store_options": options, "selected_stores": chosen["stores"],
                "savings_vs_single_store": round(single["total"] - chosen["total"], 2) if single else None}
    finally:
        session.close()
