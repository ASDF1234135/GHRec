def gate(m: dict, champ: dict | None = None, p99_budget_ms=200.0):
    """Hard thresholds first, then primary metric vs champion on the SAME snapshot."""
    if m["p99_ms"] > p99_budget_ms:
        return False, f"p99 {m['p99_ms']:.0f}ms over budget"
    if champ and m["e2e_ndcg@10"] <= champ["e2e_ndcg@10"] + 1e-4:  # margin: ignore float noise
        return False, "does not beat champion"
    return True, "ok"
