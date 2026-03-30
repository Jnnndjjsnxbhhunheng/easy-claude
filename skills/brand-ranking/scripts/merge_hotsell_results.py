#!/usr/bin/env python3
import json
import sys
from typing import Any


def load_payload() -> dict[str, Any]:
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'r', encoding='utf-8') as fh:
            return json.load(fh)
    return json.load(sys.stdin)


def normalize_brand_name(value: Any) -> str:
    if not isinstance(value, str):
        return ''
    return ' '.join(value.strip().split())


def product_identity(product: dict[str, Any]) -> str:
    for key in ('product_id', 'sku_id', 'item_id', 'ware_id', 'id'):
        value = product.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return f'{key}:{value}'
    for key in ('url', 'link'):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return f'{key}:{value.strip()}'
    title = product.get('title') or product.get('product_name') or ''
    return f'title:{str(title).strip()}'


def extract_result_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get('results') or payload.get('executed') or []
    return [item for item in raw if isinstance(item, dict)]


def main() -> int:
    payload = load_payload()
    merged: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []

    for entry in extract_result_items(payload):
        brand_name = normalize_brand_name(entry.get('brand_name') or entry.get('brand'))
        if not brand_name:
            continue
        seen_products: set[str] = set()
        merged_products: list[dict[str, Any]] = []
        pages = entry.get('pages') or entry.get('results') or []
        if not isinstance(pages, list):
            pages = []

        for page in pages:
            if not isinstance(page, dict):
                continue
            items = page.get('items') or page.get('products') or page.get('data') or []
            if not isinstance(items, list):
                continue
            for product in items:
                if not isinstance(product, dict):
                    continue
                identity = product_identity(product)
                if not identity or identity in seen_products:
                    continue
                seen_products.add(identity)
                merged_products.append(product)

        merged.append({'brand_name': brand_name, 'products': merged_products})
        summary.append(
            {
                'brand_name': brand_name,
                'unique_product_count': len(merged_products),
            }
        )

    json.dump(
        {
            'brand_count': len(merged),
            'merged_results': merged,
            'summary': summary,
        },
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
