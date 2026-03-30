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


def main() -> int:
    payload = load_payload()
    plan = payload.get('plan') or []
    merged_results = payload.get('merged_results') or payload.get('results') or []

    required = {
        normalize_brand_name(item.get('brand_name'))
        for item in plan
        if isinstance(item, dict) and item.get('must_execute')
    }
    required.discard('')

    completed = {
        normalize_brand_name(item.get('brand_name') or item.get('brand'))
        for item in merged_results
        if isinstance(item, dict)
    }
    completed.discard('')

    missing = sorted(required - completed)
    extra = sorted(completed - required)
    result = {
        'candidate_count': len(required),
        'executed_count': len(completed),
        'missing_brands': missing,
        'extra_brands': extra,
        'is_complete': not missing,
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write('\n')
    return 0 if result['is_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
