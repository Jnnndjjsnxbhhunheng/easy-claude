#!/usr/bin/env python3
import json
import sys
from typing import Any


def load_payload() -> Any:
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'r', encoding='utf-8') as fh:
            return json.load(fh)
    return json.load(sys.stdin)


def coerce_payload(raw_payload: Any) -> dict[str, Any]:
    if isinstance(raw_payload, dict):
        return raw_payload
    if isinstance(raw_payload, list):
        candidates = [item for item in raw_payload if isinstance(item, dict)]
        return {
            'candidate_pool': candidates,
        }
    return {}


def normalize_brand_name(value: Any) -> str:
    if not isinstance(value, str):
        return ''
    return ' '.join(value.strip().split())


def extract_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ('candidates', 'brands', 'candidate_pool'):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def tier_rank(entry: dict[str, Any]) -> int:
    tier = entry.get('candidate_tier')
    if tier == 'stable':
        return 0
    if tier == 'provisional':
        return 1
    return 9


def sort_key(entry: dict[str, Any]) -> tuple[int, float, float, str]:
    semantic = float(entry.get('semantic_score') or 0)
    mention_count = float(entry.get('mention_count') or 0)
    name = normalize_brand_name(entry.get('brand_name') or entry.get('name'))
    return (tier_rank(entry), -semantic, -mention_count, name)


def main() -> int:
    payload = coerce_payload(load_payload())
    category = payload.get('category') or payload.get('slot') or ''
    default_rn = int(payload.get('rn') or 10)
    default_pages = payload.get('pn_sequence') or [1]
    if not isinstance(default_pages, list) or not default_pages:
        default_pages = [1]

    seen: set[str] = set()
    plan: list[dict[str, Any]] = []
    frozen_candidates: list[dict[str, Any]] = []

    for entry in sorted(extract_candidates(payload), key=sort_key):
        candidate_tier = entry.get('candidate_tier')
        if candidate_tier not in {'stable', 'provisional'}:
            continue

        brand_name = normalize_brand_name(entry.get('brand_name') or entry.get('name'))
        if not brand_name or brand_name in seen:
            continue
        seen.add(brand_name)

        frozen_candidates.append(
            {
                'brand_name': brand_name,
                'candidate_tier': candidate_tier,
                'semantic_score': entry.get('semantic_score', 0),
                'mention_count': entry.get('mention_count', 0),
                'source_rounds': entry.get('source_rounds', []),
            }
        )
        plan.append(
            {
                'brand_name': brand_name,
                'candidate_tier': candidate_tier,
                'query': f'{brand_name} {category}'.strip(),
                'rn': default_rn,
                'pn_sequence': default_pages,
                'must_execute': True,
            }
        )

    result = {
        'category': category,
        'candidate_count': len(frozen_candidates),
        'frozen_candidates': frozen_candidates,
        'plan': [
            {
                'index': index,
                **item,
            }
            for index, item in enumerate(plan, start=1)
        ],
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
