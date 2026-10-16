#!/usr/bin/env python3
"""Refresh TokenBoard's frontier price table with current list rates.

Source: OpenRouter's public model API (https://openrouter.ai/api/v1/models) —
free, no API key, one request. OpenRouter is a third-party relay, not the
vendors themselves; it is the only practical no-key automated source (OpenAI
and Google's own pricing pages block automated fetches). Each entry in
prices.json carries the fetch date in the file's `note` field.

Selection rules (conservative — for cost-savings claims):
  * exact model-id match preferred
  * `:batch` variants excluded (half-price, not the list rate)
  * `-mini` / `-nano` / dated (YYYY-MM-DD) siblings excluded
  * if several candidates remain, the HIGHER output rate wins (preview tiers,
    regional variants) and the choice is noted
  * all-or-nothing: prices.json is rewritten only when every configured model
    resolves; otherwise the existing table is kept and the misses are listed

Stdlib only. Usage:  python3 scripts/refresh_prices.py [prices.json]
"""
import json
import re
import sys
import urllib.request
from datetime import date

OR_URL = "https://openrouter.ai/api/v1/models"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# (display name, vendor, preferred openrouter id, fallback id prefix)
# Edit this list to change which models the dashboard compares against.
MODELS = [
    ("GPT-5.5",         "OpenAI",    "openai/gpt-5.5",            "openai/gpt-5.5"),
    ("Claude Opus 5.5", "Anthropic", "anthropic/claude-opus-5.5", "anthropic/claude-opus-5"),
    ("Gemini 3.1 Pro",  "Google",    "google/gemini-3.1-pro",     "google/gemini-3.1-pro"),
    ("Grok 4.7",        "xAI",       "x-ai/grok-4.7",             "x-ai/grok-4.7"),
    ("GPT-4o",          "OpenAI",    "openai/gpt-4o",             "openai/gpt-4o"),
]

EXCLUDE = re.compile(r"(:batch|[-:]mini|[-:]nano|\d{4}-\d{2}-\d{2})")


def fetch_models():
    req = urllib.request.Request(OR_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def rate(entry):
    p = entry.get("pricing", {})
    try:
        return float(p["prompt"]) * 1e6, float(p["completion"]) * 1e6
    except (KeyError, TypeError, ValueError):
        return None


def resolve(models, preferred, prefix):
    """Return (id, in/1M, out/1M, note) for the best match, or None."""
    for m in models:
        mid = m.get("id", "")
        if mid == preferred and (r := rate(m)) is not None:
            return mid, *r, ""
    cands = []
    for m in models:
        mid = m.get("id", "")
        if mid.startswith(prefix) and not EXCLUDE.search(mid):
            if (r := rate(m)) is not None:
                cands.append((mid, r, "-preview" in mid))
    if not cands:
        return None
    # prefer non-preview, then the shortest id (most "base"), then highest
    # output rate (conservative)
    cands.sort(key=lambda c: (c[2], len(c[0]), -c[1][1]))
    mid, (inp, out), preview = cands[0]
    notes = []
    if preview:
        notes.append("preview-tier rate")
    if len(cands) > 1:
        notes.append(f"chose {mid} of {len(cands)} candidates (highest output rate)")
    return mid, inp, out, "; ".join(notes)


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "prices.json"
    print(f"fetching {OR_URL} ...")
    data = fetch_models()
    models = data.get("data", [])
    print(f"{len(models)} models listed")

    rows, notes, missing = [], [], []
    for name, vendor, preferred, prefix in MODELS:
        res = resolve(models, preferred, prefix)
        if res is None:
            missing.append(name)
            continue
        mid, inp, out, note = res
        rows.append({"name": name, "vendor": vendor,
                     "input_per_m": round(inp, 4), "output_per_m": round(out, 4)})
        tag = f" ({note})" if note else ""
        print(f"  {name:18s} {mid:42s} in/1M=${inp:<8.4g} out/1M=${out:<8.4g}{tag}")
        if note:
            notes.append(f"{name}: {note}")

    if missing:
        print(f"\n! could not resolve: {', '.join(missing)} — keeping existing {out_path}")
        sys.exit(1)

    today = date.today().isoformat()
    base_note = ("List API prices per 1M tokens, fetched %s from OpenRouter "
                 "(third-party relay; vendor pages are bot-walled to "
                 "automated fetches). Batch discounts, prompt-cache rates, "
                 "and region multipliers are excluded. Excludes local "
                 "electricity/labor." % today)
    if notes:
        base_note += " " + "; ".join(notes)
    table = {
        "currency": "USD",
        "verified": today,
        "source": "openrouter.ai/api/v1/models (fetched %s)" % today,
        "note": base_note,
        "models": rows,
    }
    with open(out_path, "w") as f:
        json.dump(table, f, indent=2)
        f.write("\n")
    print(f"\nwrote {out_path} (verified {today}) — dashboard picks it up on next request, no restart")


if __name__ == "__main__":
    main()
