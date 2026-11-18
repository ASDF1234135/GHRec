def owner_dedup(ids: list[int], repos: list[str], k: int, max_per_owner=2) -> list[int]:
    seen, out = {}, []
    for i in ids:
        o = repos[i].split("/")[0]
        if seen.get(o, 0) < max_per_owner:
            seen[o] = seen.get(o, 0) + 1; out.append(i)
        if len(out) == k:
            break
    return out
