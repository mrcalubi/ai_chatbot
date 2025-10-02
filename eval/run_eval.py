import json, sys, requests

API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8005"
cases = json.load(open("eval/cases.json"))

def hit(case):
    payload = {"query": case["query"], "k": 8, "strict": case.get("strict", True)}
    r = requests.post(f"{API}/chat", json=payload, timeout=60)
    r.raise_for_status()
    return r.json()

passed = 0
for c in cases:
    res = hit(c)
    text = (res.get("answer") or "").lower()
    pages = " ".join([str(x.get("page")) for x in res.get("citations", [])])
    ok = any(tok in text for tok in c["expect_any_page_contains"])
    print(f"[{'PASS' if ok else 'FAIL'}] {c['name']}  | pages: {pages}")
    if ok: passed += 1

print(f"\n{passed}/{len(cases)} passed")
