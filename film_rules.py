"""Small, validated FilmBot-specific parser rules; stored in existing settings."""
import re


def clean_rules(raw):
    raw = raw if isinstance(raw, dict) else {}
    def words(key):
        values = raw.get(key, [])
        if not isinstance(values, list):
            return []
        return list(dict.fromkeys(v.lower() for v in values[:100] if isinstance(v, str) and re.fullmatch(r'[a-zA-Z0-9]{1,40}', v)))
    exceptions = {}
    entries = raw.get('exceptions')
    if isinstance(entries, list):
        entries = {e['filename']: [e.get('title'), e.get('year')] for e in entries[:100] if isinstance(e, dict) and isinstance(e.get('filename'), str)}
    if isinstance(entries, dict):
        for k, v in list(entries.items())[:100]:
            if isinstance(k, str) and 0 < len(k) <= 512 and isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and 0 < len(v[0]) <= 160 and (v[1] is None or type(v[1]) is int and 1888 <= v[1] <= 2100):
                exceptions[k] = v
    return {'markers': words('markers'), 'disabled': words('disabled'), 'exceptions': exceptions}


def active_markers(raw):
    r = clean_rules(raw)
    return set(r['markers']) - set(r['disabled']), set(r['disabled'])


def title_exception(raw, rules):
    return clean_rules(rules)['exceptions'].get(raw.strip().casefold())


def stored_rules(raw):
    r = clean_rules(raw)
    r['exceptions'] = [{'filename': k, 'title': v[0], 'year': v[1]} for k, v in r['exceptions'].items()]
    return r
